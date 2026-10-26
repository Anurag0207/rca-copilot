"""A release shrinks the DB connection pool; requests queue, time out, then 5xx."""

from scenarios.base import GroundTruth, Scenario, ScenarioBuilder


def build(b: ScenarioBuilder) -> None:
    b.change(1, "deploy", "checkout-api v2.4.1 rolled out; changes: spring.datasource.hikari.maximum-pool-size 20 -> 5", version="v2.4.1")
    b.change(-40, "deploy", "inventory-api v1.9.3 rolled out", version="v1.9.3", service="inventory-api")
    b.metric("db_pool_wait_ms", 6, 27000, trigger_min=3, noise=0.2)
    b.metric("db_pool_active", 8, 5, trigger_min=3, noise=0.1)
    b.metric("http_p99_ms", 120, 30000, trigger_min=4, noise=0.05)
    b.metric("cpu_pct", 40)
    b.burst(4, 21, 18, "error", lambda r: f"HikariPool-1 - Connection is not available, request timed out after {r.randint(29000, 30500)}ms")
    b.burst(6, 21, 14, "error", lambda r: f"POST /checkout {r.choice([500, 503, 503])} in {r.randint(30000, 31000)}ms", http_status=503)


SCENARIO = Scenario(
    name="bad_deploy_pool",
    service="checkout-api",
    symptom="checkout-api is returning HTTP 500/503 errors to customers",
    description="Deploy v2.4.1 cuts the DB pool from 20 to 5 connections.",
    ground_truth=GroundTruth(
        label="Deploy reduced DB connection pool size",
        keywords=("pool", "deploy", "reduc|smaller|shrunk|lower|decreas"),
        expected_sources=("deploys",),
    ),
    seed=1101,
    build=build,
)
