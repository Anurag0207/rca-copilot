"""A config change lowers an HTTP client timeout below the downstream's real latency."""

from scenarios.base import GroundTruth, Scenario, ScenarioBuilder


def build(b: ScenarioBuilder) -> None:
    b.change(1, "config_change", "payments-api fraud-service client read timeout 5000ms -> 500ms (config rev 88)", author="ops")
    b.change(-25, "config_change", "payments-api log level set to INFO (config rev 87)", author="ops")
    b.metric("fraud_call_p99_ms", 180, 500, trigger_min=2, noise=0.03)
    b.metric("payment_success_rate", 99.5, 62, trigger_min=3, noise=0.02)
    b.metric("http_p99_ms", 220, 700, trigger_min=3)
    b.burst(2, 21, 20, "error", lambda r: "java.net.SocketTimeoutException: Read timed out calling fraud-service after 500ms")
    b.burst(5, 21, 12, "error", lambda r: f"POST /payments 502 fraud check failed (order {r.randint(10**6, 10**7)})", http_status=502)


SCENARIO = Scenario(
    name="config_timeout",
    service="payments-api",
    symptom="payments are failing with 502 errors",
    description="Config revision 88 cuts the fraud-service client timeout from 5000ms to 500ms.",
    ground_truth=GroundTruth(
        label="Config change shortened the fraud-service client timeout",
        keywords=("timeout", "config", "fraud|downstream|client|500"),
        expected_sources=("deploys",),
    ),
    seed=1103,
    build=build,
)
