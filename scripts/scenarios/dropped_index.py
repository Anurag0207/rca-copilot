"""A migration shipped with a release drops a database index; queries go from ms to timeouts."""

from scenarios.base import GroundTruth, Scenario, ScenarioBuilder


def build(b: ScenarioBuilder) -> None:
    b.change(2, "deploy", "reports-api v7.8.0 rolled out; migration 0042 drops index idx_orders_created_at on orders", version="v7.8.0")
    b.change(-30, "config_change", "reports-api report cache TTL 300s -> 600s", author="ops")
    b.metric("db_query_p99_ms", 90, 28000, trigger_min=3, ramp_min=5, noise=0.08)
    b.metric("db_cpu_pct", 30, 95, trigger_min=3, ramp_min=4, noise=0.03, cap=100)
    b.metric("http_p99_ms", 400, 30000, trigger_min=5, ramp_min=4)
    b.burst(3, 21, 25, "warn", lambda r: f"slow query ({r.randint(2000, 29000)}ms): SELECT * FROM orders WHERE created_at > ? ORDER BY created_at")
    b.burst(8, 21, 15, "error", lambda r: "query timeout after 30000ms (statement_timeout)")
    b.burst(9, 21, 10, "error", lambda r: "GET /reports/daily 504 gateway timeout", http_status=504)


SCENARIO = Scenario(
    name="dropped_index",
    service="reports-api",
    symptom="daily reports time out with 504 errors",
    description="Migration 0042 in release v7.8.0 drops the index used by the report query.",
    ground_truth=GroundTruth(
        label="Migration dropped a database index",
        keywords=("index", "drop|migration|remov|missing", "slow|query|scan|database|db"),
        expected_sources=("deploys",),
    ),
    seed=1108,
    build=build,
)
