"""Correlator: spike detection, error signatures, metric anomalies, timeline alignment."""

from __future__ import annotations

import pytest

from app.rca.correlator import Correlator, error_signature
from tests.rca_helpers import evidence, make_incident, record

BASELINE_MINUTES = 60


def logs_with_spike(spike_start: int = 5, spike_per_min: int = 20) -> list:
    """Baseline: one error every 10 min. Incident: a burst of timeouts from `spike_start`."""
    rows, seq = [], 0
    for minute in range(-BASELINE_MINUTES, 0):
        rows.append(record("logs", seq, minute + 0.1, "request ok", "info"))
        seq += 1
        if minute % 10 == 0:
            rows.append(record("logs", seq, minute + 0.2, "cache miss for key 42", "error"))
            seq += 1
    for minute in range(spike_start, 15):
        for i in range(spike_per_min):
            rows.append(record("logs", seq, minute + i / 100, f"timeout acquiring connection from pool after {30000 + i}ms", "error"))
            seq += 1
    return rows


def analyze(*sources, **kwargs):
    return Correlator(zscore_threshold=3.0).analyze(make_incident(), list(sources), BASELINE_MINUTES, **kwargs)


def test_detects_error_spike_with_onset_and_zscore():
    report = analyze(evidence("logs", logs_with_spike(spike_start=5)))
    assert len(report.error_spikes) == 1
    spike = report.error_spikes[0]
    assert spike.source == "logs"
    assert spike.onset.minute == 5
    assert spike.peak_count == 20
    assert spike.peak_zscore >= 3.0
    assert spike.spike_buckets == 10


def test_flat_error_rate_produces_no_spike():
    rows = [record("logs", i, m, "boom", "error") for i, m in enumerate(range(-60, 15))]  # 1 error/min throughout
    report = analyze(evidence("logs", rows))
    assert report.error_spikes == []
    assert report.first_anomaly_at is None


def test_zero_baseline_does_not_divide_by_zero():
    rows = [record("logs", i, 5 + i / 10, "boom", "error") for i in range(30)]  # only incident errors
    report = analyze(evidence("logs", rows))
    assert report.error_spikes and report.error_spikes[0].baseline_std == 1.0  # std floor


def test_signatures_mask_volatile_tokens_and_flag_novel_errors():
    report = analyze(evidence("logs", logs_with_spike()))
    top = report.signatures[0]
    assert top.signature == "timeout acquiring connection from pool after <n>ms"
    assert top.incident_count == 200
    assert top.novel is True
    assert len(top.sample_record_ids) == 3


def test_known_baseline_error_is_not_novel():
    rows = logs_with_spike()
    rows.append(record("logs", 9000, 7.5, "cache miss for key 99", "error"))  # same signature as the baseline noise
    cache = [s for s in analyze(evidence("logs", rows)).signatures if "cache miss" in s.signature]
    assert len(cache) == 1 and cache[0].novel is False and cache[0].baseline_count > 0


def test_metric_anomaly_uses_baseline_zscore():
    rows = [record("metrics", i, m, "pool", None, metric="db_pool_active", value=10 + (i % 3)) for i, m in enumerate(range(-30, 0))]
    rows += [record("metrics", 100 + i, 6 + i, "pool", None, metric="db_pool_active", value=50) for i in range(5)]
    report = analyze(evidence("metrics", rows, "hive"))
    assert len(report.metric_anomalies) == 1
    anomaly = report.metric_anomalies[0]
    assert anomaly.metric == "db_pool_active" and anomaly.peak_value == 50 and anomaly.peak_zscore >= 3.0
    assert anomaly.first_anomalous_at.minute == 6


def test_metric_with_too_little_baseline_is_ignored():
    rows = [record("metrics", 0, -5, "pool", None, metric="m", value=1), record("metrics", 1, 6, "pool", None, metric="m", value=99)]
    assert analyze(evidence("metrics", rows, "hive")).metric_anomalies == []


def test_timeline_orders_deploy_before_metric_before_errors_with_offsets():
    deploy = record("deploys", 0, 1, "checkout-api v2.4.1 rolled out", None, kind="deploy")
    metrics = [record("metrics", i, m, "pool", None, metric="db_pool_active", value=10 + (i % 2)) for i, m in enumerate(range(-30, 0))]
    metrics += [record("metrics", 100, 3, "pool", None, metric="db_pool_active", value=100)]

    report = analyze(
        evidence("logs", logs_with_spike(spike_start=5)),
        evidence("deploys", [deploy], "mongodb"),
        evidence("metrics", metrics, "hive"),
    )

    kinds = [(e.source, e.kind) for e in report.timeline]
    assert kinds.index(("deploys", "change")) < kinds.index(("metrics", "metric_anomaly")) < kinds.index(("logs", "error_spike_onset"))
    by_kind = {e.kind: e for e in report.timeline if e.source != "logs" or e.kind == "error_spike_onset"}
    assert report.first_anomaly_at == by_kind["metric_anomaly"].timestamp
    assert by_kind["metric_anomaly"].offset_seconds == 0
    assert by_kind["error_spike_onset"].offset_seconds == pytest.approx(120, abs=1)
    assert by_kind["change"].offset_seconds == pytest.approx(-120, abs=1)


def test_cited_ids_cover_timeline_signatures_and_metrics():
    report = analyze(evidence("logs", logs_with_spike()))
    assert set(report.signatures[0].sample_record_ids) <= report.cited_record_ids()


def test_activity_histogram_counts_errors_per_source_minute():
    report = analyze(evidence("logs", logs_with_spike(spike_start=5)))
    bucket = next(b for b in report.activity["logs"] if b.timestamp.minute == 5 and b.timestamp.hour == 12)
    assert bucket.errors == 20


def test_error_signature_masks_uuid_ip_hex_numbers():
    assert error_signature("Failed 10.0.3.7:5432 req 3f2b8c1e-aaaa-bbbb-cccc-123456789abc code 503") == "failed <ip> req <uuid> code <n>"


# ── change proximity ────────────────────────────────────────────────────────


def test_changes_are_marked_by_lead_time_and_whether_an_anomaly_followed():
    old_change = record("deploys", 0, -38, "ttl 300 -> 600", None, kind="config_change")
    near_change = record("deploys", 1, 1, "v2 rolled out", None, kind="deploy")
    report = analyze(evidence("logs", logs_with_spike(spike_start=5)), evidence("deploys", [old_change, near_change], "mongodb"))

    by_msg = {e.description.split(": ", 1)[1]: e for e in report.timeline if e.kind == "change"}
    near, old = by_msg["v2 rolled out"], by_msg["ttl 300 -> 600"]
    assert near.followed_by_anomaly is True and old.followed_by_anomaly is False
    assert near.lead_seconds == pytest.approx(240, abs=1)
    assert old.lead_seconds == pytest.approx(43 * 60, abs=1)
    assert near.nearest_preceding_change is True and old.nearest_preceding_change is False


def test_change_after_the_first_anomaly_is_never_the_nearest_preceding_change():
    late = record("deploys", 0, 10, "late deploy", None, kind="deploy")
    report = analyze(evidence("logs", logs_with_spike(spike_start=5)), evidence("deploys", [late], "mongodb"))
    change = next(e for e in report.timeline if e.kind == "change")
    assert change.lead_seconds < 0 and change.nearest_preceding_change is False
