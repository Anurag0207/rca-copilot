"""
LLM hypothesis ranking with strict output validation.

The LLM sees only the correlator's compact report (never raw logs) and must cite
evidence ids from it. Anything it returns is untrusted: the response is parsed,
schema-validated, and every hypothesis must cite at least one id that actually
exists in the report, otherwise it is rejected.

Confidence is the model's own estimate, not a calibrated probability.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import structlog
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.llm.base import LLMProviderBase
from app.llm.json_repair import LLMResponseParseError, escape_literal_newlines, extract_json_object
from app.rca.models import CorrelationReport, Hypothesis, Incident

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are a senior site reliability engineer performing root cause analysis.
You are given an incident and a deterministic analysis of evidence from several sources
(error spikes, error signatures, metric anomalies and a time-aligned timeline).

Rules:
- Propose up to {max_hypotheses} candidate ROOT causes, not symptoms. Prefer causes that
  precede the first anomaly (see offset_seconds) over effects that follow it.
- Every hypothesis MUST cite supporting_evidence_ids copied EXACTLY from the "evidence_ids"
  or "record_ids" fields in the input. Never invent ids. A hypothesis without valid ids is discarded.
- confidence is a number between 0 and 1.
- Respond with ONLY this JSON object, no prose, no markdown:
{{"hypotheses": [{{"title": "short cause", "description": "why the evidence points here and how it led to the symptom", "confidence": 0.0, "supporting_evidence_ids": ["id"]}}]}}"""


class RankingError(Exception):
    """The LLM never produced a valid, evidence-backed ranking."""


class _LLMHypothesis(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence_ids: list[str] = Field(min_length=1)


class _LeadingCandidate(BaseModel):
    what: str = Field(min_length=1)
    verdict: Literal["accepted", "rejected"]
    reason: str = Field(min_length=1)


@dataclass
class RankingOutcome:
    hypotheses: list[Hypothesis] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    leading_candidate: dict | None = None


def build_ranker_input(incident: Incident, report: CorrelationReport) -> str:
    """Compact JSON view of the report: the only evidence the LLM gets."""

    def ts(value) -> str:
        return value.strftime("%H:%M:%S")

    payload = {
        "incident": {
            "service": incident.service,
            "symptom": incident.symptom,
            "window": f"{ts(incident.start)} to {ts(incident.end)} UTC",
        },
        "first_anomaly_at": ts(report.first_anomaly_at) if report.first_anomaly_at else None,
        "error_spikes": [
            {
                "source": s.source,
                "onset": ts(s.onset),
                "peak_per_min": s.peak_count,
                "baseline_per_min": s.baseline_mean,
                "zscore": s.peak_zscore,
            }
            for s in report.error_spikes
        ],
        "error_signatures": [
            {
                "signature": s.signature,
                "incident_count": s.incident_count,
                "baseline_count": s.baseline_count,
                "novel": s.novel,
                "first_seen": ts(s.first_seen),
                "evidence_ids": s.sample_record_ids,
            }
            for s in report.signatures
        ],
        "metric_anomalies": [
            {
                "source": a.source,
                "metric": a.metric,
                "baseline_mean": a.baseline_mean,
                "peak": a.peak_value,
                "zscore": a.peak_zscore,
                "first_anomalous_at": ts(a.first_anomalous_at),
                "evidence_ids": a.sample_record_ids,
            }
            for a in report.metric_anomalies
        ],
        "timeline": [
            {
                "time": ts(e.timestamp),
                "offset_seconds": e.offset_seconds,
                "source": e.source,
                "kind": e.kind,
                "what": e.description,
                "record_ids": e.record_ids,
                **(
                    {
                        "lead_seconds": e.lead_seconds,
                        "followed_by_anomaly": e.followed_by_anomaly,
                        "nearest_preceding_change": e.nearest_preceding_change,
                    }
                    if e.kind == "change"
                    else {}
                ),
            }
            for e in report.timeline
        ],
    }
    return json.dumps(payload, indent=1)


def parse_ranking(raw: str, allowed_ids: set[str], max_hypotheses: int) -> RankingOutcome:
    """
    Validate a raw LLM response into ranked hypotheses.

    Raises:
        LLMResponseParseError: the response is not JSON or lacks a 'hypotheses' list.
    Hypotheses that violate the schema or cite no known evidence are dropped with a warning.
    """
    try:
        payload = json.loads(escape_literal_newlines(extract_json_object(raw)))
    except json.JSONDecodeError as exc:
        raise LLMResponseParseError(f"response is not valid JSON: {exc}") from exc
    items = payload.get("hypotheses") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise LLMResponseParseError("response must be an object with a 'hypotheses' list")

    outcome = RankingOutcome()
    # Optional: the prompt does not currently request it (see docs/failure_analysis.md), but a model
    # that volunteers a verdict has it validated and surfaced; a malformed one is flagged, an absent one is not.
    if payload.get("leading_candidate") is not None:
        try:
            outcome.leading_candidate = _LeadingCandidate.model_validate(payload["leading_candidate"]).model_dump()
        except ValidationError:
            outcome.warnings.append("ranker gave a malformed leading_candidate verdict; ignored")
    accepted: list[_LLMHypothesis] = []
    for index, item in enumerate(items):
        try:
            candidate = _LLMHypothesis.model_validate(item)
        except ValidationError as exc:
            outcome.warnings.append(f"hypothesis #{index + 1} rejected: invalid schema ({exc.errors()[0]['msg']})")
            continue

        valid_ids = [i for i in dict.fromkeys(candidate.supporting_evidence_ids) if i in allowed_ids]
        if len(valid_ids) < len(set(candidate.supporting_evidence_ids)):
            outcome.warnings.append(f"hypothesis '{candidate.title}': dropped citations to unknown evidence ids")
        if not valid_ids:
            outcome.warnings.append(f"hypothesis '{candidate.title}' rejected: no supporting evidence")
            continue
        candidate.supporting_evidence_ids = valid_ids
        accepted.append(candidate)

    accepted.sort(key=lambda h: h.confidence, reverse=True)
    outcome.hypotheses = [
        Hypothesis(rank=rank, **h.model_dump()) for rank, h in enumerate(accepted[:max_hypotheses], start=1)
    ]
    return outcome


class HypothesisRanker:
    def __init__(
        self,
        llm: LLMProviderBase,
        max_hypotheses: int = 5,
        max_attempts: int = 2,
        max_tokens: int = 1500,
        call_timeout_seconds: float = 90.0,
    ) -> None:
        self._llm = llm
        self._call_timeout = call_timeout_seconds
        self._max_hypotheses = max_hypotheses
        self._max_attempts = max_attempts
        self._max_tokens = max_tokens

    async def _complete(self, messages: list[dict], warnings: list[str]) -> str:
        """One LLM call bounded by the timeout; a timed-out call is retried exactly once."""
        for call in (1, 2):
            try:
                return await asyncio.wait_for(self._llm.complete(messages, max_tokens=self._max_tokens), self._call_timeout)
            except asyncio.TimeoutError:
                suffix = "; retrying once" if call == 1 else ""
                warnings.append(f"ranker LLM call timed out after {self._call_timeout:g}s{suffix}")
        raise RankingError(f"ranker LLM timed out twice ({self._call_timeout:g}s each); {'; '.join(warnings)}")

    async def rank(self, incident: Incident, report: CorrelationReport) -> RankingOutcome:
        allowed_ids = report.cited_record_ids()
        if not allowed_ids:
            # Nothing citable means nothing the LLM could ground a cause in.
            return RankingOutcome(warnings=["no evidence anomalies were detected; no hypotheses generated"])

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT.format(max_hypotheses=self._max_hypotheses)},
            {"role": "user", "content": build_ranker_input(incident, report)},
        ]
        warnings: list[str] = []
        last_problem = "no attempt made"

        for attempt in range(1, self._max_attempts + 1):
            try:
                raw = await self._complete(messages, warnings)
                outcome = parse_ranking(raw, allowed_ids, self._max_hypotheses)
            except LLMResponseParseError as exc:
                last_problem = str(exc)
                warnings.append(f"attempt {attempt}: {exc}")
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": f"Invalid response: {exc}. Reply again with ONLY the JSON object."},
                ]
                continue
            except RuntimeError as exc:
                raise RankingError(f"LLM call failed: {exc}") from exc

            warnings.extend(outcome.warnings)
            if outcome.hypotheses:
                outcome.warnings = warnings
                return outcome
            last_problem = "no hypothesis cited valid evidence"
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "No hypothesis cited valid evidence ids. Cite ids copied exactly from the input."},
            ]

        raise RankingError(f"no valid ranking after {self._max_attempts} attempts ({last_problem}); {'; '.join(warnings)}")
