"""
FRESH (saturation / traffic type): a bulk import floods a processing queue; consumers saturate, the
backlog grows and uploads are rejected with backpressure. No change event.
"""

from scenarios.base import GroundTruth, Scenario, ScenarioBuilder


def build(b: ScenarioBuilder) -> None:
    b.metric("inbound_messages_per_s", 500, 6000, trigger_min=2, ramp_min=3, noise=0.04)
    b.metric("queue_depth", 10, 400000, trigger_min=3, ramp_min=8, noise=0.05)
    b.metric("consumer_cpu_pct", 40, 97, trigger_min=3, ramp_min=3, noise=0.02, cap=100)
    b.metric("consumer_lag_s", 1, 900, trigger_min=4, ramp_min=8)
    b.burst(4, 21, 35, "warn", lambda r: f"queue depth {r.randint(50000, 400000)} above high watermark 20000")
    b.burst(8, 21, 16, "error", lambda r: f"consumer lag {r.randint(300, 900)}s exceeded SLA of 60s")
    b.burst(9, 21, 10, "error", lambda r: "POST /upload 503 backpressure: queue full", http_status=503)


SCENARIO = Scenario(
    name="queue_flood",
    service="upload-api",
    symptom="uploads are being rejected with 503 and processing is hours behind",
    description="Inbound message rate grows 12x, saturating consumers and filling the queue.",
    ground_truth=GroundTruth(
        label="Inbound message flood saturating the processing queue",
        keywords=("inbound|import|bulk|message|traffic", "flood|spike|surge|increase|backlog|grow", "queue|lag|backpressure|consumer"),
        expected_sources=("metrics", "app-logs"),
    ),
    seed=2102,
    build=build,
    tags=("fresh",),
)
