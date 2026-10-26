"""
HARD: the container CPU limit drops, so the process is throttled. There is no change event
and no log line mentions CPU: the cause exists only in metrics.
"""

from scenarios.base import GroundTruth, Scenario, ScenarioBuilder


def build(b: ScenarioBuilder) -> None:
    b.metric("container_cpu_limit_cores", 2.0, 0.5, trigger_min=2, noise=0.0)
    b.metric("cpu_throttled_seconds_per_min", 0.5, 45, trigger_min=2, noise=0.1)
    b.metric("cpu_usage_cores", 1.4, 0.5, trigger_min=2, noise=0.03)
    b.metric("http_p99_ms", 150, 4800, trigger_min=3, ramp_min=4)
    b.metric("memory_used_pct", 52)
    b.burst(3, 21, 30, "warn", lambda r: f"request took {r.randint(1200, 5000)}ms (slow request threshold 1000ms)")
    b.burst(8, 21, 12, "error", lambda r: "GET /recommendations 504 gateway timeout after 5000ms", http_status=504)


SCENARIO = Scenario(
    name="cpu_throttling_metrics_only",
    service="recs-api",
    symptom="recommendation requests are slow and timing out",
    description="CPU limit cut from 2.0 to 0.5 cores; visible only in container metrics.",
    ground_truth=GroundTruth(
        label="CPU throttling after the container CPU limit was reduced",
        keywords=("cpu", "throttl|limit", "reduc|lower|decreas|quota|container|cut"),
        expected_sources=("metrics",),
    ),
    seed=1110,
    build=build,
    hard=True,
    tags=("metrics-only",),
)
