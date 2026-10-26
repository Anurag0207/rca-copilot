"""RCA orchestration: collect -> correlate -> rank, with partial-result handling."""

from __future__ import annotations

import asyncio
import time

import structlog

from app.config import Settings
from app.connectors.base import ConnectorHealth, Evidence
from app.connectors.registry import EvidenceRegistry
from app.llm.base import LLMProviderBase
from app.rca.collector import EvidenceCollector, QueryPlanner
from app.rca.correlator import Correlator
from app.rca.models import EvidenceRecord, EvidenceSummary, Incident, RCAResult
from app.rca.ranker import HypothesisRanker, RankingError

logger = structlog.get_logger(__name__)


class RCAEngine:
    def __init__(self, registry: EvidenceRegistry, llm: LLMProviderBase, settings: Settings) -> None:
        self._registry = registry
        self._baseline_minutes = settings.RCA_BASELINE_MINUTES
        self._collector = EvidenceCollector(
            registry=registry,
            planner=QueryPlanner(llm),
            default_timeout_seconds=settings.RCA_CONNECTOR_TIMEOUT_SECONDS,
            baseline_minutes=settings.RCA_BASELINE_MINUTES,
            max_records=settings.RCA_MAX_RECORDS_PER_SOURCE,
        )
        self._correlator = Correlator(
            zscore_threshold=settings.RCA_ZSCORE_THRESHOLD,
            change_lookback_minutes=settings.RCA_CHANGE_LOOKBACK_MINUTES,
        )
        self._ranker = HypothesisRanker(llm, call_timeout_seconds=settings.RCA_RANKER_TIMEOUT_SECONDS)

    async def analyze(self, incident: Incident) -> RCAResult:
        result = RCAResult(incident=incident)
        result.warnings.extend(f"connector '{n}' not loaded: {why}" for n, why in self._registry.config_errors.items())

        collection = await self._collector.collect(incident, parallel=True)
        result.timings_ms["collect"] = collection.elapsed_ms
        result.source_failures = collection.failures
        result.evidence = [
            EvidenceSummary(
                source=e.source,
                source_type=e.source_type,
                record_count=len(e.records),
                truncated=e.truncated,
                elapsed_ms=e.elapsed_ms,
                query_used=e.query_used,
                planned_by=collection.plans[e.source].planned_by,
                plan_note=collection.plans[e.source].note,
            )
            for e in collection.evidence
        ]
        if collection.failures:
            total = len(collection.failures) + len(collection.evidence)
            result.warnings.append(f"partial result: {len(collection.failures)} of {total} sources failed")
        if not collection.evidence:
            result.warnings.append("no evidence could be collected; analysis skipped")
            return result

        started = time.monotonic()
        result.correlation = self._correlator.analyze(incident, collection.evidence, self._baseline_minutes)
        result.timings_ms["correlate"] = int((time.monotonic() - started) * 1000)

        started = time.monotonic()
        try:
            outcome = await self._ranker.rank(incident, result.correlation)
            result.hypotheses = outcome.hypotheses
            result.leading_candidate = outcome.leading_candidate
            result.warnings.extend(outcome.warnings)
        except RankingError as exc:
            logger.warning("Ranking failed", error=str(exc))
            result.warnings.append(f"ranking failed: {exc}")
        result.timings_ms["rank"] = int((time.monotonic() - started) * 1000)

        result.cited_records = self._cited_records(result, collection.evidence)
        return result

    @staticmethod
    def _cited_records(result: RCAResult, evidence: list[Evidence]) -> dict[str, EvidenceRecord]:
        wanted = result.correlation.cited_record_ids() if result.correlation else set()
        for hypothesis in result.hypotheses:
            wanted.update(hypothesis.supporting_evidence_ids)
        return {r.id: r for e in evidence for r in e.records if r.id in wanted}

    async def connector_health(self) -> list[ConnectorHealth]:
        return list(await asyncio.gather(*(c.health() for c in self._registry.all())))
