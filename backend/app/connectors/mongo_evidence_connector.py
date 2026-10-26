"""MongoDB evidence connector: wraps the existing motor-based MongoDBConnector."""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

from app.connectors.base import (
    ConnectorConfigError,
    ConnectorError,
    ConnectorHealth,
    Evidence,
    EvidenceSpec,
    MCPConnectorBase,
    QueryRejectedError,
)
from app.connectors.normalize import FieldMap, build_record
from app.connectors.registry import register_connector
from app.mcp.base import QueryExecutionError, QueryTimeoutError, SchemaDiscoveryError, UserIdentity
from app.mcp.mongo_connector import MongoDBConnector

logger = structlog.get_logger(__name__)

# Operators that execute server-side JavaScript or arbitrary expressions.
_FORBIDDEN_OPERATORS = {"$where", "$function", "$accumulator", "$expr", "$jsonSchema"}
_SERVICE_IDENTITY = UserIdentity(user_id="rca-copilot", username="rca-copilot")


def _scan_match(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _FORBIDDEN_OPERATORS:
                raise QueryRejectedError(f"operator '{key}' is not allowed in an evidence query")
            _scan_match(value)
    elif isinstance(node, list):
        for item in node:
            _scan_match(item)


@register_connector("mongodb")
class MongoEvidenceConnector(MCPConnectorBase):
    def __init__(
        self,
        name: str,
        uri: str,
        database: str,
        collections: list[str],
        fields: FieldMap,
    ) -> None:
        super().__init__(name)
        if not collections:
            raise ConnectorConfigError("mongodb connector needs options.collection or options.collections")
        self._uri = uri
        self._database = database
        self._collections = collections
        self._fields = fields
        self._warehouse: MongoDBConnector | None = None

    @classmethod
    def from_config(cls, name: str, settings: Any, options: dict[str, Any]) -> "MongoEvidenceConnector":
        named = settings.MONGO_CONNECTIONS.get(options.get("connection_id", ""), {})
        collections = options.get("collections") or ([options["collection"]] if "collection" in options else [])
        return cls(
            name=name,
            uri=options.get("uri") or named.get("uri") or settings.MONGODB_URI,
            database=options.get("database") or named.get("database") or settings.MONGODB_DATABASE,
            collections=collections,
            fields=FieldMap(
                timestamp=options.get("timestamp_field", "timestamp"),
                message=options.get("message_field", "message"),
                level=options.get("level_field", "level"),
                service=options.get("service_field", "service"),
            ),
        )

    @property
    def query_instructions(self) -> str:
        return (
            'Return {"collection": "<one of: ' + ", ".join(self._collections) + '>", '
            '"match": <MongoDB $match filter object>}, e.g. '
            '{"collection": "deploy_events", "match": {"service": "checkout-api"}}. '
            "Do NOT add a time range, sorting or limit; they are added automatically."
        )

    async def connect(self) -> None:
        if self._warehouse is None:
            self._warehouse = MongoDBConnector(uri=self._uri, database=self._database)

    async def health(self) -> ConnectorHealth:
        started = time.monotonic()
        try:
            await self.connect()
            assert self._warehouse is not None
            healthy = await self._warehouse.test_connection(self._database, _SERVICE_IDENTITY)
            detail = "" if healthy else "ping failed"
        except Exception as exc:
            healthy, detail = False, str(exc)
        return ConnectorHealth(
            name=self.name,
            source_type=self.source_type,
            healthy=healthy,
            latency_ms=int((time.monotonic() - started) * 1000),
            detail=detail,
        )

    async def describe_schema(self) -> str:
        await self.connect()
        assert self._warehouse is not None
        try:
            schema = await self._warehouse.discover_schema(self._database, _SERVICE_IDENTITY)
        except SchemaDiscoveryError as exc:
            raise ConnectorError(str(exc)) from exc

        lines = [f"MongoDB database '{self._database}':"]
        for table in schema.tables.values():
            if table.name in self._collections:
                columns = ", ".join(f"{c.name} ({c.data_type})" for c in table.columns)
                lines.append(f"- collection {table.name}: {columns}")
        return "\n".join(lines)

    def default_query(self, service: str) -> dict[str, Any]:
        return {"collection": self._collections[0], "match": {self._fields.service: service}}

    def build_pipeline(self, spec: EvidenceSpec) -> tuple[str, list[dict[str, Any]]]:
        """Validate the fragment and return (collection, bounded pipeline). Pure, unit-tested."""
        fragment = spec.query if spec.query is not None else self.default_query(spec.service)
        collection = fragment.get("collection")
        match = fragment.get("match", {})
        if collection not in self._collections:
            raise QueryRejectedError(f"collection {collection!r} is not one of {self._collections}")
        if not isinstance(match, dict):
            raise QueryRejectedError("'match' must be an object")
        _scan_match(match)

        time_filter = {self._fields.timestamp: {"$gte": spec.time_range.start, "$lte": spec.time_range.end}}
        pipeline = [
            {"$match": {"$and": [match, time_filter]}},
            {"$sort": {self._fields.timestamp: -1}},  # newest first, so truncation drops old baseline
            # one extra row lets us detect truncation without a second count query
            {"$limit": spec.limit + 1},
        ]
        return collection, pipeline

    async def fetch_evidence(self, spec: EvidenceSpec) -> Evidence:
        await self.connect()
        assert self._warehouse is not None
        collection, pipeline = self.build_pipeline(spec)

        started = time.monotonic()
        try:
            result = await self._warehouse.aggregate(collection, pipeline)
        except (QueryExecutionError, QueryTimeoutError) as exc:
            raise ConnectorError(str(exc)) from exc

        truncated = result.row_count > spec.limit
        records = [
            record
            for seq, row in enumerate(result.rows[: spec.limit])
            if (record := build_record(self.name, seq, row, self._fields)) is not None
        ]
        return Evidence(
            source=self.name,
            source_type=self.source_type,
            time_range=spec.time_range,
            records=records,
            query_used=json.dumps({"collection": collection, "pipeline": pipeline}, default=str),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            truncated=truncated,
        )

    async def close(self) -> None:
        if self._warehouse is not None:
            await self._warehouse.close()
            self._warehouse = None
