"""
Scenario framework: a Scenario is data plus a build function that drives a ScenarioBuilder.

Every scenario produces three datasets in the shapes the connectors read:
  logs     Elasticsearch documents  {"@timestamp", "service", "level", "message", "host", ...}
  changes  MongoDB documents        {"timestamp", "service", "kind", "version", "author", "message"}
  metrics  Hive-shaped rows         {"ts", "service", "metric", "value", "message", "level"}

Timeline (minutes relative to the incident start T): baseline from -60 to 0, the trigger
at a scenario-chosen minute, cascading symptoms after it, incident window ends at +20.
Generation is deterministic: fixed anchor time and a per-scenario random seed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

T0 = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)  # fixed anchor: no dependence on "now"
BASELINE_MINUTES = 60
INCIDENT_MINUTES = 20

# Error volumes are authored for ~20k log events; larger scales multiply them so the
# error:total ratio stays realistic instead of the cascade vanishing into the noise.
_REFERENCE_SCALE = 20_000


@dataclass(frozen=True)
class GroundTruth:
    label: str
    #: lowercase substrings that must ALL appear in a correct hypothesis; "a|b" means either
    keywords: tuple[str, ...]
    #: names of the evidence sources a correct hypothesis may legitimately cite (any one suffices)
    expected_sources: tuple[str, ...]


@dataclass
class ScenarioData:
    logs: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    metrics: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Scenario:
    name: str
    service: str
    symptom: str
    description: str
    ground_truth: GroundTruth
    seed: int
    build: Callable[["ScenarioBuilder"], None]
    hard: bool = False
    tags: tuple[str, ...] = ()

    @property
    def index_name(self) -> str:
        return f"rca-eval-{self.name.replace('_', '-')}"

    @property
    def mongo_database(self) -> str:
        return f"rca_eval_{self.name}"

    @property
    def start(self) -> datetime:
        return T0

    @property
    def end(self) -> datetime:
        return T0 + timedelta(minutes=INCIDENT_MINUTES)

    def generate(self, scale: int = 5_000) -> ScenarioData:
        """Build the datasets. `scale` is the number of background log events."""
        builder = ScenarioBuilder(self.service, scale, self.seed)
        builder.background_traffic()
        self.build(builder)
        return builder.finish()


class ScenarioBuilder:
    def __init__(self, service: str, scale: int, seed: int) -> None:
        self.service = service
        self.scale = scale
        self.rng = random.Random(seed)
        self._error_mult = max(1.0, scale / _REFERENCE_SCALE)
        self._data = ScenarioData()
        self._hosts = [f"{service}-{i}" for i in range(1, 5)]

    # ── primitives ──────────────────────────────────────────────────────────

    def at(self, minute: float, jitter_s: float = 0.0) -> datetime:
        return T0 + timedelta(minutes=minute, seconds=self.rng.uniform(0, jitter_s))

    def log(self, ts: datetime, level: str, message: str, **extra) -> None:
        self._data.logs.append(
            {
                "@timestamp": ts.isoformat(),
                "service": self.service,
                "level": level,
                "message": message,
                "host": self.rng.choice(self._hosts),
                **extra,
            }
        )

    def change(self, minute: float, kind: str, message: str, version: str = "-", author: str = "ci-bot", service: str | None = None) -> None:
        self._data.changes.append(
            {
                "timestamp": self.at(minute),
                "service": service or self.service,
                "kind": kind,
                "version": version,
                "author": author,
                "message": message,
            }
        )

    # ── composites ──────────────────────────────────────────────────────────

    def background_traffic(self) -> None:
        """`scale` routine info events over the whole window, plus sparse benign errors."""
        total_minutes = BASELINE_MINUTES + INCIDENT_MINUTES + 1
        per_minute = max(self.scale // total_minutes, 1)
        paths = ["/api/v1/items", "/api/v1/cart", "/api/v1/session", "/healthz", "/api/v1/search"]
        for minute in range(-BASELINE_MINUTES, INCIDENT_MINUTES + 1):
            for _ in range(per_minute):
                self.log(
                    self.at(minute, 59.9),
                    "info",
                    f"GET {self.rng.choice(paths)} 200 in {self.rng.randint(8, 140)}ms",
                    http_status=200,
                )
            # benign background noise that must NOT be mistaken for the cause
            if minute % 12 == 0:
                self.log(self.at(minute, 59), "warn", f"retrying request id {self.rng.randint(10**5, 10**6)} after transient error")
            if minute % 20 == 0:
                self.log(self.at(minute, 59), "error", f"cache miss for session {self.rng.randint(1000, 9999)}")

    def burst(self, start_min: float, end_min: float, per_min: int, level: str, message: Callable[[random.Random], str], ramp_min: float = 0.0, **extra) -> None:
        """Symptom logs at `per_min` (scaled), optionally ramping up linearly over `ramp_min`."""
        minute = start_min
        while minute < end_min:
            factor = 1.0 if ramp_min <= 0 else min(1.0, (minute - start_min + 1) / ramp_min)
            count = max(int(per_min * self._error_mult * factor), 1)
            for _ in range(count):
                self.log(self.at(minute, 59.9), level, message(self.rng), **extra)
            minute += 1

    def metric(self, name: str, baseline: float, incident: float | None = None, trigger_min: float = 3, noise: float = 0.05, ramp_min: float = 0.0, floor: float | None = None, cap: float | None = None) -> None:
        """
        One sample per minute. Before `trigger_min` the value hovers around `baseline`; after it,
        around `incident` (reaching it linearly over `ramp_min`). `incident=None` keeps the
        metric flat, which is how healthy distractor metrics are added.
        """
        target = baseline if incident is None else incident
        for minute in range(-BASELINE_MINUTES, INCIDENT_MINUTES + 1):
            if minute < trigger_min:
                centre = baseline
            elif ramp_min <= 0:
                centre = target
            else:
                centre = baseline + (target - baseline) * min(1.0, (minute - trigger_min + 1) / ramp_min)
            value = centre + self.rng.gauss(0, abs(centre) * noise + 1e-9)
            if floor is not None:
                value = max(value, floor)
            if cap is not None:
                value = min(value, cap)
            rounded = round(value, 2)
            self._data.metrics.append(
                {
                    "ts": self.at(minute),
                    "service": self.service,
                    "metric": name,
                    "value": float(rounded),
                    "message": f"{name}={rounded}",
                    "level": "",
                }
            )

    def finish(self) -> ScenarioData:
        self._data.logs.sort(key=lambda d: d["@timestamp"])
        return self._data
