"""
Pure evaluation logic: answer matching, statistics, aggregation and report rendering.

No I/O and no LLM here, so everything in this module is unit-tested
(backend/tests/test_rca_evaluation.py).
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# ── matching ────────────────────────────────────────────────────────────────


def normalize_text(text: str) -> str:
    """Lowercase and collapse punctuation so 'Connection-Pool' matches 'connection pool'."""
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


def keyword_present(normalized_text: str, keyword: str) -> bool:
    """A keyword may list alternatives with '|'; any one matching counts."""
    return any(normalize_text(alt) in normalized_text for alt in keyword.split("|") if alt.strip())


def missing_keywords(text: str, keywords: Iterable[str]) -> list[str]:
    normalized = normalize_text(text)
    return [kw for kw in keywords if not keyword_present(normalized, kw)]


def source_of(evidence_id: str, records: dict[str, Any]) -> str:
    """Source name of an evidence id; falls back to the id's own 'source#n' prefix."""
    record = records.get(evidence_id)
    if record is not None:
        return record["source"] if isinstance(record, dict) else record.source
    return evidence_id.rsplit("#", 1)[0]


@dataclass(frozen=True)
class Match:
    text_ok: bool
    source_ok: bool
    missing: int = 0  # how many ground-truth keywords the hypothesis lacks

    @property
    def correct(self) -> bool:
        """Strict (primary) criterion: every keyword present and an expected source cited."""
        return self.text_ok and self.source_ok

    @property
    def lenient(self) -> bool:
        """Secondary criterion: at most one keyword missing, expected source still cited."""
        return self.missing <= 1 and self.source_ok


def match_hypothesis(hypothesis: dict, keywords: Sequence[str], expected_sources: Sequence[str], records: dict[str, Any]) -> Match:
    """
    Correct = title+description contain every keyword (case-insensitive) AND at least one
    cited evidence id comes from an expected source.
    """
    missing = missing_keywords(f"{hypothesis['title']} {hypothesis['description']}", keywords)
    cited_sources = {source_of(i, records) for i in hypothesis["supporting_evidence_ids"]}
    return Match(text_ok=not missing, source_ok=bool(cited_sources & set(expected_sources)), missing=len(missing))


def first_correct_rank(
    hypotheses: Sequence[dict],
    keywords: Sequence[str],
    expected_sources: Sequence[str],
    records: dict[str, Any],
    lenient: bool = False,
) -> int | None:
    """1-based rank of the highest-ranked correct hypothesis (strict unless `lenient`), or None."""
    for position, hypothesis in enumerate(hypotheses, start=1):
        match = match_hypothesis(hypothesis, keywords, expected_sources, records)
        if (match.lenient if lenient else match.correct):
            return position
    return None


def explain_miss(hypotheses: Sequence[dict], keywords: Sequence[str], expected_sources: Sequence[str], records: dict[str, Any], warnings: Sequence[str] = ()) -> str:
    """One-line reason a run did not get the right answer at rank 1 (empty string if it did)."""
    if not hypotheses:
        return "no hypotheses produced" + (f" ({'; '.join(warnings)[:160]})" if warnings else "")
    matches = [match_hypothesis(h, keywords, expected_sources, records) for h in hypotheses]
    if matches[0].correct:
        return ""
    top = hypotheses[0]
    missing = missing_keywords(f"{top['title']} {top['description']}", keywords)
    for position, match in enumerate(matches, start=1):
        if match.correct:
            return f"correct cause ranked #{position}; #1 was '{top['title']}'"
    for position, match in enumerate(matches, start=1):
        if match.text_ok and not match.source_ok:
            return f"hypothesis #{position} named the cause but cited no evidence from {list(expected_sources)}"
    return f"top hypothesis '{top['title']}' missing keywords {missing}"


# ── statistics ──────────────────────────────────────────────────────────────


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile (q in 0..100), same convention as numpy's default."""
    if not values:
        raise ValueError("percentile of empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * q / 100
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def compression_ratio(collected: float, cited: float) -> float | None:
    """Records collected per record cited as evidence; None when nothing was cited."""
    return None if cited <= 0 else collected / cited


def rate(flags: Sequence[bool]) -> float:
    return sum(flags) / len(flags) if flags else 0.0


def aggregate_runs(runs: Sequence[dict]) -> dict:
    """Summarise a list of per-run dicts (same schema for one scenario or for all)."""
    latencies = [r["wall_ms"] for r in runs]
    collected = statistics.fmean(r["records_collected"] for r in runs)
    cited = statistics.fmean(r["records_cited"] for r in runs)
    stages = {
        stage: statistics.median(r["timings_ms"].get(stage, 0) for r in runs)
        for stage in ("collect", "correlate", "rank")
    }
    return {
        "runs": len(runs),
        "top1_accuracy": rate([r["top1"] for r in runs]),
        "top3_accuracy": rate([r["top3"] for r in runs]),
        "top1_lenient_accuracy": rate([r["top1_lenient"] for r in runs]),
        "top3_lenient_accuracy": rate([r["top3_lenient"] for r in runs]),
        "mean_records_collected": round(collected, 1),
        "mean_records_cited": round(cited, 2),
        "compression_ratio": None if compression_ratio(collected, cited) is None else round(compression_ratio(collected, cited), 1),
        "latency_ms_p50": round(percentile(latencies, 50)),
        "latency_ms_p95": round(percentile(latencies, 95)),
        "stage_median_ms": {k: round(v) for k, v in stages.items()},
        "runs_with_failed_source": sum(1 for r in runs if r["source_failures"]),
        "runs_without_hypotheses": sum(1 for r in runs if not r["top3_titles"]),
    }


# ── rendering ───────────────────────────────────────────────────────────────


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _num(value: Any, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value}{suffix}"


def render_markdown(report: dict) -> str:
    meta, overall, scenarios = report["meta"], report["overall"], report["scenarios"]
    lines = [
        "# RCA Copilot evaluation",
        "",
        "**All numbers come from synthetic scenarios generated by `scripts/scenarios/`. They are not production incidents.**",
        "",
        f"- Date: {meta['date']}",
        f"- Model: {meta['model']}",
        f"- Hardware: {meta['hardware']}",
        f"- Command: `{meta['command']}`",
        *([f"- Code evaluated: {meta['label']}"] if meta.get("label") else []),
        f"- Scenarios: {len(scenarios)} ({sum(1 for s in scenarios.values() if s['hard'])} hard), "
        f"{meta['runs_per_scenario']} runs each, {meta['scale']:,} background log events per scenario",
        f"- Sources: {meta['sources_note']}",
        "",
        "## Overall",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Runs | {overall['runs']} |",
        f"| Top-1 accuracy | {_pct(overall['top1_accuracy'])} |",
        f"| Top-3 accuracy | {_pct(overall['top3_accuracy'])} |",
        f"| Top-1 / Top-3, lenient (secondary: at most 1 keyword missing) | {_pct(overall['top1_lenient_accuracy'])} / {_pct(overall['top3_lenient_accuracy'])} |",
        f"| Events in store per run (mean) | {overall['mean_events_in_store']:,.0f} |",
        f"| Records collected per run (mean) | {overall['mean_records_collected']:,.0f} |",
        f"| Records cited by hypotheses per run (mean) | {overall['mean_records_cited']} |",
        f"| Evidence compression (collected / cited) | {_num(overall['compression_ratio'], 'x')} |",
        f"| Events in store / cited | {_num(overall['store_to_cited_ratio'], 'x')} |",
        f"| End-to-end latency p50 / p95 | {overall['latency_ms_p50'] / 1000:.1f}s / {overall['latency_ms_p95'] / 1000:.1f}s |",
        f"| Stage medians (collect / correlate / rank) | {overall['stage_median_ms']['collect'] / 1000:.1f}s / "
        f"{overall['stage_median_ms']['correlate'] / 1000:.2f}s / {overall['stage_median_ms']['rank'] / 1000:.1f}s |",
        "",
        "## Per scenario",
        "",
        "| Scenario | Hard | Truth | Top-1 | Top-3 | Top-1 lenient | In store | Collected | Cited | Compression | p50 | p95 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, s in scenarios.items():
        lines.append(
            f"| {name} | {'yes' if s['hard'] else 'no'} | {s['ground_truth']} | {_pct(s['top1_accuracy'])} | {_pct(s['top3_accuracy'])} | {_pct(s['top1_lenient_accuracy'])} "
            f"| {s['events_in_store']:,} | {s['mean_records_collected']:,.0f} | {s['mean_records_cited']} "
            f"| {_num(s['compression_ratio'], 'x')} | {s['latency_ms_p50'] / 1000:.1f}s | {s['latency_ms_p95'] / 1000:.1f}s |"
        )
    lines += [
        "",
        f"Percentiles use linear interpolation over n={meta['runs_per_scenario']} runs per scenario "
        f"(n={overall['runs']} overall); per-scenario p95 is close to the maximum.",
        "",
        "## Stage timings (median per run)",
        "",
        "| Scenario | Collect | Correlate | Rank |",
        "|---|---|---|---|",
    ]
    for name, s in scenarios.items():
        st = s["stage_median_ms"]
        lines.append(f"| {name} | {st['collect'] / 1000:.1f}s | {st['correlate'] / 1000:.2f}s | {st['rank'] / 1000:.1f}s |")

    speedup = report.get("speedup")
    if speedup:
        lines += [
            "",
            "## Parallel vs sequential collection (real connectors)",
            "",
            "| Mode | Median sequential | Median parallel | Median speedup | Samples |",
            "|---|---|---|---|---|",
        ]
        for mode, row in speedup["modes"].items():
            lines.append(
                f"| {mode} | {row['sequential_ms_median']:.0f}ms | {row['parallel_ms_median']:.0f}ms "
                f"| {row['speedup_median']:.2f}x | {row['samples']} |"
            )
        lines += ["", speedup["note"]]

    failures = [(n, f) for n, s in scenarios.items() for f in s["failures"]]
    lines += ["", "## Runs that missed at rank 1", ""]
    if not failures:
        lines.append("None.")
    else:
        lines += ["| Scenario | Run | Reason |", "|---|---|---|"]
        lines += [f"| {n} | {f['run']} | {f['reason']} |" for n, f in failures]

    lines += ["", "## Notes", ""] + [f"- {note}" for note in meta["notes"]]
    return "\n".join(lines) + "\n"
