"""
FRESH / HARD (resource-limit type): the container memory limit is cut, pods are OOM-killed. There is
no change event and no log line names memory: the cause exists only in metrics.
"""

from scenarios.base import GroundTruth, Scenario, ScenarioBuilder


def build(b: ScenarioBuilder) -> None:
    b.metric("container_memory_limit_mb", 4096, 1024, trigger_min=2, noise=0.0)
    b.metric("memory_working_set_mb", 900, 1020, trigger_min=2, ramp_min=3, noise=0.01, cap=1024)
    b.metric("oom_kills_per_min", 0, 6, trigger_min=4, noise=0.1)
    b.metric("pod_ready_replicas", 4, 2, trigger_min=5, noise=0.0)
    b.metric("http_p99_ms", 130, 3900, trigger_min=4, ramp_min=3)
    b.metric("cpu_pct", 36)
    b.burst(4, 21, 28, "warn", lambda r: f"readiness probe failed for pod catalog-api-{r.randint(1, 4)}: connection refused")
    b.burst(5, 21, 18, "error", lambda r: "upstream connect error or disconnect/reset before headers")
    b.burst(8, 21, 12, "error", lambda r: "GET /catalog 503 no healthy upstream", http_status=503)


SCENARIO = Scenario(
    name="memory_limit_oom",
    service="catalog-api",
    symptom="catalog requests are failing with 503 errors and pods keep restarting",
    description="Container memory limit cut from 4096MB to 1024MB; visible only in container metrics.",
    ground_truth=GroundTruth(
        label="Container memory limit reduced, causing OOM kills",
        keywords=("memory", "limit", "reduc|lower|decreas|cut|oom"),
        expected_sources=("metrics",),
    ),
    seed=2101,
    build=build,
    hard=True,
    tags=("fresh", "metrics-only"),
)
