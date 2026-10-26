"""
Abstract MCP connector interface and shared data types.

Every warehouse connector must subclass MCPConnectorBase and implement
exactly three methods: discover_schema, execute_query, and test_connection.

The rest of the application interacts exclusively through this interface,
keeping the core orchestration layer source-agnostic. Swapping the warehouse
requires only a new connector file and a configuration change — no core changes.

Custom exceptions:
  QueryTimeoutError    — execute_query exceeded timeout_seconds
  QueryExecutionError  — warehouse-side failure during execution
  SchemaDiscoveryError — discover_schema could not introspect the warehouse
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models.schemas import QueryPlan, SchemaContext


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class UserIdentity:
    """
    Authenticated user identity forwarded to the warehouse.

    The connector uses this to impersonate the user so that warehouse-level
    RBAC/RLS applies to every query. The ``raw_jwt`` is available for
    connectors that accept Bearer tokens for passthrough auth.
    """

    user_id: str
    username: str
    roles: list[str] = field(default_factory=list)
    raw_jwt: str = ""


@dataclass
class QueryResult:
    """Result set returned by execute_query."""

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    execution_time_ms: int


# ─────────────────────────────────────────────────────────────────────────────
# Custom exceptions
# ─────────────────────────────────────────────────────────────────────────────


class QueryTimeoutError(Exception):
    """Raised when execute_query exceeds the configured timeout."""


class QueryExecutionError(Exception):
    """Raised on a warehouse-side failure during query execution."""


class SchemaDiscoveryError(Exception):
    """Raised when discover_schema cannot introspect the warehouse."""


# ─────────────────────────────────────────────────────────────────────────────
# Abstract connector base
# ─────────────────────────────────────────────────────────────────────────────


class MCPConnectorBase(ABC):
    """
    Abstract interface that all warehouse MCP connectors must implement.

    Design contract:
      - discover_schema: introspects warehouse metadata; must respect user RBAC
      - execute_query:   executes a read-only query; must forward user identity
      - test_connection: lightweight liveness check; swallows errors
      - close:           releases all resources (called on app shutdown)

    To add a new connector (e.g. Snowflake):
      1. Create backend/app/mcp/snowflake_connector.py
      2. class SnowflakeConnector(MCPConnectorBase)
      3. Implement all four methods below
      4. Add "snowflake" to ConnectorType in config.py
      5. Add branch in ConnectorRegistry._build() in app/mcp/registry.py
    """

    @abstractmethod
    async def discover_schema(
        self,
        connection_id: str,
        user_identity: UserIdentity,
    ) -> "SchemaContext":
        """
        Introspect the warehouse and return its full schema.

        Args:
            connection_id: Logical connection name (matches config).
            user_identity: Used to respect warehouse RBAC during discovery.

        Returns:
            SchemaContext with all tables and columns visible to the user.

        Raises:
            SchemaDiscoveryError: If introspection fails.

        Contract:
            - Must complete within 10 seconds.
            - Must not return tables inaccessible to user_identity.
            - Is idempotent and safe to call repeatedly.
        """

    @abstractmethod
    async def execute_query(
        self,
        query: "QueryPlan",
        user_identity: UserIdentity,
        timeout_seconds: int = 25,
    ) -> QueryResult:
        """
        Execute a read-only query and return results.

        Args:
            query:           QueryPlan with the SQL/HQL string.
            user_identity:   Forwarded to the warehouse for impersonation.
            timeout_seconds: Hard limit; raises QueryTimeoutError if exceeded.

        Returns:
            QueryResult with column names and row dicts.

        Raises:
            QueryTimeoutError:   If execution exceeds timeout_seconds.
            QueryExecutionError: On warehouse-side failures.

        Contract:
            - Must forward user_identity for RBAC/RLS.
            - Must NOT cache results — every call hits the warehouse.
            - Must only execute SELECT queries (validated upstream by QueryPlanner).
        """

    @abstractmethod
    async def test_connection(
        self,
        connection_id: str,
        user_identity: UserIdentity,
    ) -> bool:
        """
        Lightweight liveness check.

        Returns:
            True if the user can connect; False otherwise.
            Must not raise exceptions — swallow errors and return False.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release all open connections and async resources."""
