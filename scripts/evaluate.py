#!/usr/bin/env python3
"""
Evaluate the RCA Copilot on synthetic incident scenarios, using REAL Elasticsearch, MongoDB and
Ollama (nothing mocked). For each scenario:

  1. seed it (--scale background log events, bulk indexed) into its own index/database
  2. run RCAEngine.analyze --runs times
  3. score each run against the scenario's pre-declared ground truth (see eval_lib.py)
  4. measure parallel vs sequential collection on the same connectors

Writes docs/evaluation.md and docs/evaluation.json. Requires running Elasticsearch, MongoDB and
Ollama (see `make eval`). Nothing in the report is estimated: every number is measured by this run.

  python scripts/evaluate.py                       # 10 scenarios x 5 runs, 100k events each
  python scripts/evaluate.py --only bad_deploy_pool --runs 2 --scale 20000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# RCA_BACKEND_ROOT lets a saved copy of older backend code be evaluated side by side
sys.path.insert(0, os.environ.get("RCA_BACKEND_ROOT") or str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# The app's Settings requires these even though evaluation never touches Postgres/Redis.
os.environ.setdefault("SECRET_KEY", "evaluation-not-a-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unused:unused@localhost/unused")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")  # the config default is the Docker hostname

import eval_lib  # noqa: E402
import scenarios  # noqa: E402
import seed_scenarios  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.connectors.registry import EvidenceRegistry  # noqa: E402
from app.llm.ollama_provider import OllamaProvider  # noqa: E402
from app.rca.collector import EvidenceCollector, QueryPlanner  # noqa: E402
from app.rca.engine import RCAEngine  # noqa: E402
from app.rca.models import Incident  # noqa: E402
from scenarios.base import Scenario  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=5, help="analyses per scenario (K)")
    parser.add_argument("--scale", type=int, default=seed_scenarios.DEFAULT_SCALE, help="background log events per scenario")
    parser.add_argument("--only", nargs="*", help="scenario names (default: all in the chosen set)")
    parser.add_argument("--set", choices=["main", "fresh"], default="main", help="scenario set: the original 10, or the fresh generalisation check")
    parser.add_argument("--label", default="", help="free-text label stored in the report (e.g. which code version was evaluated)")
    parser.add_argument("--max-records", type=int, default=10_000, help="RCA_MAX_RECORDS_PER_SOURCE during evaluation")
    parser.add_argument("--speedup-runs", type=int, default=3, help="timed runs per mode for the I/O-only speedup")
    parser.add_argument("--speedup-llm-runs", type=int, default=1, help="timed runs per mode for the LLM-planner speedup")
    parser.add_argument("--skip-speedup", action="store_true")
    parser.add_argument("--model", help="override OLLAMA_MODEL")
    parser.add_argument("--hardware", help="hardware note (default: auto-detected)")
    parser.add_argument("--out-dir", default=str(ROOT / "docs"))
    parser.add_argument("--keep-data", action="store_true", help="do not drop scenario data afterwards")
    return parser.parse_args()


# ── environment description ─────────────────────────────────────────────────


def detect_hardware() -> str:
    parts = [platform.platform(), f"{os.cpu_count()} logical CPUs"]
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name; [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)"],
            capture_output=True, text=True, timeout=20,
        ).stdout.split()
        if out:
            parts.append(f"{' '.join(out[:-1])}, {out[-1]} GB RAM")
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True, timeout=20).stdout.strip()
        if gpu:
            parts.append(f"GPU: {gpu.splitlines()[0]}")
    except (OSError, subprocess.SubprocessError):
        pass
    return "; ".join(parts) + ". Elasticsearch 8.13.2 and MongoDB 7 run in Docker on the same machine."


def connector_config(scenario: Scenario) -> list[dict]:
    return [
        {"name": "app-logs", "type": "elasticsearch", "options": {"index": scenario.index_name}},
        {"name": "deploys", "type": "mongodb", "options": {"database": scenario.mongo_database, "collection": seed_scenarios.CHANGES_COLLECTION}},
        {"name": "metrics", "type": "mongodb", "options": {"database": scenario.mongo_database, "collection": seed_scenarios.METRICS_COLLECTION}},
    ]


def log(message: str) -> None:
    print(message, flush=True)


# ── one scenario ────────────────────────────────────────────────────────────


async def run_once(engine: RCAEngine, scenario: Scenario, run_index: int, events_in_store: int) -> dict:
    incident = Incident(service=scenario.service, start=scenario.start, end=scenario.end, symptom=scenario.symptom)
    truth = scenario.ground_truth
    started = time.monotonic()
    try:
        result = await engine.analyze(incident)
    except Exception as exc:  # a crashed run counts as a miss, with the reason recorded
        return {
            "run": run_index, "wall_ms": round((time.monotonic() - started) * 1000), "timings_ms": {},
            "records_collected": 0, "records_cited": 0, "events_in_store": events_in_store,
            "top1": False, "top3": False, "rank_of_correct": None, "top1_lenient": False, "top3_lenient": False,
            "top3_titles": [],
            "warnings": [], "source_failures": {}, "truncated_sources": [], "planned_by": {},
            "reason": f"analysis raised {type(exc).__name__}: {exc}",
        }
    wall_ms = round((time.monotonic() - started) * 1000)

    hypotheses = [h.model_dump() for h in result.hypotheses]
    rank = eval_lib.first_correct_rank(hypotheses, truth.keywords, truth.expected_sources, result.cited_records)
    lenient_rank = eval_lib.first_correct_rank(hypotheses, truth.keywords, truth.expected_sources, result.cited_records, lenient=True)
    cited_ids = {i for h in hypotheses for i in h["supporting_evidence_ids"]}
    return {
        "run": run_index,
        "wall_ms": wall_ms,
        "timings_ms": result.timings_ms,
        "records_collected": sum(e.record_count for e in result.evidence),
        "records_cited": len(cited_ids),
        "events_in_store": events_in_store,
        "top1": rank == 1,
        "top3": rank is not None and rank <= 3,
        "rank_of_correct": rank,
        "top1_lenient": lenient_rank == 1,
        "top3_lenient": lenient_rank is not None and lenient_rank <= 3,
        "top3_titles": [{"rank": h["rank"], "title": h["title"], "confidence": h["confidence"]} for h in hypotheses[:3]],
        "warnings": result.warnings,
        "source_failures": result.source_failures,
        "truncated_sources": [e.source for e in result.evidence if e.truncated],
        "planned_by": {e.source: e.planned_by for e in result.evidence},
        "reason": eval_lib.explain_miss(hypotheses, truth.keywords, truth.expected_sources, result.cited_records, result.warnings),
    }


async def measure_speedup(registry: EvidenceRegistry, scenario: Scenario, settings, llm: OllamaProvider | None, runs: int) -> dict:
    """Median collection time, sequential vs parallel, on the scenario's real connectors."""
    collector = EvidenceCollector(
        registry=registry,
        planner=QueryPlanner(llm) if llm else None,
        default_timeout_seconds=settings.RCA_CONNECTOR_TIMEOUT_SECONDS,
        baseline_minutes=settings.RCA_BASELINE_MINUTES,
        max_records=settings.RCA_MAX_RECORDS_PER_SOURCE,
    )
    incident = Incident(service=scenario.service, start=scenario.start, end=scenario.end, symptom=scenario.symptom)
    await collector.collect(incident, parallel=True)  # warm-up: connections, schema/mapping caches, model load

    timings: dict[str, list[float]] = {"sequential": [], "parallel": []}
    for _ in range(runs):
        for mode in ("sequential", "parallel"):
            result = await collector.collect(incident, parallel=(mode == "parallel"))
            if result.failures:
                raise RuntimeError(f"connector failed during speedup measurement: {result.failures}")
            timings[mode].append(result.elapsed_ms)
    return {mode: statistics.median(values) for mode, values in timings.items()}


async def evaluate_scenario(scenario: Scenario, args: argparse.Namespace, settings, llm: OllamaProvider, es, mongo) -> dict:
    log(f"\n=== {scenario.name} ({'hard' if scenario.hard else 'standard'}) ===")
    stats = seed_scenarios.seed_scenario(scenario, args.scale, es, mongo)
    events_in_store = stats.logs + stats.changes + stats.metrics
    log(f"seeded {stats.logs:,} logs, {stats.changes} change events, {stats.metrics} metric rows in {stats.seconds}s")

    scenario_settings = settings.model_copy(update={"RCA_CONNECTORS": connector_config(scenario)})
    registry = EvidenceRegistry.from_settings(scenario_settings)
    if registry.config_errors:
        raise RuntimeError(f"connector config rejected: {registry.config_errors}")
    engine = RCAEngine(registry, llm, scenario_settings)

    runs: list[dict] = []
    speedups: dict[str, dict] = {}
    try:
        for i in range(1, args.runs + 1):
            run = await run_once(engine, scenario, i, events_in_store)
            runs.append(run)
            log(f"  run {i}: {run['wall_ms'] / 1000:5.1f}s  top1={run['top1']!s:<5} rank={run['rank_of_correct']}  "
                f"collected={run['records_collected']:,} cited={run['records_cited']}  #1: "
                f"{run['top3_titles'][0]['title'] if run['top3_titles'] else '-'}")
        if not args.skip_speedup:
            speedups["io_only"] = await measure_speedup(registry, scenario, scenario_settings, None, args.speedup_runs)
            speedups["with_llm_planner"] = await measure_speedup(registry, scenario, scenario_settings, llm, args.speedup_llm_runs)
            log(f"  speedup io_only {speedups['io_only']}  with_llm_planner {speedups['with_llm_planner']}")
    finally:
        await registry.close_all()
        if not args.keep_data:
            seed_scenarios.drop_scenario(scenario, es, mongo)

    summary = eval_lib.aggregate_runs(runs)
    return {
        **summary,
        "hard": scenario.hard,
        "service": scenario.service,
        "ground_truth": scenario.ground_truth.label,
        "keywords": list(scenario.ground_truth.keywords),
        "expected_sources": list(scenario.ground_truth.expected_sources),
        "events_in_store": events_in_store,
        "seed_seconds": stats.seconds,
        "failures": [{"run": r["run"], "reason": r["reason"]} for r in runs if not r["top1"]],
        "speedup": speedups,
        "per_run": runs,
    }


# ── report ──────────────────────────────────────────────────────────────────


def build_speedup_summary(per_scenario: dict[str, dict]) -> dict | None:
    modes: dict[str, dict] = {}
    for mode in ("io_only", "with_llm_planner"):
        pairs = [s["speedup"][mode] for s in per_scenario.values() if mode in s["speedup"]]
        if not pairs:
            continue
        modes[mode] = {
            "sequential_ms_median": statistics.median(p["sequential"] for p in pairs),
            "parallel_ms_median": statistics.median(p["parallel"] for p in pairs),
            "speedup_median": statistics.median(p["sequential"] / max(p["parallel"], 1) for p in pairs),
            "samples": len(pairs),
        }
    if not modes:
        return None
    return {
        "modes": modes,
        "note": (
            "Each sample is one scenario's median (io_only: median of several timed runs; with_llm_planner: one timed run "
            "per mode). 'io_only' disables the LLM planner to isolate connector I/O. 'with_llm_planner' includes three query-planning "
            "LLM calls against one local Ollama instance, so its parallel gain depends on that instance's concurrency."
        ),
    }


def build_report(args: argparse.Namespace, settings, per_scenario: dict[str, dict]) -> dict:
    all_runs = [r for s in per_scenario.values() for r in s["per_run"]]
    overall = eval_lib.aggregate_runs(all_runs)
    overall["mean_events_in_store"] = statistics.fmean(r["events_in_store"] for r in all_runs)
    overall["store_to_cited_ratio"] = (
        round(overall["mean_events_in_store"] / overall["mean_records_cited"], 1) if overall["mean_records_cited"] else None
    )

    truncated = sorted({n for n, s in per_scenario.items() for r in s["per_run"] if r["truncated_sources"]})
    fallback = sorted({n for n, s in per_scenario.items() for r in s["per_run"] if "fallback" in r["planned_by"].values()})
    notes = [
        "Scenarios are synthetic and were authored together with the system; ground-truth keywords were fixed before the first run and not changed afterwards.",
        "A run counts as correct only if a hypothesis contains every ground-truth keyword (substring, case-insensitive; '|' = alternatives) AND cites at least one evidence id from an expected source. This checks wording and citation, not the quality of the reasoning. The lenient figures (at most one keyword missing, same source rule) were defined before the full run and are secondary; strict accuracy is the headline number.",
        f"All {args.runs} runs of a scenario analyse identical data (fixed seed); the only variation between runs is LLM sampling (temperature 0.1).",
        "Metrics are stored in MongoDB with the Hive row shape; no Hive cluster was available, so the Hive connector is not exercised by this evaluation.",
        f"The Elasticsearch connector excludes info/debug/trace logs server-side, so 'records collected' counts only warn/error-level logs plus change and metric rows; 'events in store' counts everything seeded. Row cap per source: {args.max_records:,}.",
        "Compression = records collected per record cited by any hypothesis; it measures how far the pipeline narrows the data, not correctness.",
    ]
    if truncated:
        notes.append(f"Row-cap truncation occurred in: {', '.join(truncated)}.")
    if fallback:
        notes.append(f"The LLM query planner fell back to the default query in at least one run for: {', '.join(fallback)}.")

    return {
        "meta": {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "model": settings.OLLAMA_MODEL,
            "hardware": args.hardware or detect_hardware(),
            "command": "python " + " ".join(sys.argv),
            "runs_per_scenario": args.runs,
            "label": args.label,
            "scale": args.scale,
            "sources_note": "Elasticsearch (logs), MongoDB (change events and metrics), real instances; Ollama LLM",
            "notes": notes,
        },
        "overall": overall,
        "scenarios": {n: {k: v for k, v in s.items() if k != "per_run"} for n, s in per_scenario.items()},
        "speedup": build_speedup_summary(per_scenario),
        "raw_runs": {n: s["per_run"] for n, s in per_scenario.items()},
    }


def write_report(report: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "evaluation.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out_dir / "evaluation.md").write_text(eval_lib.render_markdown(report), encoding="utf-8")


# ── main ────────────────────────────────────────────────────────────────────


async def preflight(settings, es, mongo, llm: OllamaProvider) -> None:
    problems = []
    if not es.ping():
        problems.append("Elasticsearch is not reachable (ES_URL)")
    try:
        mongo.admin.command("ping")
    except Exception as exc:
        problems.append(f"MongoDB is not reachable (MONGODB_URI): {exc}")
    try:
        models = await llm.list_models()
        if settings.OLLAMA_MODEL not in models:
            problems.append(f"Ollama model {settings.OLLAMA_MODEL!r} is not pulled; available: {models}")
    except Exception as exc:
        problems.append(f"Ollama is not reachable (OLLAMA_BASE_URL): {exc}")
    if problems:
        sys.exit("Preflight failed, nothing was measured:\n  - " + "\n  - ".join(problems))


async def main() -> int:
    args = parse_args()
    settings = get_settings().model_copy(
        update={
            "RCA_MAX_RECORDS_PER_SOURCE": args.max_records,
            "RCA_CONNECTOR_TIMEOUT_SECONDS": 180.0,
            **({"OLLAMA_MODEL": args.model} if args.model else {}),
        }
    )
    default_set = scenarios.load_fresh() if args.set == "fresh" else scenarios.load_all()
    selected = [scenarios.get(n) for n in args.only] if args.only else default_set
    es, mongo, llm = seed_scenarios.es_client(), seed_scenarios.mongo_client(), OllamaProvider(settings)
    await preflight(settings, es, mongo, llm)

    per_scenario: dict[str, dict] = {}
    for scenario in selected:
        per_scenario[scenario.name] = await evaluate_scenario(scenario, args, settings, llm, es, mongo)
        write_report(build_report(args, settings, per_scenario), Path(args.out_dir))  # checkpoint after every scenario

    report = build_report(args, settings, per_scenario)
    write_report(report, Path(args.out_dir))
    log("\n" + eval_lib.render_markdown(report))
    log(f"Wrote {Path(args.out_dir) / 'evaluation.md'} and evaluation.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
