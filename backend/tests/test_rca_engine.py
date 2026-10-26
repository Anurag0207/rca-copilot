"""End-to-end engine and API behaviour with fake connectors and a mocked LLM."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.connectors.base import ConnectorError
from app.connectors.registry import EvidenceRegistry
from app.rca.engine import RCAEngine
from tests.rca_helpers import FakeConnector, make_incident, record
from tests.test_rca_correlator import logs_with_spike

SETTINGS = SimpleNamespace(
    RCA_BASELINE_MINUTES=60,
    RCA_CONNECTOR_TIMEOUT_SECONDS=5.0,
    RCA_MAX_RECORDS_PER_SOURCE=1000,
    RCA_ZSCORE_THRESHOLD=3.0,
    RCA_CHANGE_LOOKBACK_MINUTES=15,
    RCA_RANKER_TIMEOUT_SECONDS=90.0,
)


def build_engine(connectors, llm_replies) -> tuple[RCAEngine, AsyncMock]:
    llm = AsyncMock()
    llm.complete.side_effect = llm_replies
    registry = EvidenceRegistry({c.name: c for c in connectors})
    return RCAEngine(registry, llm, SETTINGS), llm


def planner_reply() -> str:
    return json.dumps({"query": {"term": {"x": 1}}, "reasoning": "r"})


def ranking_reply(cited_id: str) -> str:
    return json.dumps({"hypotheses": [{
        "title": "Bad deploy exhausted the pool",
        "description": "deploy preceded timeouts",
        "confidence": 0.8,
        "supporting_evidence_ids": [cited_id],
    }]})


def sources():
    deploy = record("deploys", 0, 1, "v2.4.1 rolled out", None, kind="deploy")
    return [FakeConnector("logs", records=logs_with_spike()), FakeConnector("deploys", records=[deploy])]


@pytest.mark.asyncio
async def test_full_pipeline_returns_grounded_hypothesis():
    engine, _ = build_engine(sources(), [planner_reply(), planner_reply(), ranking_reply("deploys#0")])
    result = await engine.analyze(make_incident())

    assert result.hypotheses[0].rank == 1
    assert result.hypotheses[0].supporting_evidence_ids == ["deploys#0"]
    assert "deploys#0" in result.cited_records
    assert result.correlation.error_spikes[0].source == "logs"
    assert set(result.timings_ms) == {"collect", "correlate", "rank"}
    assert result.source_failures == {}


@pytest.mark.asyncio
async def test_failed_source_gives_partial_result_with_warning():
    connectors = sources() + [FakeConnector("hive", error=ConnectorError("hiveserver down"))]
    engine, _ = build_engine(connectors, [planner_reply()] * 3 + [ranking_reply("deploys#0")])
    result = await engine.analyze(make_incident())

    assert "hive" in result.source_failures
    assert any("partial result" in w for w in result.warnings)
    assert result.hypotheses


@pytest.mark.asyncio
async def test_ranking_failure_still_returns_correlation_and_no_fabricated_hypotheses():
    engine, _ = build_engine(sources(), [planner_reply(), planner_reply(), "garbage", "garbage"])
    result = await engine.analyze(make_incident())

    assert result.hypotheses == []
    assert result.correlation is not None and result.correlation.error_spikes
    assert any("ranking failed" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_all_sources_down_skips_analysis():
    engine, llm = build_engine([FakeConnector("a", error=ConnectorError("x"))], [planner_reply()])
    result = await engine.analyze(make_incident())

    assert result.correlation is None and result.hypotheses == []
    assert any("no evidence" in w for w in result.warnings)


# ── API ─────────────────────────────────────────────────────────────────────


def api_client(engine: RCAEngine) -> TestClient:
    import os

    os.environ.setdefault("SECRET_KEY", "test")
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    from app.api.deps import get_current_user, get_rca_engine
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_rca_engine] = lambda: engine
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="u1")
    return TestClient(app)


BODY = {"service": "checkout-api", "start": "2026-01-15T12:00:00Z", "end": "2026-01-15T12:20:00Z", "symptom": "5xx"}


def test_analyze_endpoint_round_trip():
    engine, _ = build_engine(sources(), [planner_reply(), planner_reply(), ranking_reply("deploys#0")])
    response = api_client(engine).post("/rca/analyze", json=BODY)
    assert response.status_code == 200
    assert response.json()["hypotheses"][0]["title"].startswith("Bad deploy")


def test_analyze_rejects_inverted_window():
    engine, _ = build_engine(sources(), [])
    response = api_client(engine).post("/rca/analyze", json={**BODY, "end": "2026-01-15T11:00:00Z"})
    assert response.status_code == 422


def test_analyze_unknown_connector_is_400():
    engine, _ = build_engine(sources(), [])
    response = api_client(engine).post("/rca/analyze", json={**BODY, "connectors": ["nope"]})
    assert response.status_code == 400


def test_connectors_health_endpoint():
    engine, _ = build_engine([FakeConnector("ok"), FakeConnector("down", error=ConnectorError("x"))], [])
    response = api_client(engine).get("/rca/connectors/health")
    assert {h["name"]: h["healthy"] for h in response.json()} == {"ok": True, "down": False}


def test_endpoints_require_auth():
    import os

    os.environ.setdefault("SECRET_KEY", "test")
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    from app.main import create_app

    assert TestClient(create_app()).get("/rca/connectors/health").status_code == 401
