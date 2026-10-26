"""
Evidence collection: one LLM-planned query per enabled connector, run in parallel.

Failure isolation: every connector runs in its own task under its own timeout and
reports success or failure as data, so one slow or broken source degrades the
analysis to partial results instead of failing it.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Literal

import structlog

from app.connectors.base import ConnectorError, Evidence, EvidenceSpec, MCPConnectorBase
from app.connectors.registry import EvidenceRegistry
from app.llm.base import LLMProviderBase
from app.llm.json_repair import LLMResponseParseError, escape_literal_newlines, extract_json_object
from app.rca.models import Incident

logger = structlog.get_logger(__name__)

_PLANNER_SYSTEM = (
    "You write ONE read-only evidence query for a root cause analysis. "
    "Respond with ONLY a JSON object: "
    '{"query": <fragment>, "reasoning": "<one sentence>"}. '
    "A broad query for everything about the incident's service is ALWAYS run separately, so your "
    "query should target ADDITIONAL specific evidence suggested by the symptom. "
    "Use only field names and values that appear in the schema; never invent them."
)


@dataclass
class QueryPlan:
    fragment: dict | None
    planned_by: Literal["llm", "fallback"]
    note: str = ""


@dataclass
class CollectionResult:
    evidence: list[Evidence] = field(default_factory=list)
    plans: dict[str, QueryPlan] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    elapsed_ms: int = 0


def merge_evidence(context: Evidence, focus: Evidence) -> Evidence:
    """
    Union of the broad context fetch and the planner's focused fetch.

    Rows are deduplicated by (timestamp, message, attributes) and re-numbered so record ids stay
    unique per source. Truncation is flagged if either fetch was truncated.
    """
    seen: set[tuple] = set()
    merged = []
    for record in [*context.records, *focus.records]:
        key = (record.timestamp, record.message, json.dumps(record.attributes, sort_keys=True, default=str))
        if key not in seen:
            seen.add(key)
            merged.append(record)
    renumbered = [r.model_copy(update={"id": f"{context.source}#{i}"}) for i, r in enumerate(merged)]
    return context.model_copy(
        update={
            "records": renumbered,
            "query_used": json.dumps({"context": context.query_used, "focus": focus.query_used}),
            "elapsed_ms": max(context.elapsed_ms, focus.elapsed_ms),
            "truncated": context.truncated or focus.truncated,
        }
    )


class QueryPlanner:
    """Asks the LLM for a connector-native query fragment; falls back to the connector default."""

    def __init__(self, llm: LLMProviderBase, max_tokens: int = 512) -> None:
        self._llm = llm
        self._max_tokens = max_tokens

    async def plan(self, connector: MCPConnectorBase, incident: Incident) -> QueryPlan:
        try:
            schema = await connector.describe_schema()
            raw = await self._llm.complete(
                [
                    {"role": "system", "content": _PLANNER_SYSTEM},
                    {"role": "user", "content": self._user_prompt(connector, incident, schema)},
                ],
                max_tokens=self._max_tokens,
            )
            fragment = self._parse(raw)
            return QueryPlan(fragment=fragment, planned_by="llm")
        except (ConnectorError, RuntimeError, LLMResponseParseError, json.JSONDecodeError) as exc:
            logger.warning("Query planning failed, using default query", connector=connector.name, error=str(exc))
            return QueryPlan(fragment=None, planned_by="fallback", note=f"planner failed: {exc}")

    @staticmethod
    def _user_prompt(connector: MCPConnectorBase, incident: Incident, schema: str) -> str:
        return (
            f"Incident service: {incident.service}\n"
            f"Symptom: {incident.symptom or '(not provided)'}\n\n"
            f"Source schema:\n{schema}\n\n"
            f"Query fragment format:\n{connector.query_instructions}"
        )

    @staticmethod
    def _parse(raw: str) -> dict:
        payload = json.loads(escape_literal_newlines(extract_json_object(raw)))
        fragment = payload.get("query") if isinstance(payload, dict) else None
        if not isinstance(fragment, dict) or not fragment:
            raise LLMResponseParseError("planner response has no non-empty object under 'query'")
        return fragment


class EvidenceCollector:
    def __init__(
        self,
        registry: EvidenceRegistry,
        planner: QueryPlanner | None,
        default_timeout_seconds: float,
        baseline_minutes: int,
        max_records: int,
    ) -> None:
        self._registry = registry
        self._planner = planner
        self._default_timeout = default_timeout_seconds
        self._baseline_minutes = baseline_minutes
        self._max_records = max_records

    async def collect(self, incident: Incident, parallel: bool = True) -> CollectionResult:
        connectors = self._select(incident)
        started = time.monotonic()
        result = CollectionResult()

        if parallel:
            outcomes = await asyncio.gather(*(self._collect_one(c, incident) for c in connectors))
        else:
            outcomes = [await self._collect_one(c, incident) for c in connectors]

        for connector, (evidence, plan, error) in zip(connectors, outcomes):
            result.plans[connector.name] = plan
            if evidence is not None:
                result.evidence.append(evidence)
            else:
                result.failures[connector.name] = error or "unknown error"
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    def _select(self, incident: Incident) -> list[MCPConnectorBase]:
        if incident.connectors is None:
            return self._registry.all()
        return [self._registry.get(name) for name in incident.connectors]

    async def _collect_one(
        self, connector: MCPConnectorBase, incident: Incident
    ) -> tuple[Evidence | None, QueryPlan, str | None]:
        """Never raises: returns (evidence, plan, None) or (None, plan, error message)."""
        timeout = connector.timeout_seconds or self._default_timeout
        plan = QueryPlan(fragment=None, planned_by="fallback", note="planner disabled")
        try:
            evidence, plan = await asyncio.wait_for(self._plan_and_fetch(connector, incident), timeout)
            return evidence, plan, None
        except asyncio.TimeoutError:
            logger.warning("Connector timed out", connector=connector.name, timeout_seconds=timeout)
            return None, plan, f"timed out after {timeout:g}s"
        except Exception as exc:  # isolation boundary: any source failure becomes a partial result
            logger.warning("Connector failed", connector=connector.name, error=str(exc))
            return None, plan, f"{type(exc).__name__}: {exc}"

    async def _plan_and_fetch(self, connector: MCPConnectorBase, incident: Incident) -> tuple[Evidence, QueryPlan]:
        window = incident.evidence_window(self._baseline_minutes)
        plan = (
            await self._planner.plan(connector, incident)
            if self._planner
            else QueryPlan(fragment=None, planned_by="fallback", note="planner disabled")
        )

        def spec_for(fragment: dict | None) -> EvidenceSpec:
            return EvidenceSpec(
                service=incident.service,
                time_range=window,
                symptom=incident.symptom,
                query=fragment,
                limit=self._max_records,
            )

        # The broad, service-scoped context fetch always runs: the planner's query can only add to it.
        if plan.fragment is None:
            return await connector.fetch_evidence(spec_for(None)), plan

        context_result, focus_result = await asyncio.gather(
            connector.fetch_evidence(spec_for(None)),
            connector.fetch_evidence(spec_for(plan.fragment)),
            return_exceptions=True,
        )
        if isinstance(context_result, BaseException):
            raise context_result
        if isinstance(focus_result, BaseException):
            # A rejected or failing focused query degrades to context-only rather than failing the source.
            logger.warning("Planner fragment fetch failed", connector=connector.name, error=str(focus_result))
            plan = QueryPlan(fragment=None, planned_by="fallback", note=f"focused query failed, context only: {focus_result}")
            return context_result, plan
        return merge_evidence(context_result, focus_result), plan
