"""
Hive evidence connector.

The pre-existing ``app.mcp.hive_connector.HiveConnector`` is a set of
NotImplementedError stubs, so this connector talks to HiveServer2 directly via
PyHive (optional dependency, imported lazily). PyHive is synchronous, so every
call runs in a worker thread.

NOT verified against a live Hive cluster in this repo's test environment; the
unit tests exercise query building and validation only.
"""

from __future__ import annotations

import asyncio
import json
import re
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

logger = structlog.get_logger(__name__)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# PyHive auth names differ from the HIVE_AUTH_MECHANISM setting values
_PYHIVE_AUTH = {"PLAIN": "NONE", "LDAP": "LDAP", "KERBEROS": "KERBEROS"}


def _identifier(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.match(value):
        raise QueryRejectedError(f"{what} {value!r} is not a plain identifier")
    return value


@register_connector("hive")
class HiveEvidenceConnector(MCPConnectorBase):
    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        database: str,
        tables: list[str],
        fields: FieldMap,
        username: str = "",
        password: str = "",
        auth: str = "NONE",
    ) -> None:
        super().__init__(name)
        if not tables:
            raise ConnectorConfigError("hive connector needs options.table or options.tables")
        self._conn_args = {"host": host, "port": port, "database": database}
        self._credentials = {"username": username or None, "password": password or None, "auth": auth}
        self._database = _identifier(database, "database")
        self._tables = [_identifier(t, "table") for t in tables]
        self._fields = fields

    @classmethod
    def from_config(cls, name: str, settings: Any, options: dict[str, Any]) -> "HiveEvidenceConnector":
        tables = options.get("tables") or ([options["table"]] if "table" in options else [])
        return cls(
            name=name,
            host=options.get("host", settings.HIVE_HOST),
            port=int(options.get("port", settings.HIVE_PORT)),
            database=options.get("database", settings.HIVE_DATABASE),
            tables=tables,
            fields=FieldMap(
                timestamp=options.get("timestamp_field", "ts"),
                message=options.get("message_field", "message"),
                level=options.get("level_field", "level"),
                service=options.get("service_field", "service"),
            ),
            username=settings.HIVE_USERNAME,
            password=settings.HIVE_PASSWORD,
            auth=_PYHIVE_AUTH[settings.HIVE_AUTH_MECHANISM],
        )

    @property
    def query_instructions(self) -> str:
        return (
            'Return {"table": "<one of: ' + ", ".join(self._tables) + '>", '
            '"filters": {"<column>": "<exact value>"}}, e.g. '
            '{"table": "service_metrics", "filters": {"service": "checkout-api"}}. '
            "Only equality filters on existing columns. Do NOT add a time range or limit."
        )

    # ── PyHive plumbing (sync, run in threads) ───────────────────────────────

    def _run_sync(self, sql: str, params: dict[str, Any]) -> tuple[list[str], list[tuple]]:
        try:
            from pyhive import hive  # noqa: PLC0415
        except ImportError as exc:
            raise ConnectorConfigError("pyhive is not installed (see requirements.txt)") from exc

        connection = hive.Connection(**self._conn_args, **self._credentials)
        try:
            cursor = connection.cursor()
            cursor.execute(sql, params)
            # Hive prefixes result columns with "table."
            columns = [d[0].split(".")[-1] for d in cursor.description or []]
            return columns, cursor.fetchall()
        finally:
            connection.close()

    async def _run(self, sql: str, params: dict[str, Any] | None = None) -> tuple[list[str], list[tuple]]:
        return await asyncio.to_thread(self._run_sync, sql, params or {})

    async def connect(self) -> None:
        # Connections are opened per query; HiveServer2 sessions are cheap next to RCA queries.
        return None

    async def health(self) -> ConnectorHealth:
        started = time.monotonic()
        try:
            await self._run("SELECT 1")
            healthy, detail = True, ""
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
        lines = [f"Hive database '{self._database}':"]
        for table in self._tables:
            try:
                _, rows = await self._run(f"DESCRIBE {self._database}.{table}")
            except ConnectorConfigError:
                raise
            except Exception as exc:
                raise ConnectorError(f"Hive DESCRIBE {table} failed: {exc}") from exc
            columns = ", ".join(f"{row[0]} ({row[1]})" for row in rows if row[0] and not row[0].startswith("#"))
            lines.append(f"- table {table}: {columns}")
        return "\n".join(lines)

    def default_query(self, service: str) -> dict[str, Any]:
        return {"table": self._tables[0], "filters": {self._fields.service: service}}

    def build_sql(self, spec: EvidenceSpec) -> tuple[str, dict[str, Any]]:
        """Return (parameterised HiveQL, params). Pure function, unit-tested."""
        fragment = spec.query if spec.query is not None else self.default_query(spec.service)
        table = fragment.get("table")
        filters = fragment.get("filters", {})
        if table not in self._tables:
            raise QueryRejectedError(f"table {table!r} is not one of {self._tables}")
        if not isinstance(filters, dict):
            raise QueryRejectedError("'filters' must be an object")

        params: dict[str, Any] = {
            "t_start": spec.time_range.start.strftime("%Y-%m-%d %H:%M:%S"),
            "t_end": spec.time_range.end.strftime("%Y-%m-%d %H:%M:%S"),
        }
        ts = _identifier(self._fields.timestamp, "timestamp column")
        clauses = [f"{ts} >= %(t_start)s", f"{ts} <= %(t_end)s"]
        for i, (column, value) in enumerate(filters.items()):
            if not isinstance(value, (str, int, float, bool)):
                raise QueryRejectedError(f"filter value for {column!r} must be a scalar")
            clauses.append(f"{_identifier(column, 'column')} = %(f{i})s")
            params[f"f{i}"] = value

        # limit+1 lets fetch_evidence detect truncation
        sql = (
            f"SELECT * FROM {self._database}.{table} WHERE {' AND '.join(clauses)} "
            f"ORDER BY {ts} DESC LIMIT {int(spec.limit) + 1}"
        )
        return sql, params

    async def fetch_evidence(self, spec: EvidenceSpec) -> Evidence:
        sql, params = self.build_sql(spec)
        started = time.monotonic()
        try:
            columns, rows = await self._run(sql, params)
        except ConnectorConfigError:
            raise
        except Exception as exc:
            raise ConnectorError(f"Hive query failed: {exc}") from exc

        truncated = len(rows) > spec.limit
        raw_rows = [dict(zip(columns, row)) for row in rows[: spec.limit]]
        records = [
            record
            for seq, raw in enumerate(raw_rows)
            if (record := build_record(self.name, seq, raw, self._fields)) is not None
        ]
        return Evidence(
            source=self.name,
            source_type=self.source_type,
            time_range=spec.time_range,
            records=records,
            query_used=json.dumps({"sql": sql, "params": params}),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            truncated=truncated,
        )

    async def close(self) -> None:
        return None
