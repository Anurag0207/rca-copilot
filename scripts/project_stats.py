#!/usr/bin/env python3
"""
Generate docs/PROJECT_STATS.md: scope counts measured from the repository, plus evaluation numbers
read from the saved evaluation JSON files. Nothing is typed in by hand; rerun after changes.

  python scripts/project_stats.py
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
DOCS = ROOT / "docs"


def count_modules() -> dict[str, int]:
    def py(pattern: str) -> int:
        return len([p for p in BACKEND.glob(pattern) if p.name != "__init__.py" and "venv" not in p.parts and "__pycache__" not in p.parts])

    return {
        "backend": py("app/**/*.py"),
        "rca_engine": py("app/rca/*.py"),
        "connectors": py("app/connectors/*.py"),
    }


def count_endpoints() -> dict[str, int]:
    pattern = re.compile(r"@(?:router|app)\.(get|post|put|delete|patch)\(")
    files = list((BACKEND / "app/api/routes").glob("*.py")) + [BACKEND / "app/main.py"]
    total = sum(len(pattern.findall(f.read_text(encoding="utf-8"))) for f in files)
    rca = len(pattern.findall((BACKEND / "app/api/routes/rca.py").read_text(encoding="utf-8")))
    return {"total": total, "rca": rca}


def count_compose_services() -> int:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    block = text.split("\nservices:", 1)[1].split("\nvolumes:", 1)[0]
    return len(re.findall(r"^  ([a-z_]+):\s*$", block, flags=re.M))


def count_tests() -> int | None:
    try:
        out = subprocess.run([sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"], cwd=BACKEND, capture_output=True, text=True, timeout=120).stdout
        match = re.search(r"(\d+) tests? collected", out)
        return int(match.group(1)) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def count_scenarios() -> dict[str, int]:
    sys.path.insert(0, str(ROOT / "scripts"))
    import scenarios

    main, fresh = scenarios.load_all(), scenarios.load_fresh()
    return {"main": len(main), "main_hard": sum(s.hard for s in main), "fresh": len(fresh)}


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def load(name: str) -> dict | None:
    path = DOCS / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def evaluation_section(baseline: dict, interim: dict | None) -> list[str]:
    o, m = baseline["overall"], baseline["meta"]
    lines = [
        "## Evaluation: baseline (complete)",
        "",
        f"Synthetic scenarios, real Elasticsearch + MongoDB + Ollama (`{m['model']}`), {o['runs']} runs "
        f"({len(baseline['scenarios'])} scenarios x {m['runs_per_scenario']}), {m['scale']:,}+ background log events per scenario, {m['date']}.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Top-1 accuracy (strict) | {pct(o['top1_accuracy'])} |",
        f"| Top-3 accuracy (strict) | {pct(o['top3_accuracy'])} |",
        f"| Top-1 / top-3 (lenient, secondary) | {pct(o['top1_lenient_accuracy'])} / {pct(o['top3_lenient_accuracy'])} |",
        f"| Events in store per run | {o['mean_events_in_store']:,.0f} |",
        f"| Records collected per run | {o['mean_records_collected']:,.0f} |",
        f"| Records cited by hypotheses per run | {o['mean_records_cited']} |",
        f"| Evidence compression (collected / cited) | {o['compression_ratio']}x |",
        f"| End-to-end latency p50 / p95 | {o['latency_ms_p50'] / 1000:.1f}s / {o['latency_ms_p95'] / 1000:.1f}s |",
    ]
    sp = (baseline.get("speedup") or {}).get("modes", {})
    for mode, row in sp.items():
        lines.append(f"| Parallel collection speedup ({mode}) | {row['speedup_median']:.2f}x |")

    if interim:
        names = list(interim["scenarios"])
        b_mean = statistics.fmean(baseline["scenarios"][n]["top1_accuracy"] for n in names)
        i_mean = statistics.fmean(interim["scenarios"][n]["top1_accuracy"] for n in names)
        lat_b = statistics.median(baseline["scenarios"][n]["latency_ms_p50"] for n in names) / 1000
        lat_i = statistics.median(interim["scenarios"][n]["latency_ms_p50"] for n in names) / 1000
        lines += [
            "",
            "## After iterating on the failures: INTERIM (incomplete)",
            "",
            f"The re-evaluation was interrupted after {len(names)} of {len(baseline['scenarios'])} scenarios "
            "(the two hard scenarios had not run yet), so the figures below cover only those scenarios and are compared "
            "against the baseline on the **same** scenarios. They are not a final result.",
            "",
            "| Scenario | Strict top-1 before | after | Top-3 before | after | p50 before | after |",
            "|---|---|---|---|---|---|---|",
        ]
        for n in names:
            b, i = baseline["scenarios"][n], interim["scenarios"][n]
            lines.append(
                f"| {n} | {pct(b['top1_accuracy'])} | {pct(i['top1_accuracy'])} | {pct(b['top3_accuracy'])} | {pct(i['top3_accuracy'])} "
                f"| {b['latency_ms_p50'] / 1000:.1f}s | {i['latency_ms_p50'] / 1000:.1f}s |"
            )
        lines += [
            "",
            f"Mean strict top-1 on these {len(names)} scenarios: {pct(b_mean)} before, {pct(i_mean)} after "
            f"({interim['overall']['runs']} runs). Median per-scenario p50 latency: {lat_b:.1f}s before, {lat_i:.1f}s after; "
            "the fixes made the pipeline slower in every scenario.",
        ]
    return lines


def main() -> int:
    baseline = load("evaluation_baseline.json")
    if baseline is None:
        sys.exit("docs/evaluation_baseline.json not found")
    interim = load("evaluation_after_interim.json")

    modules, endpoints, scen = count_modules(), count_endpoints(), count_scenarios()
    tests = count_tests()
    o = baseline["overall"]

    lines = [
        "# RCA Copilot: project stats",
        "",
        f"Generated by `scripts/project_stats.py` on {date.today().isoformat()}. Counts are measured from the repository; "
        "evaluation figures are read from the saved evaluation JSON. All evaluation data is synthetic.",
        "",
        "## Shareable summary",
        "",
        f"- Source-agnostic root cause analysis: {len(['elasticsearch', 'mongodb', 'hive'])} pluggable evidence connectors "
        "(Elasticsearch, MongoDB, Hive), queried in parallel with per-source timeouts and partial results.",
        "- Deterministic correlation (z-score spikes, error signatures, cross-source timeline) feeds a local LLM "
        f"(`{baseline['meta']['model']}`) that must cite real evidence ids; hypotheses without valid evidence are rejected.",
        f"- Evaluated on {scen['main']} synthetic incident scenarios ({scen['main_hard']} hard) with real stores at 100k+ events each: "
        f"{pct(o['top1_accuracy'])} strict top-1, {pct(o['top3_accuracy'])} top-3 on the first complete run; "
        f"{o['mean_events_in_store']:,.0f} events narrowed to about {o['mean_records_cited']} cited records per analysis.",
        "",
        "## Scope",
        "",
        "| | Count |",
        "|---|---|",
        f"| Backend Python modules | {modules['backend']} |",
        f"| RCA engine modules / connector modules | {modules['rca_engine']} / {modules['connectors']} |",
        f"| API endpoints (RCA-specific) | {endpoints['total']} ({endpoints['rca']}) |",
        f"| Docker Compose services | {count_compose_services()} |",
        f"| Evidence connectors | 3 (Hive connector never run against a real cluster) |",
        f"| Automated tests | {tests if tests is not None else 'n/a'} |",
        f"| Evaluation scenarios | {scen['main']} main ({scen['main_hard']} hard) + {scen['fresh']} fresh generalisation scenarios |",
        "",
    ]
    lines += evaluation_section(baseline, interim)
    lines += [
        "",
        "## What these numbers do not show",
        "",
        "- Scenarios were written by the same people who built the system; matching is keyword-based, so it measures wording and "
        "citation, not reasoning quality.",
        "- The fresh generalisation scenarios and the final re-evaluation have not been run yet, so there is no held-out result.",
        "- Metrics were stored in MongoDB (Hive-shaped rows) because no Hive cluster was available.",
        "- One machine, one local 7B model, 5 runs per scenario: per-scenario percentages move in 20-point steps.",
        "",
        "Details: [evaluation_baseline.md](evaluation_baseline.md), [failure_analysis.md](failure_analysis.md).",
    ]
    (DOCS / "PROJECT_STATS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote", DOCS / "PROJECT_STATS.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
