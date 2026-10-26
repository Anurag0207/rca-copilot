"""
MongoDB MCP connector.

Implements MCPConnectorBase using `motor` (async pymongo wrapper).

Schema discovery:
  - Lists all collections in the configured database
  - Samples up to SAMPLE_SIZE documents per collection to infer field types
  - Maps to SchemaContext / TableMeta / ColumnMeta (same contract as HiveConnector)

Query execution:
  - `query.query_string` must be a JSON string:
      {"collection": "<name>", "pipeline": [...]}
  - Aggregation pipeline is passed directly to motor's aggregate()
  - Result rows have ObjectId/_id serialised to strings
  - $out and $merge stages are blocked as write-protection
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from app.mcp.base import (
    MCPConnectorBase,
    QueryExecutionError,
    QueryResult,
    QueryTimeoutError,
    SchemaDiscoveryError,
    UserIdentity,
)

if TYPE_CHECKING:
    from app.models.schemas import ColumnMeta, QueryPlan, SchemaContext, TableMeta

logger = structlog.get_logger(__name__)

# Number of documents to sample per collection for type inference
_SAMPLE_SIZE = 100

# Aggregation stages that write data — always blocked
_WRITE_STAGES = {"$out", "$merge"}


def _serialize_value(v: Any) -> Any:
    """
    Convert a MongoDB document value to a JSON-safe scalar for sample storage.

    Converts datetime → ISO string, keeps str/int/float/bool as-is.
    Nested dicts and lists are stringified to avoid blowing up the schema cache.
    """
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (str, int, float, bool)):
        return v
    # Nested structures — represent as truncated string so the model sees the shape
    s = str(v)
    return s[:60] + ("…" if len(s) > 60 else "")


class MongoDBConnector(MCPConnectorBase):
    """
    MongoDB connector using the motor async driver.

    One motor client is shared per connector instance. The client manages its
    own connection pool internally; no per-query connection overhead.
    """

    def __init__(self, uri: str, database: str) -> None:
        # Lazy-import so motor is not required when using other connectors
        import motor.motor_asyncio as _motor  # noqa: PLC0415

        self._client = _motor.AsyncIOMotorClient(
            uri,
            serverSelectionTimeoutMS=5000,
        )
        self._db_name = database

    # ─────────────────────────────────────────────────────────────────────────
    # Schema discovery
    # ─────────────────────────────────────────────────────────────────────────

    async def discover_schema(
        self,
        connection_id: str,
        user_identity: UserIdentity,
    ) -> "SchemaContext":
        """
        Introspect all collections in the configured database.

        Samples up to _SAMPLE_SIZE documents per collection to infer field
        types. Returns a SchemaContext keyed by '{database}.{collection}'.
        """
        from app.models.schemas import ColumnMeta, SchemaContext, TableMeta  # noqa: PLC0415

        try:
            db = self._client[self._db_name]
            collection_names: list[str] = await db.list_collection_names()

            tables: dict[str, TableMeta] = {}
            for col_name in sorted(collection_names):
                columns = await self._infer_columns(db, col_name)
                table = TableMeta(
                    name=col_name,
                    database=self._db_name,
                    columns=columns,
                )
                tables[table.qualified_name] = table

            return SchemaContext(
                tables=tables,
                fetched_at=datetime.utcnow(),
                connector_type="mongodb",
            )
        except Exception as exc:
            raise SchemaDiscoveryError(f"MongoDB schema discovery failed: {exc}") from exc

    async def _infer_columns(self, db: Any, collection_name: str) -> list["ColumnMeta"]:
        """
        Sample documents and infer field types, sample values, and null rates.

        For each field we collect:
          - data_type: inferred from Python/BSON type of observed values
          - sample_values: up to 5 distinct non-null values (for prompt injection)
          - null_pct: fraction of sampled docs where the field is null/missing
        """
        from app.models.schemas import ColumnMeta  # noqa: PLC0415

        cursor = db[collection_name].find({}, limit=_SAMPLE_SIZE)

        field_types: dict[str, set[str]] = {}
        field_samples: dict[str, list] = {}    # distinct non-null values, max 5
        field_null_count: dict[str, int] = {}
        total_docs = 0

        async for doc in cursor:
            total_docs += 1
            for key, value in doc.items():
                type_str = _python_type_to_string(value)
                field_types.setdefault(key, set()).add(type_str)

                if value is None:
                    field_null_count[key] = field_null_count.get(key, 0) + 1
                else:
                    samples = field_samples.setdefault(key, [])
                    # keep up to 5 distinct values; skip ObjectIds (not useful in prompts)
                    try:
                        from bson import ObjectId  # noqa: PLC0415
                        if isinstance(value, ObjectId):
                            continue
                    except ImportError:
                        pass
                    serialised = _serialize_value(value)
                    if len(samples) < 5 and serialised not in samples:
                        samples.append(serialised)

        columns: list[ColumnMeta] = []
        for field_name, types in field_types.items():
            data_type = next(iter(types)) if len(types) == 1 else "mixed"
            null_count = field_null_count.get(field_name, 0)
            null_pct = null_count / total_docs if total_docs > 0 else 0.0
            columns.append(
                ColumnMeta(
                    name=field_name,
                    data_type=data_type,
                    nullable=null_pct > 0,
                    sample_values=field_samples.get(field_name, []),
                    null_pct=round(null_pct, 3),
                )
            )
        return columns

    # ─────────────────────────────────────────────────────────────────────────
    # Query execution
    # ─────────────────────────────────────────────────────────────────────────

    async def execute_query(
        self,
        query: "QueryPlan",
        user_identity: UserIdentity,
        timeout_seconds: int = 25,
    ) -> QueryResult:
        """
        Execute a MongoDB aggregation pipeline.

        query.query_string must be JSON:
            {"collection": "<name>", "pipeline": [...]}

        Raises:
            QueryTimeoutError:   If execution exceeds timeout_seconds.
            QueryExecutionError: On parse failure or MongoDB error.
        """
        try:
            payload: dict[str, Any] = json.loads(query.query_string)
        except (json.JSONDecodeError, TypeError) as exc:
            raise QueryExecutionError(
                f"query_string is not valid JSON: {exc}\nGot: {query.query_string[:200]}"
            ) from exc

        collection_name: str = payload.get("collection", "")
        pipeline: list[dict] = payload.get("pipeline", [])

        if not collection_name:
            raise QueryExecutionError("query_string missing 'collection' key")

        # Strip "database." prefix if the LLM included it (e.g. "ecommerce_db.olist_orders_dataset")
        if "." in collection_name:
            collection_name = collection_name.split(".", 1)[1]

        self._block_write_stages(pipeline)

        pipeline_str = json.dumps(pipeline)
        logger.info(
            "MongoDB query executing",
            collection=collection_name,
            pipeline=pipeline_str[:600],
            stage_count=len(pipeline),
        )

        try:
            result = await asyncio.wait_for(
                self._run_aggregate(collection_name, pipeline),
                timeout=timeout_seconds,
            )
            logger.info(
                "MongoDB query complete",
                collection=collection_name,
                rows_returned=result.row_count,
                columns=result.columns,
                execution_ms=result.execution_time_ms,
                first_row=result.rows[0] if result.rows else None,
            )
            return result
        except asyncio.TimeoutError:
            logger.error("MongoDB query timed out", collection=collection_name, timeout_seconds=timeout_seconds)
            raise QueryTimeoutError(
                f"MongoDB aggregation exceeded {timeout_seconds}s timeout"
            )
        except QueryExecutionError:
            raise
        except Exception as exc:
            logger.error("MongoDB query failed", collection=collection_name, error=str(exc))
            raise QueryExecutionError(f"MongoDB aggregation error: {exc}") from exc

    async def aggregate(
        self,
        collection_name: str,
        pipeline: list[dict],
        timeout_seconds: int = 25,
    ) -> QueryResult:
        """
        Run an already-parsed pipeline (may contain datetime values, which the
        JSON ``query_string`` path of execute_query cannot carry).

        Same read-only and timeout guarantees as execute_query.
        """
        self._block_write_stages(pipeline)
        try:
            return await asyncio.wait_for(
                self._run_aggregate(collection_name, pipeline),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise QueryTimeoutError(f"MongoDB aggregation exceeded {timeout_seconds}s timeout")
        except QueryExecutionError:
            raise
        except Exception as exc:
            raise QueryExecutionError(f"MongoDB aggregation error: {exc}") from exc

    async def _run_aggregate(
        self, collection_name: str, pipeline: list[dict]
    ) -> QueryResult:
        start = time.monotonic()
        db = self._client[self._db_name]
        cursor = db[collection_name].aggregate(pipeline)
        rows_raw: list[dict] = await cursor.to_list(length=10_000)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        rows = [_serialize_doc(doc) for doc in rows_raw]
        columns = list(rows[0].keys()) if rows else []

        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            execution_time_ms=elapsed_ms,
        )

    def _block_write_stages(self, pipeline: list[dict]) -> None:
        """Raise QueryExecutionError if any stage writes data."""
        for stage in pipeline:
            for key in stage:
                if key in _WRITE_STAGES:
                    raise QueryExecutionError(
                        f"Write stage '{key}' is not allowed in read-only queries"
                    )

    # ─────────────────────────────────────────────────────────────────────────
    # Liveness check
    # ─────────────────────────────────────────────────────────────────────────

    async def test_connection(
        self,
        connection_id: str,
        user_identity: UserIdentity,
    ) -> bool:
        try:
            await self._client.admin.command("ping")
            return True
        except Exception:
            logger.warning("MongoDBConnector.test_connection failed", connection_id=connection_id)
            return False

    async def close(self) -> None:
        self._client.close()
        logger.info("MongoDBConnector closed")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _python_type_to_string(value: Any) -> str:
    """Map a Python/BSON value to a readable type string for SchemaContext."""
    try:
        from bson import ObjectId  # motor ships bson  # noqa: PLC0415
        if isinstance(value, ObjectId):
            return "objectid"
    except ImportError:
        pass

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "mixed"


def _serialize_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Flatten a MongoDB document to JSON-serialisable Python types."""
    result: dict[str, Any] = {}
    for key, value in doc.items():
        result[key] = _serialize_value(value)
    return result


def _serialize_value(value: Any) -> Any:
    try:
        from bson import ObjectId  # noqa: PLC0415
        if isinstance(value, ObjectId):
            return str(value)
    except ImportError:
        pass

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize_value(v) for v in value]
    return value
