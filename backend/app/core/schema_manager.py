"""
Schema context manager — discovers and caches warehouse schema via MCP.

Maintains a per-connection schema map sourced from the MCP connector.
Cache is stored in Redis under ``schema:{connection_id}`` with a configurable
TTL (default 5 minutes). A cache hit avoids an expensive MCP round-trip for
every query in the same time window.

Cache invalidation:
  - Call get_schema(force_refresh=True) to bypass the cache.
  - TTL expiry triggers automatic re-discovery on the next request.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from app.models.schemas import ColumnMeta, SchemaContext, TableMeta

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.config import Settings
    from app.mcp.base import UserIdentity
    from app.mcp.registry import ConnectorRegistry

logger = structlog.get_logger(__name__)

SCHEMA_CACHE_KEY_PATTERN: str = "schema:{connection_id}"


class SchemaManager:
    """
    Fetches warehouse schema via the MCP connector and caches it in Redis.

    The cache uses a fixed TTL (not sliding) because schema changes are rare
    and a brief stale window is acceptable at PoC scale.
    """

    def __init__(
        self,
        connector_registry: "ConnectorRegistry",
        redis_client: "Redis",
        cache_ttl_seconds: int = 300,
    ) -> None:
        self._registry = connector_registry
        self._redis = redis_client
        self._cache_ttl = cache_ttl_seconds

    async def get_schema(
        self,
        connection_id: str,
        user_identity: "UserIdentity",
        force_refresh: bool = False,
    ) -> SchemaContext:
        """
        Return the current schema for *connection_id*.

        Checks Redis cache first unless force_refresh=True. Falls back to
        MCP connector on cache miss and saves the result to Redis.

        Args:
            connection_id:  Logical name of the warehouse connection.
            user_identity:  User context forwarded to the MCP connector.
            force_refresh:  Bypass cache and call the MCP connector directly.

        Returns:
            SchemaContext with all tables/columns visible to the user.

        Raises:
            SchemaDiscoveryError: If MCP connector fails and no cache is available.
        """
        # TODO: implement
        if not force_refresh:
            cached = await self._load_from_cache(connection_id)
            if cached is not None:
                logger.debug("Schema cache hit", connection_id=connection_id)
                return cached

        logger.info("Fetching schema from MCP", connection_id=connection_id)
        schema = await self._fetch_from_mcp(connection_id, user_identity)
        await self._save_to_cache(connection_id, schema)
        return schema

    async def _fetch_from_mcp(
        self,
        connection_id: str,
        user_identity: "UserIdentity",
    ) -> SchemaContext:
        """Resolve the connector for connection_id and discover its current schema."""
        connector = self._registry.get(connection_id)
        return await connector.discover_schema(connection_id, user_identity)

    async def _load_from_cache(self, connection_id: str) -> SchemaContext | None:
        """
        Load and deserialise a cached SchemaContext from Redis.

        Returns:
            Deserialised SchemaContext or None on cache miss / parse error.
        """
        key = SCHEMA_CACHE_KEY_PATTERN.format(connection_id=connection_id)
        raw = await self._redis.get(key)
        if raw is None:
            return None
        try:
            return self._deserialise_schema(raw)
        except Exception as exc:
            logger.warning("Schema cache deserialise failed", error=str(exc))
            return None

    async def _save_to_cache(
        self, connection_id: str, schema: SchemaContext
    ) -> None:
        """Serialise and store a SchemaContext in Redis with the configured TTL."""
        key = SCHEMA_CACHE_KEY_PATTERN.format(connection_id=connection_id)
        payload = self._serialise_schema(schema)
        await self._redis.set(key, payload, ex=self._cache_ttl)

    def _serialise_schema(self, schema: SchemaContext) -> str:
        """Convert SchemaContext to a JSON string for Redis storage."""
        import json

        tables_data = {}
        for table_key, table in schema.tables.items():
            tables_data[table_key] = {
                "name": table.name,
                "database": table.database,
                "description": table.description,
                "row_count_estimate": table.row_count_estimate,
                "columns": [
                    {
                        "name": c.name,
                        "data_type": c.data_type,
                        "nullable": c.nullable,
                        "description": c.description,
                        "sample_values": c.sample_values,
                        "null_pct": c.null_pct,
                    }
                    for c in table.columns
                ],
            }
        return json.dumps(
            {
                "tables": tables_data,
                "fetched_at": schema.fetched_at.isoformat(),
                "connector_type": schema.connector_type,
            }
        )

    def _deserialise_schema(self, raw: str | bytes) -> SchemaContext:
        """Reconstruct a SchemaContext from a JSON string retrieved from Redis."""
        import json

        data = json.loads(raw)
        tables: dict[str, TableMeta] = {}
        for table_key, t in data["tables"].items():
            columns = [
                ColumnMeta(
                    name=c["name"],
                    data_type=c["data_type"],
                    nullable=c["nullable"],
                    description=c.get("description"),
                    sample_values=c.get("sample_values", []),
                    null_pct=c.get("null_pct", 0.0),
                )
                for c in t.get("columns", [])
            ]
            tables[table_key] = TableMeta(
                name=t["name"],
                database=t["database"],
                columns=columns,
                description=t.get("description"),
                row_count_estimate=t.get("row_count_estimate"),
            )
        return SchemaContext(
            tables=tables,
            fetched_at=datetime.fromisoformat(data["fetched_at"]),
            connector_type=data["connector_type"],
        )
