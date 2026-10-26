"""A downstream dependency goes down; there is no change event for the affected service."""

from scenarios.base import GroundTruth, Scenario, ScenarioBuilder


def build(b: ScenarioBuilder) -> None:
    b.change(-40, "deploy", "billing-api v4.1.0 rolled out", version="v4.1.0", service="billing-api")
    b.metric("inventory_call_success_rate", 99.8, 0.5, trigger_min=2, noise=0.01)
    b.metric("orders_per_min", 120, 15, trigger_min=3)
    b.metric("cpu_pct", 38)
    b.burst(2, 21, 25, "error", lambda r: "java.net.ConnectException: Connection refused: inventory-service:8080")
    b.burst(3, 21, 15, "warn", lambda r: "circuit breaker 'inventory-service' is OPEN, short-circuiting calls")
    b.burst(4, 21, 12, "error", lambda r: f"POST /orders 503 inventory unavailable (order {r.randint(10**6, 10**7)})", http_status=503)


SCENARIO = Scenario(
    name="dependency_outage",
    service="orders-api",
    symptom="orders cannot be placed; customers see 503 errors",
    description="inventory-service becomes unreachable. The cause is visible in logs and metrics only.",
    ground_truth=GroundTruth(
        label="inventory-service dependency outage",
        keywords=("inventory", "unavailable|down|outage|refused|unreachable|failure", "dependency|downstream|service"),
        expected_sources=("app-logs", "metrics"),
    ),
    seed=1104,
    build=build,
)
