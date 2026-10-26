"""A TLS certificate expires at the incident start: a hard step change, with the cause in the logs."""

from scenarios.base import GroundTruth, Scenario, ScenarioBuilder


def build(b: ScenarioBuilder) -> None:
    b.metric("login_success_rate", 99.7, 3, trigger_min=0, noise=0.01)
    b.metric("cpu_pct", 30)
    b.burst(0, 21, 30, "error", lambda r: "x509: certificate has expired or is not yet valid: current time is after 2026-03-10T11:59:59Z")
    b.burst(0, 21, 20, "error", lambda r: f"TLS handshake error from 10.0.{r.randint(0, 9)}.{r.randint(1, 250)}: remote error: tls: bad certificate")
    b.burst(1, 21, 10, "error", lambda r: "login failed: identity provider unreachable", http_status=502)


SCENARIO = Scenario(
    name="cert_expiry",
    service="auth-api",
    symptom="users cannot log in",
    description="The service TLS certificate expires exactly at the incident start.",
    ground_truth=GroundTruth(
        label="Expired TLS certificate",
        keywords=("certificate", "expir", "tls|x509|handshake"),
        expected_sources=("app-logs",),
    ),
    seed=1107,
    build=build,
)
