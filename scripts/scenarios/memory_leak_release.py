"""A release introduces an unbounded cache; heap climbs, GC thrashes, pods OOM."""

from scenarios.base import GroundTruth, Scenario, ScenarioBuilder


def build(b: ScenarioBuilder) -> None:
    b.change(0.5, "deploy", "search-api v3.2.0 rolled out; changes: new result cache (unbounded LRU, no max entries)", version="v3.2.0")
    b.metric("jvm_heap_used_pct", 55, 98, trigger_min=1, ramp_min=11, noise=0.02, cap=99.5)
    b.metric("gc_pause_ms", 40, 2500, trigger_min=4, ramp_min=8, noise=0.1)
    b.metric("pod_restarts_per_min", 0, 3, trigger_min=12, noise=0.1)
    b.metric("cpu_pct", 35)
    b.burst(6, 21, 20, "warn", lambda r: f"GC overhead: stop-the-world pause {r.randint(900, 3000)}ms")
    b.burst(10, 21, 12, "error", lambda r: "java.lang.OutOfMemoryError: Java heap space")
    b.burst(12, 21, 3, "error", lambda r: f"pod search-api-{r.randint(1, 4)} OOMKilled, restarting")


SCENARIO = Scenario(
    name="memory_leak_release",
    service="search-api",
    symptom="search-api latency is spiking and pods keep restarting",
    description="Release v3.2.0 adds an unbounded cache, causing a memory leak and OOM kills.",
    ground_truth=GroundTruth(
        label="Memory leak introduced by release v3.2.0",
        keywords=("memory|heap|oom", "leak|unbounded|growth|grow|increas", "deploy|release|v3.2.0|cache"),
        expected_sources=("deploys", "metrics"),
    ),
    seed=1102,
    build=build,
)
