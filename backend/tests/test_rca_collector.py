"""Collector: parallelism, per-connector timeouts, partial results, planner fallback."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from app.connectors.base import ConnectorError, Evidence, EvidenceSpec
from app.connectors.registry import EvidenceRegistry
from app.rca.collector import EvidenceCollector, QueryPlanner
from tests.rca_helpers import FakeConnector, make_incident, record


def collector(connectors, llm=None, timeout=5.0) -> EvidenceCollector:
    registry = EvidenceRegistry({c.name: c for c in connectors})
    return EvidenceCollector(
        registry=registry,
        planner=QueryPlanner(llm) if llm else None,
        default_timeout_seconds=timeout,
        baseline_minutes=60,
        max_records=100,
    )


@pytest.mark.asyncio
async def test_parallel_collection_takes_max_not_sum_of_connector_latency():
    connectors = [FakeConnector(f"c{i}", delay=0.2) for i in range(4)]
    started = time.monotonic()
    result = await collector(connectors).collect(make_incident(), parallel=True)
    elapsed = time.monotonic() - started
    assert len(result.evidence) == 4
    assert elapsed < 0.5  # sequential would be >= 0.8


@pytest.mark.asyncio
async def test_sequential_mode_sums_latency():
    connectors = [FakeConnector(f"c{i}", delay=0.1) for i in range(3)]
    started = time.monotonic()
    await collector(connectors).collect(make_incident(), parallel=False)
    assert time.monotonic() - started >= 0.3


@pytest.mark.asyncio
async def test_one_failing_and_one_slow_connector_yield_partial_results():
    connectors = [
        FakeConnector("good", records=[record("good", 0, 1)]),
        FakeConnector("broken", error=ConnectorError("connection refused")),
        FakeConnector("slow", delay=5),
    ]
    connectors[2].timeout_seconds = 0.1  # per-connector override

    result = await collector(connectors).collect(make_incident())

    assert [e.source for e in result.evidence] == ["good"]
    assert "connection refused" in result.failures["broken"]
    assert "timed out after 0.1s" in result.failures["slow"]


@pytest.mark.asyncio
async def test_incident_can_restrict_connectors():
    connectors = [FakeConnector("a"), FakeConnector("b")]
    result = await collector(connectors).collect(make_incident(connectors=["b"]))
    assert [e.source for e in result.evidence] == ["b"]


@pytest.mark.asyncio
async def test_spec_covers_baseline_plus_incident_window():
    connector = FakeConnector("a")
    await collector([connector]).collect(make_incident(minutes=20))
    window = connector.last_spec.time_range
    assert (window.end - window.start).total_seconds() == (60 + 20) * 60
    assert connector.last_spec.limit == 100


@pytest.mark.asyncio
async def test_llm_planned_fragment_is_passed_to_connector():
    connector = FakeConnector("a")
    llm = AsyncMock()
    llm.complete.return_value = '```json\n{"query": {"term": {"level": "error"}}, "reasoning": "x"}\n```'

    result = await collector([connector], llm=llm).collect(make_incident())

    # broad context fetch (no fragment) always runs; the planner's fragment is an additional fetch
    assert sorted(str(s.query) for s in connector.specs) == sorted(["None", str({"term": {"level": "error"}})])
    assert result.plans["a"].planned_by == "llm"


@pytest.mark.asyncio
@pytest.mark.parametrize("llm_reply", ["no json here", '{"query": {}}', '{"query": "string"}'])
async def test_unusable_planner_output_falls_back_to_default_query(llm_reply):
    connector = FakeConnector("a")
    llm = AsyncMock()
    llm.complete.return_value = llm_reply

    result = await collector([connector], llm=llm).collect(make_incident())

    assert [s.query for s in connector.specs] == [None]  # connector applies its own default, once
    assert result.plans["a"].planned_by == "fallback"
    assert len(result.evidence) == 1


@pytest.mark.asyncio
async def test_llm_outage_falls_back_instead_of_failing_collection():
    llm = AsyncMock()
    llm.complete.side_effect = RuntimeError("ollama down")
    result = await collector([FakeConnector("a")], llm=llm).collect(make_incident())
    assert len(result.evidence) == 1 and "ollama down" in result.plans["a"].note


@pytest.mark.asyncio
async def test_rejected_llm_query_is_retried_with_default():
    from app.connectors.base import QueryRejectedError

    class PickyConnector(FakeConnector):
        async def fetch_evidence(self, spec: EvidenceSpec):
            if spec.query is not None:
                raise QueryRejectedError("script not allowed")
            return await super().fetch_evidence(spec)

    llm = AsyncMock()
    llm.complete.return_value = '{"query": {"script": 1}}'
    result = await collector([PickyConnector("a")], llm=llm).collect(make_incident())

    assert len(result.evidence) == 1
    assert result.plans["a"].planned_by == "fallback" and "context only" in result.plans["a"].note


# ── context + focus merge ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_context_and_focus_results_are_merged_deduplicated_and_renumbered():
    shared = record("a", 0, 1, "same row")
    only_context = record("a", 1, 2, "context only row")

    class SplitConnector(FakeConnector):
        async def fetch_evidence(self, spec):
            self.specs.append(spec)
            rows = [shared, only_context] if spec.query is None else [shared, record("a", 0, 3, "focus only row")]
            return Evidence(source="a", source_type="fake", time_range=spec.time_range, records=rows, query_used="q", truncated=spec.query is not None)

    llm = AsyncMock()
    llm.complete.return_value = '{"query": {"term": {"x": 1}}}'
    result = await collector([SplitConnector("a")], llm=llm).collect(make_incident())

    merged = result.evidence[0]
    assert [r.message for r in merged.records] == ["same row", "context only row", "focus only row"]
    assert [r.id for r in merged.records] == ["a#0", "a#1", "a#2"]
    assert merged.truncated is True


@pytest.mark.asyncio
async def test_failing_focus_query_degrades_to_context_but_failing_context_fails_the_source():
    class FocusFails(FakeConnector):
        async def fetch_evidence(self, spec):
            if spec.query is not None:
                raise ConnectorError("bad focus")
            return await super().fetch_evidence(spec)

    class ContextFails(FakeConnector):
        async def fetch_evidence(self, spec):
            if spec.query is None:
                raise ConnectorError("context down")
            return await super().fetch_evidence(spec)

    llm = AsyncMock()
    llm.complete.return_value = '{"query": {"term": {"x": 1}}}'
    ok = await collector([FocusFails("a", records=[record("a", 0, 1)])], llm=llm).collect(make_incident())
    broken = await collector([ContextFails("b")], llm=llm).collect(make_incident())

    assert len(ok.evidence[0].records) == 1 and ok.plans["a"].planned_by == "fallback"
    assert "context down" in broken.failures["b"]
