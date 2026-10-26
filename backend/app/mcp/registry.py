"""
Connector registry — resolves a connection_id to a live MCPConnectorBase.

The rest of the app (SchemaManager, chat.py) only knows a connection_id
string until the moment it needs to talk to a warehouse. This registry is
the one place that turns that string into an actual connector instance,
caching one instance per connection_id for the lifetime of the process.

Why this exists instead of a single global connector:
    Multiple Mongo databases (e.g. "olist" and "retail_wh") can be active
    at once, selected per chat session via CreateSessionRequest.connection_id.
    A single settings.MONGODB_URI/MONGODB_DATABASE pair can't express that,
    so named connections live in settings.MONGO_CONNECTIONS instead.

Hive is intentionally excluded from the registry: HiveConnector's methods
are all unimplemented stubs today, and HIVE_* settings describe exactly one
cluster. If/when Hive gains multi-cluster support, extend HIVE settings the
same way MONGO_CONNECTIONS was added here, and branch below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from app.mcp.base import MCPConnectorBase
from app.mcp.hive_connector import HiveConnector
from app.mcp.mongo_connector import MongoDBConnector

if TYPE_CHECKING:
    from app.config import Settings

logger = structlog.get_logger(__name__)


class ConnectorRegistry:
    """
    Lazily builds and caches one MCPConnectorBase per connection_id.

    One registry instance is shared process-wide (see deps.get_connector_registry).
    Connectors are never evicted from the cache — at PoC scale the connection
    set is small and static, coming entirely from settings.
    """

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings
        self._cache: dict[str, MCPConnectorBase] = {}

    def get(self, connection_id: str) -> MCPConnectorBase:
        """
        Return the connector for *connection_id*, building it on first use.

        Raises:
            ValueError: If connection_id is not configured anywhere
                (MONGO_CONNECTIONS, or as the legacy MCP_DEFAULT_CONNECTION_ID
                fallback using MONGODB_URI/MONGODB_DATABASE).
        """
        cached = self._cache.get(connection_id)
        if cached is not None:
            return cached

        connector = self._build(connection_id)
        self._cache[connection_id] = connector
        return connector

    def _build(self, connection_id: str) -> MCPConnectorBase:
        if self._settings.MCP_CONNECTOR_TYPE == "hive":
            # Single shared cluster today — connection_id is not yet
            # distinguishing, but is still accepted for interface symmetry.
            return HiveConnector(settings=self._settings)

        uri, database = self._resolve_mongo_target(connection_id)
        logger.info(
            "ConnectorRegistry: opening MongoDB connector",
            connection_id=connection_id,
            database=database,
        )
        return MongoDBConnector(uri=uri, database=database)

    def _resolve_mongo_target(self, connection_id: str) -> tuple[str, str]:
        conn_cfg = self._settings.MONGO_CONNECTIONS.get(connection_id)
        if conn_cfg is not None:
            return conn_cfg["uri"], conn_cfg["database"]

        # Backward compat: a bare .env with no MONGO_CONNECTIONS configured
        # still works as long as the session uses MCP_DEFAULT_CONNECTION_ID.
        if connection_id == self._settings.MCP_DEFAULT_CONNECTION_ID:
            return self._settings.MONGODB_URI, self._settings.MONGODB_DATABASE

        configured = sorted(self._settings.MONGO_CONNECTIONS) or [
            f"(none — only default '{self._settings.MCP_DEFAULT_CONNECTION_ID}' is available)"
        ]
        raise ValueError(
            f"Unknown connection_id {connection_id!r}. Configured connections: {configured}"
        )

    async def close_all(self) -> None:
        """Close every cached connector — call on app shutdown."""
        for connection_id, connector in self._cache.items():
            try:
                await connector.close()
            except Exception as exc:
                logger.warning(
                    "ConnectorRegistry: error closing connector",
                    connection_id=connection_id,
                    error=str(exc),
                )
        self._cache.clear()
