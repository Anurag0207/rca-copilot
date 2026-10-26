"""Evaluation scenarios, discovered automatically: every module exposes SCENARIO."""

from __future__ import annotations

import importlib
import pkgutil

from scenarios.base import GroundTruth, Scenario, ScenarioBuilder, ScenarioData  # noqa: F401


def _load(package: str) -> list[Scenario]:
    pkg = importlib.import_module(package)
    found = [
        importlib.import_module(f"{package}.{m.name}").SCENARIO
        for m in pkgutil.iter_modules(pkg.__path__)
        if not m.ispkg and m.name != "base"
    ]
    return sorted(found, key=lambda s: (s.hard, s.name))


def load_all() -> list[Scenario]:
    """The main evaluation set (the original 10)."""
    return _load("scenarios")


def load_fresh() -> list[Scenario]:
    """Scenarios written after the first evaluation to test whether fixes generalise."""
    return _load("scenarios.fresh")


def get(name: str) -> Scenario:
    for scenario in [*load_all(), *load_fresh()]:
        if scenario.name == name:
            return scenario
    raise KeyError(f"unknown scenario {name!r}; available: {[s.name for s in [*load_all(), *load_fresh()]]}")
