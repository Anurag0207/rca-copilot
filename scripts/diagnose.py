#!/usr/bin/env python3
"""
Stage-by-stage diagnosis of one analysis per scenario. Saves, per scenario, to build/diagnosis/<name>.json:

  planner      fragment, planned_by and note per source (what the LLM chose to fetch)
  fetched      records returned per source, and the query actually sent
  correlator   the full CorrelationReport
  ranker_input the exact text the ranker LLM sees
  raw_output   the raw ranker LLM response, and the parsed hypotheses
  visibility   whether the ground-truth topic was present at each stage (fetched / ranker input)

"Visible" means the scenario's first ground-truth keyword group occurs in the text of that stage's data.
It is a diagnostic aid for deciding which stage lost the answer, not an accuracy metric.

  python scripts/diagnose.py cpu_throttling_metrics_only traffic_surge
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import evaluate  # noqa: F401  (sets sys.path/env defaults; reuses connector_config)
import eval_lib
import scenarios
import seed_scenarios
from app.config import get_settings
from app.connectors.registry import EvidenceRegistry
from app.llm.json_repair import LLMResponseParseError
from app.llm.ollama_provider import OllamaProvider
from app.rca import ranker as ranker_module
from app.rca.collector import EvidenceCollector, QueryPlanner
from app.rca.correlator import Correlator
from app.rca.models import Incident

OUT = Path(__file__).resolve().parents[1] / "build" / "diagnosis"


def record_text(evidence) -> str:
    chunks = []
    for ev in evidence:
        for r in ev.records:
            chunks.append(f"{r.message} {r.attributes.get('kind', '')} {r.attributes.get('metric', '')}")
    return eval_lib.normalize_text(" ".join(chunks))


async def diagnose(scenario, settings, llm, es, mongo, scale: int) -> dict:
    seed_scenarios.seed_scenario(scenario, scale, es, mongo)
    cfg = settings.model_copy(update={"RCA_CONNECTORS": evaluate.connector_config(scenario)})
    registry = EvidenceRegistry.from_settings(cfg)
    incident = Incident(service=scenario.service, start=scenario.start, end=scenario.end, symptom=scenario.symptom)
    truth = scenario.ground_truth
    try:
        collector = EvidenceCollector(registry, QueryPlanner(llm), cfg.RCA_CONNECTOR_TIMEOUT_SECONDS, cfg.RCA_BASELINE_MINUTES, cfg.RCA_MAX_RECORDS_PER_SOURCE)
        collection = await collector.collect(incident)
        report = Correlator(zscore_threshold=cfg.RCA_ZSCORE_THRESHOLD).analyze(incident, collection.evidence, cfg.RCA_BASELINE_MINUTES)

        ranker_input = ranker_module.build_ranker_input(incident, report)
        messages = [
            {"role": "system", "content": ranker_module.SYSTEM_PROMPT.format(max_hypotheses=5)},
            {"role": "user", "content": ranker_input},
        ]
        raw = await llm.complete(messages, max_tokens=1500)
        allowed = report.cited_record_ids()
        try:
            parsed = [h.model_dump() for h in ranker_module.parse_ranking(raw, allowed, 5).hypotheses]
        except LLMResponseParseError as exc:
            parsed = [{"parse_error": str(exc)}]
        records = {r.id: r for e in collection.evidence for r in e.records}
        rank = eval_lib.first_correct_rank([h for h in parsed if "title" in h], truth.keywords, truth.expected_sources, records)

        topic = truth.keywords[0]
        by_source = {e.source: e for e in collection.evidence}
        return {
            "scenario": scenario.name,
            "ground_truth": {"label": truth.label, "keywords": list(truth.keywords), "expected_sources": list(truth.expected_sources)},
            "planner": {n: {"fragment": p.fragment, "planned_by": p.planned_by, "note": p.note} for n, p in collection.plans.items()},
            "fetched": {n: {"records": len(e.records), "truncated": e.truncated, "query_used": e.query_used[:1500]} for n, e in by_source.items()},
            "failures": collection.failures,
            "correlator": json.loads(report.model_dump_json()),
            "ranker_input": ranker_input,
            "raw_output": raw,
            "parsed_hypotheses": parsed,
            "rank_of_correct": rank,
            "visibility": {
                "topic_keyword": topic,
                "topic_in_fetched_expected_sources": eval_lib.keyword_present(record_text([e for e in collection.evidence if e.source in truth.expected_sources]), topic),
                "topic_in_ranker_input": eval_lib.keyword_present(eval_lib.normalize_text(ranker_input), topic),
                "keywords_missing_from_ranker_input": eval_lib.missing_keywords(ranker_input, truth.keywords),
            },
        }
    finally:
        await registry.close_all()
        seed_scenarios.drop_scenario(scenario, es, mongo)


async def main() -> int:
    names = sys.argv[1:] or [s.name for s in scenarios.load_all()]
    settings = get_settings().model_copy(update={"RCA_MAX_RECORDS_PER_SOURCE": 10_000, "RCA_CONNECTOR_TIMEOUT_SECONDS": 180.0})
    llm, es, mongo = OllamaProvider(settings), seed_scenarios.es_client(), seed_scenarios.mongo_client()
    OUT.mkdir(parents=True, exist_ok=True)
    for name in names:
        result = await diagnose(scenarios.get(name), settings, llm, es, mongo, seed_scenarios.DEFAULT_SCALE)
        (OUT / f"{name}.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"{name}: rank_of_correct={result['rank_of_correct']} visibility={result['visibility']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
