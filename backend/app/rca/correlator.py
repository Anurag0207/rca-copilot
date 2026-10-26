"""
Deterministic evidence analysis. No LLM, no I/O: same input, same output.

Three analyses over normalised EvidenceRecords:
  1. Error-rate spikes per source: per-minute error counts in the incident window
     scored by z-score against a zero-filled per-minute baseline.
  2. Recurring error signatures: messages with numbers/ids masked, counted in the
     incident window and compared against the baseline to flag novel ones.
  3. Cross-source timeline: spike onsets, metric anomalies, novel signatures and
     change events (deploys, config changes) merged onto one clock, each with its
     offset from the first detected anomaly.

Records are split by time: ``timestamp < incident.start`` is baseline,
``incident.start <= timestamp <= incident.end`` is the incident window.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from app.rca.models import (
    ActivityBucket,
    CorrelationReport,
    ErrorSignature,
    ErrorSpike,
    Evidence,
    EvidenceRecord,
    Incident,
    MetricAnomaly,
    TimelineEvent,
)

ERROR_LEVELS = {"error", "err", "fatal", "critical", "severe"}
CHANGE_KINDS = {"deploy", "config_change", "restart", "scale", "rollback"}
BUCKET = timedelta(minutes=1)

# Floors keep a perfectly flat or all-zero baseline from producing infinite z-scores.
MIN_COUNT_STD = 1.0
MIN_METRIC_REL_STD = 0.05

_MASKS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
]


def error_signature(message: str) -> str:
    """Mask volatile tokens so that repeated occurrences of one error collapse together."""
    text = message.strip().lower()
    for pattern, replacement in _MASKS:
        text = pattern.sub(replacement, text)
    return re.sub(r"\s+", " ", text)[:200]


def _is_error(record: EvidenceRecord) -> bool:
    return (record.level or "") in ERROR_LEVELS


def _floor_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def _minutes_between(start: datetime, end: datetime) -> list[datetime]:
    cursor, last = _floor_minute(start), _floor_minute(end)
    minutes = []
    while cursor <= last:
        minutes.append(cursor)
        cursor += BUCKET
    return minutes


def _numeric_metric(record: EvidenceRecord) -> tuple[str, float] | None:
    metric, value = record.attributes.get("metric"), record.attributes.get("value")
    if isinstance(metric, str) and isinstance(value, (int, float)) and not isinstance(value, bool):
        return metric, float(value)
    return None


class Correlator:
    def __init__(
        self,
        zscore_threshold: float = 3.0,
        max_signatures: int = 8,
        max_timeline_events: int = 40,
        change_lookback_minutes: int = 15,
    ) -> None:
        self._lookback = timedelta(minutes=change_lookback_minutes)
        self._threshold = zscore_threshold
        self._max_signatures = max_signatures
        self._max_timeline_events = max_timeline_events

    def analyze(self, incident: Incident, evidence: list[Evidence], baseline_minutes: int) -> CorrelationReport:
        window = incident.evidence_window(baseline_minutes)
        report = CorrelationReport(
            baseline_window={"start": window.start, "end": incident.start},
            incident_window={"start": incident.start, "end": incident.end},
            zscore_threshold=self._threshold,
        )
        records = sorted((r for e in evidence for r in e.records), key=lambda r: r.timestamp)
        baseline = [r for r in records if r.timestamp < incident.start]
        during = [r for r in records if incident.start <= r.timestamp <= incident.end]

        report.error_spikes = self._error_spikes(incident, baseline_minutes, baseline, during)
        report.signatures = self._signatures(baseline, during)
        report.metric_anomalies = self._metric_anomalies(baseline, during)
        report.timeline, report.first_anomaly_at = self._timeline(report, records)
        report.activity = self._activity(records)
        return report

    # ── 1. error-rate spikes ────────────────────────────────────────────────

    def _error_spikes(
        self,
        incident: Incident,
        baseline_minutes: int,
        baseline: list[EvidenceRecord],
        during: list[EvidenceRecord],
    ) -> list[ErrorSpike]:
        baseline_start = incident.start - timedelta(minutes=baseline_minutes)
        baseline_slots = _minutes_between(baseline_start, incident.start - BUCKET)
        incident_slots = _minutes_between(incident.start, incident.end)

        def per_minute(rows: list[EvidenceRecord]) -> dict[str, Counter]:
            counts: dict[str, Counter] = defaultdict(Counter)
            for r in rows:
                if _is_error(r):
                    counts[r.source][_floor_minute(r.timestamp)] += 1
            return counts

        base_counts, inc_counts = per_minute(baseline), per_minute(during)
        spikes: list[ErrorSpike] = []
        for source in sorted(inc_counts):
            series = [base_counts[source][slot] for slot in baseline_slots]
            mean = statistics.fmean(series) if series else 0.0
            std = max(statistics.pstdev(series) if len(series) > 1 else 0.0, MIN_COUNT_STD)

            scored = [(slot, inc_counts[source][slot], (inc_counts[source][slot] - mean) / std) for slot in incident_slots]
            hot = [(slot, count, z) for slot, count, z in scored if z >= self._threshold]
            if not hot:
                continue
            peak_slot, peak_count, peak_z = max(hot, key=lambda item: item[2])
            spikes.append(
                ErrorSpike(
                    source=source,
                    onset=hot[0][0],
                    peak_at=peak_slot,
                    peak_count=peak_count,
                    baseline_mean=round(mean, 3),
                    baseline_std=round(std, 3),
                    peak_zscore=round(peak_z, 2),
                    spike_buckets=len(hot),
                )
            )
        return sorted(spikes, key=lambda s: s.onset)

    # ── 2. error signatures ─────────────────────────────────────────────────

    def _signatures(self, baseline: list[EvidenceRecord], during: list[EvidenceRecord]) -> list[ErrorSignature]:
        baseline_counts = Counter(error_signature(r.message) for r in baseline if _is_error(r))
        grouped: dict[str, list[EvidenceRecord]] = defaultdict(list)
        for record in during:
            if _is_error(record):
                grouped[error_signature(record.message)].append(record)

        ranked = sorted(grouped.items(), key=lambda item: len(item[1]), reverse=True)[: self._max_signatures]
        return [
            ErrorSignature(
                signature=signature,
                incident_count=len(rows),
                baseline_count=baseline_counts.get(signature, 0),
                novel=baseline_counts.get(signature, 0) == 0,
                first_seen=rows[0].timestamp,  # rows are already chronological
                sources=sorted({r.source for r in rows}),
                sample_record_ids=[r.id for r in rows[:3]],
            )
            for signature, rows in ranked
        ]

    # ── 3a. numeric metric anomalies ────────────────────────────────────────

    def _metric_anomalies(self, baseline: list[EvidenceRecord], during: list[EvidenceRecord]) -> list[MetricAnomaly]:
        def series(rows: list[EvidenceRecord]) -> dict[tuple[str, str], list[tuple[EvidenceRecord, float]]]:
            out: dict[tuple[str, str], list[tuple[EvidenceRecord, float]]] = defaultdict(list)
            for r in rows:
                parsed = _numeric_metric(r)
                if parsed:
                    out[(r.source, parsed[0])].append((r, parsed[1]))
            return out

        base, inc = series(baseline), series(during)
        anomalies: list[MetricAnomaly] = []
        for key, points in inc.items():
            base_values = [v for _, v in base.get(key, [])]
            if len(base_values) < 3:
                continue  # too little baseline to say anything meaningful
            mean = statistics.fmean(base_values)
            std = max(statistics.pstdev(base_values), abs(mean) * MIN_METRIC_REL_STD, 1e-6)

            scored = [(r, v, (v - mean) / std) for r, v in points]
            hot = [item for item in scored if abs(item[2]) >= self._threshold]
            if not hot:
                continue
            peak_record, peak_value, peak_z = max(hot, key=lambda item: abs(item[2]))
            first_record = hot[0][0]
            anomalies.append(
                MetricAnomaly(
                    source=key[0],
                    metric=key[1],
                    baseline_mean=round(mean, 3),
                    baseline_std=round(std, 3),
                    peak_value=peak_value,
                    peak_zscore=round(peak_z, 2),
                    first_anomalous_at=first_record.timestamp,
                    sample_record_ids=list(dict.fromkeys([first_record.id, peak_record.id])),
                )
            )
        return sorted(anomalies, key=lambda a: a.first_anomalous_at)

    # ── 3b. timeline alignment ──────────────────────────────────────────────

    def _timeline(
        self, report: CorrelationReport, records: list[EvidenceRecord]
    ) -> tuple[list[TimelineEvent], datetime | None]:
        events: list[TimelineEvent] = []

        for r in records:
            kind = r.attributes.get("kind")
            if kind in CHANGE_KINDS:
                events.append(
                    TimelineEvent(
                        timestamp=r.timestamp,
                        source=r.source,
                        kind="change",
                        description=f"{kind}: {r.message}",
                        record_ids=[r.id],
                    )
                )
        for spike in report.error_spikes:
            events.append(
                TimelineEvent(
                    timestamp=spike.onset,
                    source=spike.source,
                    kind="error_spike_onset",
                    description=(
                        f"error rate spike (peak {spike.peak_count}/min, z={spike.peak_zscore}, "
                        f"baseline {spike.baseline_mean}/min)"
                    ),
                )
            )
        for anomaly in report.metric_anomalies:
            events.append(
                TimelineEvent(
                    timestamp=anomaly.first_anomalous_at,
                    source=anomaly.source,
                    kind="metric_anomaly",
                    description=(
                        f"{anomaly.metric} anomalous (peak {anomaly.peak_value:g}, "
                        f"baseline {anomaly.baseline_mean:g}, z={anomaly.peak_zscore})"
                    ),
                    record_ids=anomaly.sample_record_ids,
                )
            )
        for signature in report.signatures:
            if signature.novel:
                events.append(
                    TimelineEvent(
                        timestamp=signature.first_seen,
                        source=signature.sources[0],
                        kind="new_error_signature",
                        description=f"new error x{signature.incident_count}: {signature.signature}",
                        record_ids=signature.sample_record_ids,
                    )
                )

        events.sort(key=lambda e: e.timestamp)
        anomaly_times = [e.timestamp for e in events if e.kind != "change"]
        first_anomaly = min(anomaly_times) if anomaly_times else None
        if first_anomaly:
            for event in events:
                event.offset_seconds = (event.timestamp - first_anomaly).total_seconds()
            self._mark_change_proximity(events, first_anomaly)

        if len(events) > self._max_timeline_events:
            # Keep every anomaly; trim change events (the bulk) nearest the first anomaly.
            anomalies = [e for e in events if e.kind != "change"]
            changes = sorted(
                (e for e in events if e.kind == "change"),
                key=lambda e: abs(e.offset_seconds or 0.0),
            )[: max(self._max_timeline_events - len(anomalies), 0)]
            events = sorted(anomalies + changes, key=lambda e: e.timestamp)
        return events, first_anomaly

    def _mark_change_proximity(self, events: list[TimelineEvent], first_anomaly: datetime) -> None:
        """
        Annotate change events with how they relate to the anomalies, purely from timing:
          lead_seconds             how long before the first anomaly the change happened
          followed_by_anomaly      an anomaly began within the lookback window after the change
          nearest_preceding_change the change closest before the first anomaly
        A change that no anomaly follows is a weak suspect however recent it is in absolute terms.
        """
        anomaly_times = [e.timestamp for e in events if e.kind != "change"]
        changes = [e for e in events if e.kind == "change"]
        for change in changes:
            change.lead_seconds = (first_anomaly - change.timestamp).total_seconds()
            change.followed_by_anomaly = any(change.timestamp <= t <= change.timestamp + self._lookback for t in anomaly_times)
        preceding = [c for c in changes if c.lead_seconds is not None and c.lead_seconds >= 0]
        if preceding:
            min(preceding, key=lambda c: c.lead_seconds).nearest_preceding_change = True

    # ── activity histogram for the UI ───────────────────────────────────────

    @staticmethod
    def _activity(records: list[EvidenceRecord]) -> dict[str, list[ActivityBucket]]:
        totals: dict[str, Counter] = defaultdict(Counter)
        errors: dict[str, Counter] = defaultdict(Counter)
        for r in records:
            slot = _floor_minute(r.timestamp)
            totals[r.source][slot] += 1
            if _is_error(r):
                errors[r.source][slot] += 1
        return {
            source: [
                ActivityBucket(timestamp=slot, total=counts[slot], errors=errors[source][slot])
                for slot in sorted(counts)
            ]
            for source, counts in totals.items()
        }

