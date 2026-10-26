"""Evaluation harness: answer matching, statistics, and scenario generators (no services needed)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import eval_lib  # noqa: E402
import scenarios  # noqa: E402
from app.connectors.base import Evidence, TimeRange  # noqa: E402
from app.connectors.normalize import FieldMap, build_record  # noqa: E402
from app.rca.correlator import Correlator  # noqa: E402
from app.rca.models import Incident  # noqa: E402

ALL = scenarios.load_all()
FRESH = scenarios.load_fresh()
EVERY = ALL + FRESH
SMALL_SCALE = 3_000


def hyp(title="Deploy reduced the connection pool", description="", ids=("deploys#1",)) -> dict:
    return {"rank": 1, "title": title, "description": description, "confidence": 0.8, "supporting_evidence_ids": list(ids)}


# ── matching ────────────────────────────────────────────────────────────────


def test_keywords_are_case_insensitive_and_punctuation_tolerant():
    assert eval_lib.missing_keywords("The Connection-Pool was REDUCED", ["connection pool", "reduc"]) == []


def test_keyword_alternatives_match_any_one():
    assert eval_lib.missing_keywords("heap exhausted", ["memory|heap|oom"]) == []
    assert eval_lib.missing_keywords("cpu exhausted", ["memory|heap|oom"]) == ["memory|heap|oom"]


def test_keyword_is_not_matched_inside_unrelated_text():
    assert eval_lib.missing_keywords("network blip", ["pool"]) == ["pool"]


def test_hypothesis_needs_keywords_and_expected_source():
    kw = ("pool", "deploy")
    ok = eval_lib.match_hypothesis(hyp(ids=["deploys#1"]), kw, ("deploys",), {})
    wrong_source = eval_lib.match_hypothesis(hyp(ids=["app-logs#9"]), kw, ("deploys",), {})
    wrong_text = eval_lib.match_hypothesis(hyp(title="Network blip", ids=["deploys#1"]), kw, ("deploys",), {})
    assert (ok.correct, wrong_source.correct, wrong_text.correct) == (True, False, False)
    assert wrong_source.text_ok and not wrong_source.source_ok
    assert wrong_text.source_ok and not wrong_text.text_ok


def test_any_one_cited_source_is_enough():
    assert eval_lib.match_hypothesis(hyp(ids=["app-logs#1", "deploys#2"]), ("pool",), ("deploys",), {}).correct


def test_source_comes_from_record_when_available():
    records = {"x1": SimpleNamespace(source="metrics"), "x2": {"source": "deploys"}}
    assert eval_lib.source_of("x1", records) == "metrics"
    assert eval_lib.source_of("x2", records) == "deploys"
    assert eval_lib.source_of("app-logs#7", records) == "app-logs"


def test_first_correct_rank_and_top_k_semantics():
    wrong = hyp(title="Network blip", ids=["deploys#1"])
    right = hyp()
    assert eval_lib.first_correct_rank([right, wrong], ("pool",), ("deploys",), {}) == 1
    assert eval_lib.first_correct_rank([wrong, wrong, right], ("pool",), ("deploys",), {}) == 3
    assert eval_lib.first_correct_rank([wrong, wrong], ("pool",), ("deploys",), {}) is None
    assert eval_lib.first_correct_rank([], ("pool",), ("deploys",), {}) is None


def test_lenient_match_allows_exactly_one_missing_keyword_but_still_needs_the_source():
    kw = ("pool", "deploy", "reduc")
    one_missing = hyp(title="Pool reduction", description="", ids=["deploys#1"])  # no 'deploy'... but 'reduc' present
    two_missing = hyp(title="Pool problem", description="", ids=["deploys#1"])
    wrong_source = hyp(title="Deploy reduced pool", description="", ids=["app-logs#1"])
    assert eval_lib.first_correct_rank([one_missing], kw, ("deploys",), {}) is None  # strict: fails
    assert eval_lib.first_correct_rank([one_missing], kw, ("deploys",), {}, lenient=True) == 1
    assert eval_lib.first_correct_rank([two_missing], kw, ("deploys",), {}, lenient=True) is None
    assert eval_lib.first_correct_rank([wrong_source], kw, ("deploys",), {}, lenient=True) is None


def test_explain_miss_distinguishes_failure_modes():
    kw, src = ("pool", "deploy"), ("deploys",)
    assert eval_lib.explain_miss([hyp()], kw, src, {}) == ""
    assert "no hypotheses" in eval_lib.explain_miss([], kw, src, {}, ["ranking failed"])
    assert "ranked #2" in eval_lib.explain_miss([hyp(title="Network blip"), hyp()], kw, src, {})
    assert "cited no evidence" in eval_lib.explain_miss([hyp(ids=["app-logs#1"])], kw, src, {})
    assert "missing keywords" in eval_lib.explain_miss([hyp(title="Network blip")], kw, src, {})


# ── statistics ──────────────────────────────────────────────────────────────


def test_percentile_matches_known_values():
    assert eval_lib.percentile([1, 2, 3, 4, 5], 50) == 3
    assert eval_lib.percentile([1, 2, 3, 4, 5], 95) == pytest.approx(4.8)
    assert eval_lib.percentile([10], 95) == 10
    assert eval_lib.percentile([5, 1, 3], 0) == 1
    with pytest.raises(ValueError):
        eval_lib.percentile([], 50)


def test_compression_ratio():
    assert eval_lib.compression_ratio(1000, 10) == 100
    assert eval_lib.compression_ratio(1000, 0) is None


def make_run(top1, rank, collected=1000, cited=10, wall=20000, failures=None, titles=True) -> dict:
    return {
        "run": 1, "wall_ms": wall, "timings_ms": {"collect": 5000, "correlate": 50, "rank": 15000},
        "records_collected": collected, "records_cited": cited, "events_in_store": 100_000,
        "top1": top1, "top3": rank is not None and rank <= 3, "rank_of_correct": rank,
        "top1_lenient": top1, "top3_lenient": rank is not None and rank <= 3,
        "top3_titles": [{"rank": 1, "title": "t", "confidence": 0.5}] if titles else [],
        "warnings": [], "source_failures": failures or {}, "truncated_sources": [], "planned_by": {}, "reason": "",
    }


def test_aggregate_runs():
    runs = [make_run(True, 1, wall=10000), make_run(False, 2, wall=20000), make_run(False, None, wall=30000, failures={"x": "boom"}, titles=False)]
    agg = eval_lib.aggregate_runs(runs)
    assert agg["top1_accuracy"] == pytest.approx(1 / 3)
    assert agg["top3_accuracy"] == pytest.approx(2 / 3)
    assert agg["latency_ms_p50"] == 20000 and agg["latency_ms_p95"] == 29000
    assert agg["compression_ratio"] == 100.0
    assert agg["runs_with_failed_source"] == 1 and agg["runs_without_hypotheses"] == 1
    assert agg["stage_median_ms"] == {"collect": 5000, "correlate": 50, "rank": 15000}


def test_render_markdown_contains_every_section():
    runs = [make_run(True, 1), make_run(False, None)]
    per = {**eval_lib.aggregate_runs(runs), "hard": True, "ground_truth": "Some cause", "events_in_store": 100_000,
           "failures": [{"run": 2, "reason": "missing keywords ['x']"}]}
    overall = {**eval_lib.aggregate_runs(runs), "mean_events_in_store": 100_000, "store_to_cited_ratio": 10000.0}
    report = {
        "meta": {"date": "2026-01-01", "model": "m", "hardware": "hw", "command": "python scripts/evaluate.py", "runs_per_scenario": 2,
                 "scale": 100_000, "sources_note": "s", "notes": ["a note"]},
        "overall": overall, "scenarios": {"demo": per},
        "speedup": {"modes": {"io_only": {"sequential_ms_median": 30, "parallel_ms_median": 15, "speedup_median": 2.0, "samples": 1}}, "note": "n"},
    }
    text = eval_lib.render_markdown(report)
    for expected in ("synthetic", "Top-1 accuracy | 50%", "| demo | yes |", "Stage timings", "2.00x", "missing keywords", "a note", "python scripts/evaluate.py"):
        assert expected in text


# ── scenario definitions ────────────────────────────────────────────────────


def test_fresh_set_is_separate_and_has_three_scenarios():
    assert len(FRESH) == 3
    assert not ({s.name for s in FRESH} & {s.name for s in ALL})
    assert all("fresh" in s.tags for s in FRESH)
    assert len({s.seed for s in EVERY}) == len(EVERY)


def test_scenario_set_meets_the_brief():
    assert 8 <= len(ALL) <= 10
    assert sum(s.hard for s in ALL) >= 2
    assert len({s.name for s in ALL}) == len(ALL)
    assert len({s.seed for s in ALL}) == len(ALL), "each scenario needs its own seed"
    assert len({s.index_name for s in ALL}) == len(ALL) and len({s.mongo_database for s in ALL}) == len(ALL)


@pytest.mark.parametrize("scenario", EVERY, ids=lambda s: s.name)
def test_ground_truth_is_well_formed(scenario):
    truth = scenario.ground_truth
    assert 3 <= len(truth.keywords) <= 6
    assert truth.expected_sources and set(truth.expected_sources) <= {"app-logs", "deploys", "metrics"}
    assert all(k == k.lower() for k in truth.keywords)


@pytest.mark.parametrize("scenario", EVERY, ids=lambda s: s.name)
def test_generation_is_deterministic(scenario):
    first, second = scenario.generate(SMALL_SCALE), scenario.generate(SMALL_SCALE)
    assert first.logs == second.logs
    assert first.changes == second.changes
    assert first.metrics == second.metrics


@pytest.mark.parametrize("scenario", EVERY, ids=lambda s: s.name)
def test_scale_controls_log_volume(scenario):
    small, large = scenario.generate(2_000), scenario.generate(6_000)
    assert len(small.logs) >= 2_000
    assert len(large.logs) >= 6_000 and len(large.logs) > len(small.logs)


@pytest.mark.parametrize("scenario", EVERY, ids=lambda s: s.name)
def test_data_shapes_match_the_connectors(scenario):
    data = scenario.generate(SMALL_SCALE)
    assert {"@timestamp", "service", "level", "message"} <= set(data.logs[0])
    assert {"ts", "service", "metric", "value"} <= set(data.metrics[0])
    assert all({"timestamp", "service", "kind", "message"} <= set(c) for c in data.changes)
    assert data.metrics, "every scenario needs metrics"


def to_evidence(scenario) -> list[Evidence]:
    """Mimic what the connectors return (the ES connector drops info-level logs)."""
    data = scenario.generate(SMALL_SCALE)
    window = TimeRange(start=scenario.start.replace(hour=11), end=scenario.end)

    def ev(name, rows, fields):
        records = [r for i, row in enumerate(rows) if (r := build_record(name, i, row, fields))]
        return Evidence(source=name, source_type="test", time_range=window, records=records, query_used="")

    signal_logs = [d for d in data.logs if d["level"] not in {"info", "debug", "trace"}]
    return [
        ev("app-logs", signal_logs, FieldMap(timestamp="@timestamp")),
        ev("deploys", [c for c in data.changes if c["service"] == scenario.service], FieldMap()),
        ev("metrics", data.metrics, FieldMap(timestamp="ts")),
    ]


def source_text(evidence: list[Evidence], sources) -> str:
    chunks = []
    for ev in evidence:
        if ev.source in sources:
            for r in ev.records:
                chunks.append(f"{r.message} {r.attributes.get('kind', '')} {r.attributes.get('metric', '')}")
    return eval_lib.normalize_text(" ".join(chunks))


@pytest.mark.parametrize("scenario", EVERY, ids=lambda s: s.name)
def test_ground_truth_is_present_in_the_expected_sources(scenario):
    evidence = to_evidence(scenario)
    truth = scenario.ground_truth

    # 1. the identifying topic (first keyword group) occurs in an expected source's data
    assert eval_lib.keyword_present(source_text(evidence, truth.expected_sources), truth.keywords[0])

    # 2. the deterministic stage detects an anomaly and makes a record from an expected source citable
    incident = Incident(service=scenario.service, start=scenario.start, end=scenario.end, symptom=scenario.symptom)
    report = Correlator().analyze(incident, evidence, 60)
    assert report.first_anomaly_at is not None, "no anomaly detected"
    citable_sources = {rid.rsplit("#", 1)[0] for rid in report.cited_record_ids()}
    assert citable_sources & set(truth.expected_sources), f"cited sources {citable_sources}"


def test_metrics_only_scenario_has_no_cause_in_logs_or_changes():
    scenario = scenarios.get("cpu_throttling_metrics_only")
    data = scenario.generate(SMALL_SCALE)
    assert scenario.hard and data.changes == []
    log_text = eval_lib.normalize_text(" ".join(d["message"] for d in data.logs))
    assert "cpu" not in log_text and "throttl" not in log_text
    assert any("cpu" in m["metric"] for m in data.metrics)


def test_red_herring_scenario_has_a_misleading_change_inside_the_window():
    scenario = scenarios.get("red_herring_cache")
    data = scenario.generate(SMALL_SCALE)
    assert scenario.hard
    inside = [c for c in data.changes if scenario.start <= c["timestamp"] <= scenario.end]
    assert inside and all("redis" not in c["message"].lower() for c in inside)
    assert eval_lib.missing_keywords(inside[0]["message"], scenario.ground_truth.keywords)  # the herring cannot satisfy the truth


def test_background_noise_contains_benign_errors_before_the_incident():
    data = scenarios.get("bad_deploy_pool").generate(SMALL_SCALE)
    before = [d for d in data.logs if d["@timestamp"] < "2026-03-10T12:00" and d["level"] in {"error", "warn"}]
    assert before, "baseline must contain benign warnings/errors so novelty detection is meaningful"
