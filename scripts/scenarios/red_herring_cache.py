"""
HARD: Redis hits maxmemory and evicts keys. The only change event in the window is a
plausible-looking but unrelated deploy two minutes earlier. The real cause is visible in
metrics, not in logs.
"""

from scenarios.base import GroundTruth, Scenario, ScenarioBuilder


def build(b: ScenarioBuilder) -> None:
    b.change(0, "deploy", "profile-api v5.1.0 rolled out; changes: bump jackson 2.15 -> 2.17, tune http client keep-alive", version="v5.1.0")
    b.metric("redis_used_memory_pct", 71, 100, trigger_min=2, ramp_min=6, noise=0.01, cap=100)
    b.metric("redis_evicted_keys_per_min", 0, 9000, trigger_min=4, ramp_min=3, noise=0.1)
    b.metric("cache_hit_ratio", 0.97, 0.31, trigger_min=4, ramp_min=4, noise=0.02, cap=1.0)
    b.metric("cpu_pct", 42)
    b.burst(5, 21, 25, "warn", lambda r: f"slow response from user-db: {r.randint(800, 6000)}ms")
    b.burst(9, 21, 14, "error", lambda r: "GET /profile 504 upstream timeout", http_status=504)


SCENARIO = Scenario(
    name="red_herring_cache",
    service="profile-api",
    symptom="profile pages are slow and returning 504 errors",
    description="Redis memory exhaustion with a misleading unrelated deploy in the window.",
    ground_truth=GroundTruth(
        label="Redis memory exhaustion causing key evictions",
        keywords=("redis|cache", "memory|evict", "exhaust|full|evict|limit|saturat|maxmemory"),
        expected_sources=("metrics",),
    ),
    seed=1109,
    build=build,
    hard=True,
    tags=("red-herring", "metrics-primary"),
)
