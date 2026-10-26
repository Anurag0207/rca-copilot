"""A sudden traffic surge saturates CPU and trips rate limits; no change event."""

from scenarios.base import GroundTruth, Scenario, ScenarioBuilder


def build(b: ScenarioBuilder) -> None:
    b.metric("requests_per_s", 400, 2400, trigger_min=2, ramp_min=3, noise=0.04)
    b.metric("cpu_pct", 45, 98, trigger_min=3, ramp_min=3, noise=0.02, cap=100)
    b.metric("worker_queue_depth", 5, 900, trigger_min=4, ramp_min=4)
    b.burst(4, 21, 40, "warn", lambda r: f"429 rate limit exceeded for client {r.randint(1000, 9999)}")
    b.burst(8, 21, 15, "error", lambda r: "upstream request timed out: worker queue saturated")
    b.burst(9, 21, 10, "error", lambda r: "GET /api/v1/items 503 service overloaded", http_status=503)


SCENARIO = Scenario(
    name="traffic_surge",
    service="gateway-api",
    symptom="gateway is rejecting requests with 429 and 503 errors",
    description="Request rate grows 6x within minutes, saturating CPU.",
    ground_truth=GroundTruth(
        label="Traffic surge saturating gateway capacity",
        keywords=("traffic|load|request rate|requests", "spike|surge|increase|sudden|flood|grow", "cpu|saturat|capacity|overload"),
        expected_sources=("metrics", "app-logs"),
    ),
    seed=1106,
    build=build,
)
