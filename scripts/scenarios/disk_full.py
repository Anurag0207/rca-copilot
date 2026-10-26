"""A runaway spool fills the volume; writes start failing."""

from scenarios.base import GroundTruth, Scenario, ScenarioBuilder


def build(b: ScenarioBuilder) -> None:
    b.metric("disk_used_pct", 62, 100, trigger_min=1, ramp_min=8, noise=0.01, cap=100)
    b.metric("write_errors_per_min", 0, 300, trigger_min=9, noise=0.1)
    b.metric("ingest_rate_per_s", 900)
    b.burst(2, 9, 8, "warn", lambda r: f"temp spool directory growing: {r.randint(40, 98)}% of volume used")
    b.burst(9, 21, 25, "error", lambda r: "java.io.IOException: No space left on device")
    b.burst(10, 21, 15, "error", lambda r: f"failed to write segment batch-{r.randint(1000, 9999)}")
    b.burst(11, 21, 12, "error", lambda r: "POST /ingest 500 storage write failed", http_status=500)


SCENARIO = Scenario(
    name="disk_full",
    service="ingest-api",
    symptom="ingest requests are failing with HTTP 500",
    description="The data volume fills up and segment writes fail.",
    ground_truth=GroundTruth(
        label="Disk full on the ingest volume",
        keywords=("disk", "full|space|exhaust|capacity|100", "write|storage|volume|spool|ingest"),
        expected_sources=("app-logs", "metrics"),
    ),
    seed=1105,
    build=build,
)
