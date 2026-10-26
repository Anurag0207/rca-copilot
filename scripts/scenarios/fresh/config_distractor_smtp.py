"""
FRESH / HARD (config-change-with-distractor type): the real cause is a config change that cuts a
connection limit; a second, unrelated config change sits closer to it in time and also precedes the
anomaly by less than the lookback window, so timing alone cannot separate them.
"""

from scenarios.base import GroundTruth, Scenario, ScenarioBuilder


def build(b: ScenarioBuilder) -> None:
    b.change(-8, "config_change", "notify-api log sampling rate 1.0 -> 0.5 (config rev 211)", author="ops")
    b.change(1, "config_change", "notify-api smtp-relay max_connections 50 -> 5 (config rev 212)", author="ops")
    b.metric("smtp_pool_active", 4, 5, trigger_min=2, noise=0.0, cap=5)
    b.metric("smtp_send_queue", 20, 8000, trigger_min=3, ramp_min=6)
    b.metric("email_delivery_success_rate", 99.5, 20, trigger_min=3, ramp_min=3, noise=0.01)
    b.metric("cpu_pct", 28)
    b.burst(3, 21, 22, "error", lambda r: "timeout acquiring SMTP connection from relay pool after 10000ms")
    b.burst(6, 21, 14, "error", lambda r: f"send failed for notification {r.randint(10**6, 10**7)}: relay unavailable")


SCENARIO = Scenario(
    name="config_distractor_smtp",
    service="notify-api",
    symptom="email notifications are not being delivered",
    description="Config rev 212 cuts the SMTP relay connection limit; config rev 211 (log sampling) is an unrelated distractor.",
    ground_truth=GroundTruth(
        label="Config change cut the SMTP relay connection limit",
        keywords=("smtp|mail|relay", "config", "connection|limit|pool"),
        expected_sources=("deploys",),
    ),
    seed=2103,
    build=build,
    hard=True,
    tags=("fresh", "distractor"),
)
