"""Builders shared by the RCA tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.connectors.base import (
    ConnectorHealth,
    Evidence,
    EvidenceRecord,
    EvidenceSpec,
    MCPConnectorBase,
    TimeRange,
)
from app.rca.models import Incident

T0 = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)  # incident start in all tests


def make_incident(minutes: int = 20, connectors: list[str] | None = None) -> Incident:
    return Incident(
        service="checkout-api",
        start=T0,
        end=T0 + timedelta(minutes=minutes),
        symptom="5xx errors",
        connectors=connectors,
    )


def record(source: str, seq: int, offset_min: float, message: str = "ok", level: str | None = "info", **attrs: Any) -> EvidenceRecord:
    return EvidenceRecord(
        id=f"{source}#{seq}",
        timestamp=T0 + timedelta(minutes=offset_min),
        source=source,
        service="checkout-api",
        level=level,
        message=message,
        attributes=attrs,
    )


def evidence(source: str, records: list[EvidenceRecord], source_type: str = "elasticsearch") -> Evidence:
    return Evidence(
        source=source,
        source_type=source_type,
        time_range=TimeRange(start=T0 - timedelta(minutes=60), end=T0 + timedelta(minutes=20)),
        records=records,
        query_used="{}",
    )


class FakeConnector(MCPConnectorBase):
    """Configurable in-memory connector for collector tests."""

    source_type = "fake"

    def __init__(self, name: str, delay: float = 0.0, error: Exception | None = None, records: list[EvidenceRecord] | None = None):
        super().__init__(name)
        self.delay = delay
        self.error = error
        self.records = records or []
        self.last_spec: EvidenceSpec | None = None
        self.specs: list[EvidenceSpec] = []

    @classmethod
    def from_config(cls, name, settings, options):
        return cls(name)

    @property
    def query_instructions(self) -> str:
        return 'Return {"query": {"term": "value"}}'

    async def connect(self) -> None:
        return None

    async def health(self) -> ConnectorHealth:
        return ConnectorHealth(name=self.name, source_type=self.source_type, healthy=self.error is None)

    async def describe_schema(self) -> str:
        return "fake schema"

    def default_query(self, service: str) -> dict:
        return {"default": service}

    async def fetch_evidence(self, spec: EvidenceSpec) -> Evidence:
        import asyncio

        self.last_spec = spec
        self.specs.append(spec)
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return Evidence(
            source=self.name,
            source_type=self.source_type,
            time_range=spec.time_range,
            records=self.records,
            query_used="fake",
        )

    async def close(self) -> None:
        return None
