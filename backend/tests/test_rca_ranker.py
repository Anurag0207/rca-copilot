"""Ranker: strict JSON validation of LLM output. The LLM is always mocked."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.llm.json_repair import LLMResponseParseError
from app.rca.correlator import Correlator
from app.rca.ranker import SYSTEM_PROMPT, HypothesisRanker, RankingError, build_ranker_input, parse_ranking
from tests.rca_helpers import evidence, make_incident, record
from tests.test_rca_correlator import logs_with_spike

ALLOWED = {"logs#1", "logs#2", "deploys#0"}


def llm_returning(*responses: str) -> AsyncMock:
    llm = AsyncMock()
    llm.complete.side_effect = list(responses)
    return llm


def payload(*hypotheses: dict) -> str:
    return json.dumps({"hypotheses": list(hypotheses)})


def hyp(title="Bad deploy", confidence=0.8, ids=("deploys#0",), description="because") -> dict:
    return {"title": title, "description": description, "confidence": confidence, "supporting_evidence_ids": list(ids)}


# ── parse_ranking ───────────────────────────────────────────────────────────


def test_valid_response_is_ranked_by_confidence():
    outcome = parse_ranking(payload(hyp("low", 0.2, ["logs#1"]), hyp("high", 0.9, ["deploys#0", "logs#2"])), ALLOWED, 5)
    assert [(h.rank, h.title) for h in outcome.hypotheses] == [(1, "high"), (2, "low")]
    assert outcome.hypotheses[0].supporting_evidence_ids == ["deploys#0", "logs#2"]


def test_markdown_fenced_json_is_accepted():
    outcome = parse_ranking("Here you go:\n```json\n" + payload(hyp()) + "\n```", ALLOWED, 5)
    assert len(outcome.hypotheses) == 1


def test_hypothesis_without_known_evidence_is_rejected():
    outcome = parse_ranking(payload(hyp("made up", 0.9, ["invented#9"]), hyp("real", 0.5, ["logs#1"])), ALLOWED, 5)
    assert [h.title for h in outcome.hypotheses] == ["real"]
    assert any("no supporting evidence" in w for w in outcome.warnings)


def test_unknown_ids_are_stripped_but_valid_ones_kept():
    outcome = parse_ranking(payload(hyp(ids=["invented#9", "logs#1"])), ALLOWED, 5)
    assert outcome.hypotheses[0].supporting_evidence_ids == ["logs#1"]
    assert any("unknown evidence ids" in w for w in outcome.warnings)


def test_empty_citation_list_is_rejected_by_schema():
    outcome = parse_ranking(payload(hyp(ids=[])), ALLOWED, 5)
    assert outcome.hypotheses == [] and any("invalid schema" in w for w in outcome.warnings)


@pytest.mark.parametrize("confidence", [1.5, -0.1, "high", None])
def test_out_of_range_confidence_is_rejected(confidence):
    assert parse_ranking(payload(hyp(confidence=confidence)), ALLOWED, 5).hypotheses == []


@pytest.mark.parametrize("raw", ["not json at all", '{"causes": []}', '{"hypotheses": "none"}', "[1, 2]"])
def test_structurally_invalid_response_raises(raw):
    with pytest.raises(LLMResponseParseError):
        parse_ranking(raw, ALLOWED, 5)


def test_result_is_capped_at_max_hypotheses():
    many = [hyp(f"h{i}", 0.5 + i / 100, ["logs#1"]) for i in range(6)]
    assert len(parse_ranking(payload(*many), ALLOWED, 3).hypotheses) == 3


# ── HypothesisRanker (mocked LLM) ───────────────────────────────────────────


def spike_report():
    rows = logs_with_spike()
    deploy = record("deploys", 0, 1, "v2.4.1 rolled out", None, kind="deploy")
    return Correlator().analyze(make_incident(), [evidence("logs", rows), evidence("deploys", [deploy], "mongodb")], 60)


@pytest.mark.asyncio
async def test_ranker_retries_once_after_invalid_json_then_succeeds():
    report = spike_report()
    cited = sorted(report.cited_record_ids())[0]
    llm = llm_returning("sorry, no JSON", payload(hyp(ids=[cited])))

    outcome = await HypothesisRanker(llm).rank(make_incident(), report)

    assert llm.complete.await_count == 2
    assert outcome.hypotheses[0].supporting_evidence_ids == [cited]
    assert any("attempt 1" in w for w in outcome.warnings)
    # the retry must tell the model what was wrong
    assert "Invalid response" in llm.complete.await_args_list[1].args[0][-1]["content"]


@pytest.mark.asyncio
async def test_ranker_gives_up_with_error_when_llm_never_cites_real_evidence():
    llm = llm_returning(payload(hyp(ids=["invented#1"])), payload(hyp(ids=["invented#2"])))
    with pytest.raises(RankingError):
        await HypothesisRanker(llm).rank(make_incident(), spike_report())
    assert llm.complete.await_count == 2


@pytest.mark.asyncio
async def test_ranker_wraps_llm_outage_in_ranking_error():
    llm = AsyncMock()
    llm.complete.side_effect = RuntimeError("ollama down")
    with pytest.raises(RankingError, match="ollama down"):
        await HypothesisRanker(llm).rank(make_incident(), spike_report())


@pytest.mark.asyncio
async def test_ranker_skips_llm_when_there_is_nothing_to_cite():
    llm = AsyncMock()
    empty = Correlator().analyze(make_incident(), [evidence("logs", [record("logs", 0, 1, "fine")])], 60)
    outcome = await HypothesisRanker(llm).rank(make_incident(), empty)
    assert outcome.hypotheses == [] and llm.complete.await_count == 0


def test_ranker_prompt_input_exposes_ids_and_no_raw_logs():
    report = spike_report()
    text = build_ranker_input(make_incident(), report)
    parsed = json.loads(text)
    assert parsed["timeline"][0]["record_ids"] or parsed["error_signatures"][0]["evidence_ids"]
    assert "request ok" not in text  # raw baseline log lines never reach the LLM


# ── leading candidate verdict and call timeout ──────────────────────────────


def with_candidate(verdict="accepted", **kw) -> str:
    body = json.loads(payload(hyp(**kw)))
    body["leading_candidate"] = {"what": "deploy v2", "verdict": verdict, "reason": "preceded the spike"}
    return json.dumps(body)


def test_leading_candidate_verdict_is_parsed():
    outcome = parse_ranking(with_candidate("rejected"), ALLOWED, 5)
    assert outcome.leading_candidate == {"what": "deploy v2", "verdict": "rejected", "reason": "preceded the spike"}
    assert not any("verdict" in w for w in outcome.warnings)


@pytest.mark.parametrize("candidate", [{"what": "x", "verdict": "maybe", "reason": "r"}, "accepted"])
def test_malformed_verdict_warns_but_keeps_hypotheses(candidate):
    body = json.loads(payload(hyp()))
    body["leading_candidate"] = candidate
    outcome = parse_ranking(json.dumps(body), ALLOWED, 5)
    assert outcome.leading_candidate is None and outcome.hypotheses
    assert any("malformed leading_candidate" in w for w in outcome.warnings)


def test_absent_verdict_is_silent_because_the_prompt_does_not_ask_for_one():
    outcome = parse_ranking(payload(hyp()), ALLOWED, 5)
    assert outcome.leading_candidate is None and outcome.warnings == []


def test_ranker_input_exposes_change_proximity():
    deploy = record("deploys", 0, 1, "v2 rolled out", None, kind="deploy")
    report = Correlator().analyze(make_incident(), [evidence("logs", logs_with_spike()), evidence("deploys", [deploy], "mongodb")], 60)
    change = next(e for e in json.loads(build_ranker_input(make_incident(), report))["timeline"] if e["kind"] == "change")
    assert change["nearest_preceding_change"] is True and change["followed_by_anomaly"] is True and change["lead_seconds"] > 0


@pytest.mark.asyncio
async def test_timed_out_ranker_call_is_retried_once_with_a_warning():
    import asyncio

    report = spike_report()
    cited = sorted(report.cited_record_ids())[0]
    calls = {"n": 0}

    async def flaky(messages, max_tokens=0, model=None):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(5)
        return payload(hyp(ids=[cited]))

    llm = AsyncMock()
    llm.complete.side_effect = flaky
    outcome = await HypothesisRanker(llm, call_timeout_seconds=0.1).rank(make_incident(), report)

    assert calls["n"] == 2 and outcome.hypotheses
    assert any("timed out after 0.1s; retrying once" in w for w in outcome.warnings)


@pytest.mark.asyncio
async def test_ranker_fails_clearly_after_two_timeouts():
    import asyncio

    async def hang(messages, max_tokens=0, model=None):
        await asyncio.sleep(5)

    llm = AsyncMock()
    llm.complete.side_effect = hang
    with pytest.raises(RankingError, match="timed out twice"):
        await HypothesisRanker(llm, call_timeout_seconds=0.05).rank(make_incident(), spike_report())


def test_prompt_offers_no_example_category_list_the_model_could_parrot():
    # Regression guard: an earlier prompt listed trigger categories and the model returned them verbatim as titles
    # ("Resource Limit Change"), dropping bad_deploy_pool from 4/5 to 1/5. See docs/failure_analysis.md.
    prompt = SYSTEM_PROMPT.lower()
    for phrase in ("resource limit", "expired credential", "failing dependency", "capacity exhaustion"):
        assert phrase not in prompt
