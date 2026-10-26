#!/usr/bin/env python3
"""
Benchmark sequential vs parallel evidence collection.

Uses the production EvidenceCollector, switching only its `parallel` flag, so the
numbers reflect the real code path.

Modes
  real       Connectors from RCA_CONNECTORS (needs the seeded stores to be reachable).
             The LLM planner is OFF by default so connector I/O is measured in isolation;
             pass --use-llm to include query planning (needs a running Ollama).
  simulated  In-process stand-in connectors that sleep for a configured latency.
             This measures only the orchestration (gather vs loop), NOT real sources.
             Output is labelled SIMULATED and must not be quoted as source latency.

Examples
  python scripts/benchmark_parallel.py --mode simulated --latencies es=120,mongo=80,hive=900
  python scripts/benchmark_parallel.py --mode real --runs 10
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
# Settings requires these even though the benchmark never touches Postgres/Redis.
os.environ.setdefault("SECRET_KEY", "benchmark-not-a-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unused:unused@localhost/unused")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.config import get_settings  # noqa: E402
from app.connectors.base import ConnectorHealth, Evidence, EvidenceSpec, MCPConnectorBase  # noqa: E402
from app.connectors.registry import EvidenceRegistry  # noqa: E402
from app.rca.collector import EvidenceCollector, QueryPlanner  # noqa: E402
from app.rca.models import Incident  # noqa: E402


class SimulatedConnector(MCPConnectorBase):
    source_type = "simulated"

    def __init__(self, name: str, latency_s: float) -> None:
        super().__init__(name)
        self._latency = latency_s

    @classmethod
    def from_config(cls, name, settings, options):  # pragma: no cover - never built from config
        raise NotImplementedError

    query_instructions = ""

    async def connect(self) -> None: ...

    async def health(self) -> ConnectorHealth:
        return ConnectorHealth(name=self.name, source_type=self.source_type, healthy=True)

    async def describe_schema(self) -> str:
        return ""

    def default_query(self, service: str) -> dict:
        return {}

    async def fetch_evidence(self, spec: EvidenceSpec) -> Evidence:
        started = time.monotonic()
        await asyncio.sleep(self._latency)
        return Evidence(
            source=self.name,
            source_type=self.source_type,
            time_range=spec.time_range,
            records=[],
            query_used="simulated",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    async def close(self) -> None: ...


def parse_latencies(raw: str) -> dict[str, float]:
    return {name: float(ms) / 1000 for name, ms in (pair.split("=") for pair in raw.split(","))}


def build_collector(args: argparse.Namespace) -> tuple[EvidenceCollector, EvidenceRegistry, str]:
    if args.mode == "simulated":
        registry = EvidenceRegistry({n: SimulatedConnector(n, s) for n, s in parse_latencies(args.latencies).items()})
        planner, label = None, "SIMULATED (sleep-based stand-ins, not real sources)"
    else:
        settings = get_settings()
        registry = EvidenceRegistry.from_settings(settings)
        if not registry.names:
            sys.exit("No connectors loaded; set RCA_CONNECTORS. Config errors: " + str(registry.config_errors))
        planner = None
        if args.use_llm:
            from app.llm.ollama_provider import OllamaProvider

            planner = QueryPlanner(OllamaProvider(settings))
        label = f"REAL connectors: {', '.join(registry.names)}; LLM planner {'ON' if planner else 'OFF'}"
    collector = EvidenceCollector(
        registry=registry, planner=planner, default_timeout_seconds=args.timeout, baseline_minutes=60, max_records=500
    )
    return collector, registry, label


async def timed_runs(collector: EvidenceCollector, incident: Incident, parallel: bool, runs: int):
    totals_ms: list[float] = []
    per_connector: dict[str, list[float]] = {}
    failures: dict[str, str] = {}
    for _ in range(runs):
        result = await collector.collect(incident, parallel=parallel)
        totals_ms.append(result.elapsed_ms)
        failures.update(result.failures)
        for evidence in result.evidence:
            per_connector.setdefault(evidence.source, []).append(evidence.elapsed_ms)
    return totals_ms, per_connector, failures


def summarize(values: list[float]) -> str:
    return f"median {statistics.median(values):7.0f} ms   min {min(values):7.0f}   max {max(values):7.0f}   (n={len(values)})"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["real", "simulated"], default="real")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--latencies", default="es=120,mongo=80,hive=900", help="simulated mode: name=ms,...")
    parser.add_argument("--use-llm", action="store_true", help="real mode: include LLM query planning")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    collector, registry, label = build_collector(args)
    end = datetime.now(timezone.utc)
    incident = Incident(service="checkout-api", start=end - timedelta(minutes=20), end=end, symptom="benchmark")

    print(f"Mode: {label}")
    await timed_runs(collector, incident, parallel=True, runs=1)  # warm-up: opens clients, primes caches

    seq_total, seq_each, seq_fail = await timed_runs(collector, incident, parallel=False, runs=args.runs)
    par_total, par_each, par_fail = await timed_runs(collector, incident, parallel=True, runs=args.runs)

    print("\nPer-connector fetch latency (sequential runs):")
    for name, values in seq_each.items():
        print(f"  {name:<12} {summarize(values)}")
    print("\nEnd-to-end collection:")
    print(f"  sequential   {summarize(seq_total)}")
    print(f"  parallel     {summarize(par_total)}")
    speedup = statistics.median(seq_total) / max(statistics.median(par_total), 1)
    print(f"\nMedian speedup: {speedup:.2f}x")
    failures = {**seq_fail, **par_fail}
    if failures:
        print("\nWARNING: some connectors failed during the run, numbers exclude them:", failures)

    await registry.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
