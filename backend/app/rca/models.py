"""Pydantic models shared by the RCA collector, correlator, ranker and API."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Evidence and its parts live with the connector contract; re-exported for RCA callers.
from app.connectors.base import Evidence, EvidenceRecord, TimeRange, to_utc

__all__ = [
    "ActivityBucket",
    "CorrelationReport",
    "ErrorSignature",
    "ErrorSpike",
    "Evidence",
    "EvidenceRecord",
    "EvidenceSummary",
    "Hypothesis",
    "Incident",
    "MetricAnomaly",
    "RCAResult",
    "TimelineEvent",
]


class Incident(BaseModel):
    service: str = Field(min_length=1)
    start: datetime
    end: datetime
    symptom: str = ""
    connectors: list[str] | None = Field(
        default=None, description="Subset of enabled connector names to use; None means all."
    )

    @model_validator(mode="after")
    def _check_window(self) -> "Incident":
        self.start, self.end = to_utc(self.start), to_utc(self.end)
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self

    def evidence_window(self, baseline_minutes: int) -> TimeRange:
        """Incident window extended backwards so the correlator has a baseline."""
        return TimeRange(start=self.start - timedelta(minutes=baseline_minutes), end=self.end)


# ── Correlator output ────────────────────────────────────────────────────────


class ErrorSpike(BaseModel):
    source: str
    onset: datetime
    peak_at: datetime
    peak_count: int
    baseline_mean: float
    baseline_std: float
    peak_zscore: float
    spike_buckets: int


class ErrorSignature(BaseModel):
    signature: str
    incident_count: int
    baseline_count: int
    novel: bool = Field(description="True when the signature never appears in the baseline window")
    first_seen: datetime
    sources: list[str]
    sample_record_ids: list[str]


class MetricAnomaly(BaseModel):
    source: str
    metric: str
    baseline_mean: float
    baseline_std: float
    peak_value: float
    peak_zscore: float
    first_anomalous_at: datetime
    sample_record_ids: list[str]


class TimelineEvent(BaseModel):
    timestamp: datetime
    source: str
    kind: Literal["change", "error_spike_onset", "metric_anomaly", "new_error_signature"]
    description: str
    record_ids: list[str] = Field(default_factory=list)
    offset_seconds: float | None = Field(
        default=None, description="Seconds relative to the first detected anomaly (negative = before)"
    )
    # Only set on kind == "change":
    lead_seconds: float | None = Field(
        default=None, description="Seconds the change preceded the first anomaly (negative if it came after)"
    )
    followed_by_anomaly: bool | None = Field(
        default=None, description="True if an anomaly started within the lookback window after this change"
    )
    nearest_preceding_change: bool = Field(
        default=False, description="True for the change closest before the first anomaly"
    )


class ActivityBucket(BaseModel):
    timestamp: datetime
    total: int
    errors: int


class CorrelationReport(BaseModel):
    baseline_window: TimeRange
    incident_window: TimeRange
    zscore_threshold: float
    error_spikes: list[ErrorSpike] = Field(default_factory=list)
    signatures: list[ErrorSignature] = Field(default_factory=list)
    metric_anomalies: list[MetricAnomaly] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    first_anomaly_at: datetime | None = None
    activity: dict[str, list[ActivityBucket]] = Field(default_factory=dict)

    def cited_record_ids(self) -> set[str]:
        """Every record id the ranker is allowed to cite (i.e. ids it was shown)."""
        ids: set[str] = set()
        for signature in self.signatures:
            ids.update(signature.sample_record_ids)
        for anomaly in self.metric_anomalies:
            ids.update(anomaly.sample_record_ids)
        for event in self.timeline:
            ids.update(event.record_ids)
        return ids


# ── Ranker output ────────────────────────────────────────────────────────────


class Hypothesis(BaseModel):
    rank: int
    title: str
    description: str
    confidence: float = Field(ge=0.0, le=1.0, description="Model-reported, not statistically calibrated")
    supporting_evidence_ids: list[str] = Field(min_length=1)


# ── API result ───────────────────────────────────────────────────────────────


class EvidenceSummary(BaseModel):
    source: str
    source_type: str
    record_count: int
    truncated: bool
    elapsed_ms: int
    query_used: str
    planned_by: Literal["llm", "fallback"]
    plan_note: str = ""


class RCAResult(BaseModel):
    incident: Incident
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    correlation: CorrelationReport | None = None
    evidence: list[EvidenceSummary] = Field(default_factory=list)
    cited_records: dict[str, EvidenceRecord] = Field(
        default_factory=dict, description="Records referenced by hypotheses or the timeline, keyed by id"
    )
    source_failures: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    leading_candidate: dict | None = Field(
        default=None, description="The ranker's explicit accept/reject verdict on the leading candidate"
    )
    timings_ms: dict[str, int] = Field(default_factory=dict)
