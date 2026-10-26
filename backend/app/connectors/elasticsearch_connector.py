"""Elasticsearch evidence connector (official ``elasticsearch`` async client)."""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

from app.connectors.base import (
    ConnectorError,
    ConnectorHealth,
    Evidence,
    EvidenceSpec,
    MCPConnectorBase,
    QueryRejectedError,
)
from app.connectors.normalize import FieldMap, build_record
from app.connectors.registry import register_connector

logger = structlog.get_logger(__name__)

# Keys that let a query run code or reshape results; never allowed in a planner fragment.
_FORBIDDEN_KEYS = {"script", "script_score", "script_fields", "runtime_mappings", "scripted_metric", "function_score"}
_MAX_FRAGMENT_DEPTH = 8
_MAX_SCHEMA_FIELDS = 60
DEFAULT_EXCLUDED_LEVELS = ("debug", "info", "trace")


def _scan_fragment(node: Any, depth: int = 0) -> None:
    if depth > _MAX_FRAGMENT_DEPTH:
        raise QueryRejectedError("query fragment is nested too deeply")
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _FORBIDDEN_KEYS:
                raise QueryRejectedError(f"'{key}' is not allowed in an evidence query")
            _scan_fragment(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _scan_fragment(item, depth + 1)


def _flatten_mapping(properties: dict[str, Any], prefix: str = "") -> list[str]:
    fields: list[str] = []
    for name, spec in properties.items():
        path = f"{prefix}{name}"
        if "properties" in spec:
            fields.extend(_flatten_mapping(spec["properties"], f"{path}."))
        else:
            fields.append(f"{path}: {spec.get('type', 'object')}")
    return fields


@register_connector("elasticsearch")
class ElasticsearchConnector(MCPConnectorBase):
    def __init__(
        self,
        name: str,
        url: str,
        index: str,
        fields: FieldMap,
        username: str = "",
        password: str = "",
        api_key: str = "",
        verify_certs: bool = True,
        exclude_levels: tuple[str, ...] = DEFAULT_EXCLUDED_LEVELS,
    ) -> None:
        super().__init__(name)
        self._url = url
        self._index = index
        self._fields = fields
        self._auth = {"username": username, "password": password, "api_key": api_key}
        self._verify_certs = verify_certs
        # Routine levels carry no failure signal; excluding them server-side keeps large indices
        # inside the row cap. The correlator only scores error-level records anyway.
        self._exclude_levels = sorted({v for level in exclude_levels for v in (level.lower(), level.upper())})
        self._client: Any = None

    @classmethod
    def from_config(cls, name: str, settings: Any, options: dict[str, Any]) -> "ElasticsearchConnector":
        fields = FieldMap(
            timestamp=options.get("timestamp_field", "@timestamp"),
            message=options.get("message_field", "message"),
            level=options.get("level_field", "level"),
            service=options.get("service_field", "service"),
        )
        return cls(
            name=name,
            url=options.get("url", settings.ES_URL),
            index=options.get("index", settings.ES_INDEX),
            fields=fields,
            username=settings.ES_USERNAME,
            password=settings.ES_PASSWORD,
            api_key=settings.ES_API_KEY,
            verify_certs=settings.ES_VERIFY_CERTS,
            exclude_levels=tuple(options.get("exclude_levels", DEFAULT_EXCLUDED_LEVELS)),
        )

    @property
    def query_instructions(self) -> str:
        return (
            'Return an Elasticsearch query DSL clause as the "query" value, e.g. '
            '{"bool": {"filter": [{"match_phrase": {"service": "checkout-api"}}], '
            '"should": [{"match": {"message": "timeout"}}]}}. '
            "Only query/bool/match/match_phrase/term/terms/range/exists/wildcard clauses. "
            "Do NOT add a time range, sorting or size; they are added automatically."
        )

    async def connect(self) -> None:
        if self._client is not None:
            return
        try:
            from elasticsearch import AsyncElasticsearch  # noqa: PLC0415
        except ImportError as exc:
            raise ConnectorError("elasticsearch package is not installed") from exc

        kwargs: dict[str, Any] = {"hosts": [self._url], "verify_certs": self._verify_certs}
        if self._auth["api_key"]:
            kwargs["api_key"] = self._auth["api_key"]
        elif self._auth["username"]:
            kwargs["basic_auth"] = (self._auth["username"], self._auth["password"])
        self._client = AsyncElasticsearch(**kwargs)

    async def health(self) -> ConnectorHealth:
        started = time.monotonic()
        try:
            await self.connect()
            reachable = await self._client.ping()
            detail = "" if reachable else "ping returned false"
        except Exception as exc:
            reachable, detail = False, str(exc)
        return ConnectorHealth(
            name=self.name,
            source_type=self.source_type,
            healthy=bool(reachable),
            latency_ms=int((time.monotonic() - started) * 1000),
            detail=detail,
        )

    async def describe_schema(self) -> str:
        await self.connect()
        try:
            mappings = await self._client.indices.get_mapping(index=self._index)
        except Exception as exc:
            raise ConnectorError(f"Elasticsearch mapping lookup failed: {exc}") from exc

        fields: list[str] = []
        for index_mapping in mappings.body.values():
            fields.extend(_flatten_mapping(index_mapping.get("mappings", {}).get("properties", {})))
        unique = sorted(set(fields))[:_MAX_SCHEMA_FIELDS]
        return f"Elasticsearch index pattern '{self._index}' fields:\n" + "\n".join(f"- {f}" for f in unique)

    def default_query(self, service: str) -> dict[str, Any]:
        return {"bool": {"filter": [{"match_phrase": {self._fields.service: service}}]}}

    async def fetch_evidence(self, spec: EvidenceSpec) -> Evidence:
        await self.connect()
        fragment = spec.query if spec.query is not None else self.default_query(spec.service)
        if not isinstance(fragment, dict) or not fragment:
            raise QueryRejectedError("query fragment must be a non-empty object")
        _scan_fragment(fragment)

        time_filter = {
            "range": {
                self._fields.timestamp: {
                    "gte": spec.time_range.start.isoformat(),
                    "lte": spec.time_range.end.isoformat(),
                }
            }
        }
        query: dict[str, Any] = {"bool": {"filter": [fragment, time_filter]}}
        if self._exclude_levels:
            query["bool"]["must_not"] = [{"terms": {self._fields.level: self._exclude_levels}}]
        # newest first: if the row cap truncates, we keep the incident window and lose old baseline
        sort = [{self._fields.timestamp: {"order": "desc"}}]

        started = time.monotonic()
        try:
            response = await self._client.search(
                index=self._index, query=query, sort=sort, size=spec.limit, track_total_hits=True
            )
        except Exception as exc:
            raise ConnectorError(f"Elasticsearch search failed: {exc}") from exc

        hits = response["hits"]["hits"]
        records = [
            record
            for seq, hit in enumerate(hits)
            if (record := build_record(self.name, seq, hit["_source"], self._fields)) is not None
        ]
        total = response["hits"]["total"]["value"]
        return Evidence(
            source=self.name,
            source_type=self.source_type,
            time_range=spec.time_range,
            records=records,
            query_used=json.dumps({"index": self._index, "query": query, "sort": sort, "size": spec.limit}),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            truncated=total > len(hits),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
