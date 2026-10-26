"""Connector contract: registration, config loading, and per-connector query safety."""

from __future__ import annotations

import inspect
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.connectors.base import EvidenceSpec, MCPConnectorBase, QueryRejectedError, TimeRange
from app.connectors.elasticsearch_connector import ElasticsearchConnector
from app.connectors.hive_evidence_connector import HiveEvidenceConnector
from app.connectors.mongo_evidence_connector import MongoEvidenceConnector
from app.connectors.normalize import FieldMap
from app.connectors.registry import EvidenceRegistry, load_builtin_connectors, registered_types, _CONNECTOR_TYPES
from tests.rca_helpers import T0


def settings(**overrides) -> SimpleNamespace:
    base = dict(
        RCA_CONNECTORS=[],
        ES_URL="http://es:9200", ES_INDEX="app-logs-*", ES_USERNAME="", ES_PASSWORD="", ES_API_KEY="", ES_VERIFY_CERTS=True,
        MONGODB_URI="mongodb://mongo:27017", MONGODB_DATABASE="rca_demo", MONGO_CONNECTIONS={},
        HIVE_HOST="hive", HIVE_PORT=10000, HIVE_DATABASE="rca_demo", HIVE_AUTH_MECHANISM="PLAIN",
        HIVE_USERNAME="", HIVE_PASSWORD="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def spec(query=None, limit=100) -> EvidenceSpec:
    return EvidenceSpec(
        service="checkout-api",
        time_range=TimeRange(start=T0 - timedelta(minutes=60), end=T0 + timedelta(minutes=20)),
        query=query,
        limit=limit,
    )


# ── contract ────────────────────────────────────────────────────────────────


def test_builtin_connectors_are_registered():
    assert {"elasticsearch", "mongodb", "hive"} <= set(registered_types())


@pytest.mark.parametrize("name", ["elasticsearch", "mongodb", "hive"])
def test_registered_connectors_implement_the_full_contract(name):
    load_builtin_connectors()
    cls = _CONNECTOR_TYPES[name]
    assert issubclass(cls, MCPConnectorBase)
    assert not inspect.isabstract(cls), f"{name} leaves abstract methods unimplemented"
    assert cls.source_type == name


def test_registry_builds_enabled_connectors_from_config():
    cfg = settings(RCA_CONNECTORS=[
        {"name": "logs", "type": "elasticsearch"},
        {"name": "deploys", "type": "mongodb", "options": {"collection": "deploy_events", "timeout_seconds": 5}},
    ])
    registry = EvidenceRegistry.from_settings(cfg)
    assert registry.names == ["logs", "deploys"]
    assert registry.get("deploys").timeout_seconds == 5
    assert registry.config_errors == {}


def test_registry_reports_bad_entries_without_failing_startup():
    cfg = settings(RCA_CONNECTORS=[
        {"name": "ok", "type": "elasticsearch"},
        {"name": "bad-type", "type": "carrier-pigeon"},
        {"name": "no-collection", "type": "mongodb"},
    ])
    registry = EvidenceRegistry.from_settings(cfg)
    assert registry.names == ["ok"]
    assert set(registry.config_errors) == {"bad-type", "no-collection"}


def test_registry_unknown_name_raises_keyerror():
    with pytest.raises(KeyError):
        EvidenceRegistry({}).get("nope")


# ── elasticsearch ───────────────────────────────────────────────────────────


def es_connector() -> ElasticsearchConnector:
    return ElasticsearchConnector.from_config("logs", settings(), {})


@pytest.mark.asyncio
async def test_es_fetch_adds_time_range_sort_and_normalises_hits():
    connector = es_connector()
    client = MagicMock()
    client.search = AsyncMock(return_value={
        "hits": {
            "total": {"value": 3},
            "hits": [
                {"_source": {"@timestamp": "2026-01-15T12:01:00Z", "level": "ERROR", "message": "boom", "service": "checkout-api", "http": 500}},
                {"_source": {"message": "no timestamp, must be skipped"}},
            ],
        }
    })
    connector._client = client

    result = await connector.fetch_evidence(spec(limit=2))

    kwargs = client.search.call_args.kwargs
    filters = kwargs["query"]["bool"]["filter"]
    assert "range" in filters[1] and "@timestamp" in filters[1]["range"]
    assert kwargs["size"] == 2
    assert [r.level for r in result.records] == ["error"]
    assert result.records[0].attributes == {"http": 500}
    assert result.truncated is True  # 3 total hits, 2 returned


@pytest.mark.asyncio
@pytest.mark.parametrize("fragment", [
    {"script": {"source": "x"}},
    {"bool": {"filter": [{"script_score": {}}]}},
])
async def test_es_rejects_script_queries(fragment):
    connector = es_connector()
    connector._client = MagicMock()
    with pytest.raises(QueryRejectedError):
        await connector.fetch_evidence(spec(query=fragment))


# ── mongodb ─────────────────────────────────────────────────────────────────


def mongo_connector() -> MongoEvidenceConnector:
    return MongoEvidenceConnector.from_config("deploys", settings(), {"collection": "deploy_events"})


def test_mongo_pipeline_is_time_bounded_and_limited():
    collection, pipeline = mongo_connector().build_pipeline(spec(limit=50))
    assert collection == "deploy_events"
    assert pipeline[0]["$match"]["$and"][0] == {"service": "checkout-api"}
    assert "$gte" in pipeline[0]["$match"]["$and"][1]["timestamp"]
    assert pipeline[-1] == {"$limit": 51}


@pytest.mark.parametrize("fragment", [
    {"collection": "users", "match": {}},
    {"collection": "deploy_events", "match": {"$where": "sleep(1000)"}},
    {"collection": "deploy_events", "match": {"$and": [{"$expr": {}}]}},
    {"collection": "deploy_events", "match": "not-a-dict"},
])
def test_mongo_rejects_unsafe_fragments(fragment):
    with pytest.raises(QueryRejectedError):
        mongo_connector().build_pipeline(spec(query=fragment))


# ── hive ────────────────────────────────────────────────────────────────────


def hive_connector() -> HiveEvidenceConnector:
    return HiveEvidenceConnector.from_config("metrics", settings(), {"table": "service_metrics"})


def test_hive_sql_is_parameterised():
    sql, params = hive_connector().build_sql(spec(query={"table": "service_metrics", "filters": {"service": "x'; DROP TABLE t;--"}}))
    assert "DROP" not in sql
    assert params["f0"] == "x'; DROP TABLE t;--"
    assert sql.endswith("LIMIT 101")


@pytest.mark.parametrize("fragment", [
    {"table": "other", "filters": {}},
    {"table": "service_metrics", "filters": {"service; DROP": "x"}},
    {"table": "service_metrics", "filters": {"service": {"$ne": 1}}},
])
def test_hive_rejects_unsafe_fragments(fragment):
    with pytest.raises(QueryRejectedError):
        hive_connector().build_sql(spec(query=fragment))


def test_field_map_reads_dotted_paths():
    from app.connectors.normalize import build_record

    rec = build_record("s", 0, {"ts": "2026-01-15T12:00:00Z", "log": {"level": "WARN"}}, FieldMap(timestamp="ts", level="log.level"))
    assert rec is not None and rec.level == "warn" and rec.id == "s#0"


# ── truncation keeps the incident window ────────────────────────────────────


@pytest.mark.asyncio
async def test_all_connectors_sort_newest_first_so_truncation_keeps_the_incident():
    connector = es_connector()
    client = MagicMock()
    client.search = AsyncMock(return_value={"hits": {"total": {"value": 0}, "hits": []}})
    connector._client = client
    await connector.fetch_evidence(spec())
    assert client.search.call_args.kwargs["sort"][0]["@timestamp"]["order"] == "desc"

    _, pipeline = mongo_connector().build_pipeline(spec())
    assert pipeline[1] == {"$sort": {"timestamp": -1}}

    sql, _ = hive_connector().build_sql(spec())
    assert "ORDER BY ts DESC" in sql


@pytest.mark.asyncio
async def test_es_excludes_routine_levels_server_side():
    connector = es_connector()
    client = MagicMock()
    client.search = AsyncMock(return_value={"hits": {"total": {"value": 0}, "hits": []}})
    connector._client = client
    await connector.fetch_evidence(spec())
    must_not = client.search.call_args.kwargs["query"]["bool"]["must_not"][0]["terms"]["level"]
    assert {"info", "INFO", "debug", "trace"} <= set(must_not)


@pytest.mark.asyncio
async def test_es_exclusion_can_be_disabled_by_option():
    connector = ElasticsearchConnector.from_config("logs", settings(), {"exclude_levels": []})
    client = MagicMock()
    client.search = AsyncMock(return_value={"hits": {"total": {"value": 0}, "hits": []}})
    connector._client = client
    await connector.fetch_evidence(spec())
    assert "must_not" not in client.search.call_args.kwargs["query"]["bool"]
