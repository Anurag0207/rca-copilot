"""
Result validator — semantic checks on query output before returning to the user.

MongoDB won't raise an error for a query that's syntactically fine but
semantically wrong (grouping by a field that doesn't exist just silently
produces a null-keyed group, not an exception) — something has to notice
that pattern, which is inherently code, not the LLM's job to detect from
scratch on every single query. Three checks run after every successful
MongoDB execution to catch the patterns Mongo itself can't flag:

  Check A — Empty result (row_count == 0)
  Check B — Null grouping/dimension key (the renamed dimension column is None)
  Check C — Shape mismatch (1 row returned but pipeline requested a ranking/top-N)

Deliberately NOT this module's job: diagnosing *why* the pattern occurred or
prescribing a fix. Each check returns only a factual observation (what came
back, which field, how many rows) — never a pre-baked diagnosis like "field X
doesn't exist, use field Y". That reasoning is left entirely to the LLM in
QueryPlanner.plan_with_feedback(), which also gets the previous pipeline and
the schema alongside this evidence — the same way a human debugging this
would look at the query, the schema, and the bad output together, not be
handed the answer.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from app.models.schemas import ParsedIntent, QueryPlan

logger = structlog.get_logger(__name__)


def validate_result(
    rows: list[dict[str, Any]],
    plan: "QueryPlan",
    intent: "ParsedIntent",
) -> tuple[str | None, str | None]:
    """
    Run all three semantic checks on a query result.

    Returns:
        (check_name, evidence) if a check fails — evidence is a factual
        description of what was observed, for the LLM to diagnose itself.
        (None, None) if all checks pass.
    """
    # Check A — empty result
    if len(rows) == 0:
        logger.warning("[VALIDATOR] Check A FAILED: empty result")
        return "empty_result", "The query executed successfully but returned 0 rows."

    # Check B — null grouping/dimension key.
    #
    # The query planner's prompt REQUIRES the last pipeline stage to rename _id
    # to a descriptive field via $project, so a literal "_id" key essentially
    # never survives to this point in a pipeline that followed instructions —
    # checking only for that literal key (the old implementation) made this
    # check permanently dead. Check both: the literal "_id" key, in case a
    # pipeline skipped the required rename, and the renamed dimension column
    # itself (the first column that's never numeric) — that's what actually
    # happens when the model groups by a field that doesn't exist, or isn't
    # populated, in the target collection: every document collapses into one
    # null-keyed group instead of a real breakdown.
    _MISSING = object()
    null_id_rows = [r for r in rows if r.get("_id", _MISSING) is None]
    if null_id_rows:
        logger.warning(
            "[VALIDATOR] Check B FAILED: literal _id left null",
            null_rows=len(null_id_rows),
            sample=rows[:2],
        )
        return "null_grouping_key", (
            f"The query returned {len(rows)} row(s); the field '_id' was null in "
            f"{len(null_id_rows)} of them. Sample row: {null_id_rows[0]}"
        )

    dim_col = _find_dimension_column(rows)
    if dim_col is not None:
        null_dim_rows = [r for r in rows if r.get(dim_col) is None]
        if null_dim_rows:
            logger.warning(
                "[VALIDATOR] Check B FAILED: dimension field null",
                field=dim_col,
                null_rows=len(null_dim_rows),
                sample=rows[:2],
            )
            return "null_grouping_key", (
                f"The query returned {len(rows)} row(s); the field '{dim_col}' was null "
                f"in {len(null_dim_rows)} of them. Sample row: {null_dim_rows[0]}"
            )

    # Check C — shape mismatch: 1 row returned but pipeline has $limit (ranking intent)
    if len(rows) == 1 and _pipeline_has_limit(plan):
        logger.warning(
            "[VALIDATOR] Check C FAILED: shape mismatch — 1 row for ranking query",
            single_row=rows[0],
        )
        return "shape_mismatch", (
            "The pipeline includes a $limit stage, which normally implies a "
            f"ranked/top-N list was intended, but the query returned exactly 1 "
            f"row: {rows[0]}"
        )

    logger.info("[VALIDATOR] All checks passed", row_count=len(rows))
    return None, None


def _find_dimension_column(rows: list[dict[str, Any]]) -> str | None:
    """
    Return the first column that never holds a numeric value across all rows —
    the likely dimension/label column produced by the query planner's mandatory
    $project rename. None when every column is numeric (e.g. a single total).
    """
    if not rows:
        return None
    for key in rows[0]:
        if key == "_id":
            continue
        has_numeric_value = any(
            isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)
            for r in rows
        )
        if not has_numeric_value:
            return key
    return None


def _pipeline_has_limit(plan: "QueryPlan") -> bool:
    """Return True if the aggregation pipeline contains a $limit stage."""
    try:
        plan_data = json.loads(plan.query_string)
        return any("$limit" in stage for stage in plan_data.get("pipeline", []))
    except (json.JSONDecodeError, AttributeError, TypeError):
        return False
