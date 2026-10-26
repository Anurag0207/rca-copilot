"""
Insight generator — produces a 2-3 sentence plain-English answer to the
user's question, grounded in pre-computed SummaryStats.

A single focused LLM call with max_tokens=150 keeps latency low.
On any failure the best note from SummaryStats is used as the fallback
so the response is never blocked by this optional enrichment.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from app.core.stats import SummaryStats
    from app.llm.base import LLMProviderBase

logger = structlog.get_logger(__name__)

_INSIGHT_PROMPT = """\
You are a data analyst presenting findings to a business executive.

The user asked: "{user_question}"

Top results from the data:
{top_rows}

Key statistics (pre-computed — do not recalculate):
{stats_summary}

Write 2-3 sentences that directly answer the user's question.

Rules:
- State the top finding first with a specific number
- Reference the pre-computed stats where relevant (average, total, share percentage)
- If the metric is non-additive (average/score/rate), compare the top result to the platform average
- Do not say "Based on the data", "The chart shows", "As we can see", or any similar filler opening
- Do not use bullet points or headers — plain prose only
- Keep it under 60 words
- If the stats include notes, incorporate the most interesting one into the response
"""


class InsightGenerator:
    """Generates a short plain-English insight from a validated query result."""

    async def generate(
        self,
        user_question: str,
        rows: list[dict[str, Any]],
        stats: "SummaryStats | None",
        llm: "LLMProviderBase",
    ) -> str:
        """
        Return a 2-3 sentence insight for the result.

        Single-row results get a direct value statement.
        Multi-row results get an LLM-generated insight with stats fallback.
        Empty rows should not reach here (validator catches them first).
        """
        if not rows:
            return "No results were returned for this query."

        if len(rows) == 1:
            return self._single_row_insight(rows[0], stats)

        top_rows_str = _format_top_rows(rows[:5])
        stats_str = _format_stats(stats)

        prompt = _INSIGHT_PROMPT.format(
            user_question=user_question or "your question",
            top_rows=top_rows_str,
            stats_summary=stats_str,
        )

        try:
            raw = await llm.complete(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
            )
            insight = raw.strip()
            if insight:
                word_count = len(insight.split())
                logger.info(
                    "Insight generated",
                    word_count=word_count,
                    used_fallback=False,
                )
                return insight
        except Exception as exc:
            logger.warning("InsightGenerator LLM call failed — using fallback", error=str(exc))

        # Fallback: best note from stats, or a generic summary
        return self._fallback_insight(stats, rows)

    # ── private helpers ───────────────────────────────────────────────────────

    def _single_row_insight(
        self,
        row: dict[str, Any],
        stats: "SummaryStats | None",
    ) -> str:
        if stats and stats.primary_metric:
            val = row.get(stats.primary_metric)
            if val is not None:
                metric_label = stats.primary_metric.replace("_", " ")
                return f"The total {metric_label} is {_fmt(val)}."
        # Generic fallback for single-row with no stats
        pairs = [f"{k}: {v}" for k, v in row.items() if k != "_id" and v is not None]
        return f"Result: {'; '.join(pairs[:3])}."

    def _fallback_insight(
        self,
        stats: "SummaryStats | None",
        rows: list[dict[str, Any]],
    ) -> str:
        if stats and stats.notes:
            return stats.notes[0]
        if stats:
            parts: list[str] = []
            if stats.row_count:
                parts.append(f"{stats.row_count} results returned")
            if stats.max_value is not None and stats.max_label:
                parts.append(
                    f"top: {stats.max_label} at {_fmt(stats.max_value)}"
                )
            if parts:
                return ". ".join(parts) + "."
        return f"{len(rows)} results returned."


# ── formatting helpers ────────────────────────────────────────────────────────

def _format_top_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no rows)"
    cols = list(rows[0].keys())
    header = " | ".join(str(c) for c in cols)
    sep = "-" * len(header)
    lines = [header, sep]
    for row in rows:
        lines.append(" | ".join(_cell(row.get(c)) for c in cols))
    return "\n".join(lines)


def _format_stats(stats: "SummaryStats | None") -> str:
    if stats is None:
        return "(no pre-computed statistics)"
    lines: list[str] = [
        f"- Row count: {stats.row_count}",
        f"- Primary metric: {stats.primary_metric}",
        f"- Is additive (summable): {stats.is_additive}",
    ]
    if stats.total is not None:
        lines.append(f"- Total: {_fmt(stats.total)}")
    if stats.mean is not None:
        lines.append(f"- Mean: {_fmt(stats.mean)}")
    if stats.max_value is not None:
        label_part = f" ({stats.max_label})" if stats.max_label else ""
        lines.append(f"- Maximum: {_fmt(stats.max_value)}{label_part}")
    if stats.min_value is not None:
        label_part = f" ({stats.min_label})" if stats.min_label else ""
        lines.append(f"- Minimum: {_fmt(stats.min_value)}{label_part}")
    if stats.top_share_pct is not None:
        lines.append(f"- Top item share of total: {stats.top_share_pct:.1f}%")
    if stats.pct_above_mean is not None:
        n = round(stats.pct_above_mean * stats.row_count)
        lines.append(f"- Entries above mean: {n} of {stats.row_count}")
    if stats.notes:
        lines.append(f"- Key observation: {stats.notes[0]}")
    return "\n".join(lines)


def _cell(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) >= 1 else str(round(v, 4))
    return str(v)[:30]


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        if v >= 1_000_000:
            return f"{v / 1_000_000:.2f}M"
        if v >= 1_000:
            return f"{v:,.0f}"
        return f"{v:.2f}"
    return str(v)
