"""
Evidence-source connector contract for the RCA Copilot.

Every source (Elasticsearch, MongoDB, Hive, ...) implements MCPConnectorBase and
returns its data as the same typed ``Evidence`` model, so the collector,
correlator and ranker never see source-specific shapes.

This is separate from ``app.mcp.base.MCPConnectorBase`` (the NL-to-SQL warehouse
interface): that one answers analyst questions, this one fetches time-bounded
operational evidence for an incident.

Planning split:
  The LLM only produces a small *query fragment* (``spec.query``) whose shape is
  documented by each connector's ``query_instructions``. The connector itself
  adds the time range, ordering and row limit, so a model mistake can never
  produce an unbounded or write query.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class ConnectorError(Exception):
    """Base class for connector failures that the collector reports as partial results."""


class ConnectorConfigError(ConnectorError):
    """Connector is misconfigured (missing option, missing optional dependency)."""


class QueryRejectedError(ConnectorError):
    """A planner-produced query fragment failed the connector's safety validation."""


def to_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime; naive values are assumed to already be UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class TimeRange(BaseModel):
    start: datetime
    end: datetime


class EvidenceSpec(BaseModel):
    """What to fetch: the incident scope plus the planner's native query fragment."""

    service: str
    time_range: TimeRange
    symptom: str = ""
    query: dict[str, Any] | None = Field(
        default=None,
        description="Connector-native fragment from the planner; None means use the connector default.",
    )
    limit: int = 500


class EvidenceRecord(BaseModel):
    """One normalised event/log/metric row. ``id`` is globally unique across sources."""

    id: str
    timestamp: datetime
    source: str
    service: str | None = None
    level: str | None = None
    message: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    """Everything one connector returned for one spec."""

    source: str
    source_type: str
    time_range: TimeRange
    records: list[EvidenceRecord] = Field(default_factory=list)
    query_used: str
    elapsed_ms: int = 0
    truncated: bool = False


class ConnectorHealth(BaseModel):
    name: str
    source_type: str
    healthy: bool
    latency_ms: int | None = None
    detail: str = ""


class MCPConnectorBase(ABC):
    """
    Contract for an RCA evidence source.

    To add a source: subclass this, decorate with
    ``@register_connector("<type>")`` (see registry.py), implement ``from_config``
    and drop the module into ``app/connectors/``. No core code changes.
    """

    #: registry key, set by @register_connector
    source_type: str = "unknown"

    def __init__(self, name: str, timeout_seconds: float | None = None) -> None:
        self.name = name
        #: per-connector override of RCA_CONNECTOR_TIMEOUT_SECONDS
        self.timeout_seconds = timeout_seconds

    @classmethod
    @abstractmethod
    def from_config(cls, name: str, settings: Any, options: dict[str, Any]) -> "MCPConnectorBase":
        """Build an instance from app settings plus the per-connector ``options`` dict."""

    @property
    @abstractmethod
    def query_instructions(self) -> str:
        """Prompt text telling the planner the exact JSON shape of ``spec.query``."""

    @abstractmethod
    async def connect(self) -> None:
        """Open client resources. Safe to call repeatedly."""

    @abstractmethod
    async def health(self) -> ConnectorHealth:
        """Liveness check. Must not raise; report failure in the returned model."""

    @abstractmethod
    async def describe_schema(self) -> str:
        """Compact, prompt-ready description of the fields/columns the planner may use."""

    @abstractmethod
    def default_query(self, service: str) -> dict[str, Any]:
        """Deterministic fragment used when the planner is unavailable or its output is rejected."""

    @abstractmethod
    async def fetch_evidence(self, spec: EvidenceSpec) -> Evidence:
        """
        Run the (validated) query for ``spec`` and return normalised records.

        Raises:
            QueryRejectedError: spec.query failed validation.
            ConnectorError: any source-side failure.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release client resources."""
