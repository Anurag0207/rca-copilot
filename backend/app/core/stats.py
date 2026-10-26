"""
Summary statistics for query results.

compute_stats() inspects the rows returned by a MongoDB aggregation and
produces a SummaryStats object with pre-computed numbers and 1-2 plain
English observations.  It is intentionally conservative: no stats are
produced when they would be meaningless (single row, no numeric data,
boolean flags, averages that lack a meaningful share-of-total).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

_ADDITIVE_KEYWORDS = {"revenue", "total", "sum", "count", "sales", "amount", "price", "spend"}
_NON_ADDITIVE_KEYWORDS = {"avg", "average", "mean", "score", "rate", "ratio", "pct", "percent"}


@dataclass
class SummaryStats:
    row_count: int
    primary_metric: str
    total: float | None
    mean: float | None
    min_value: float | None
    min_label: str | None
    max_value: float | None
    max_label: str | None
    pct_above_mean: float | None  # fraction of rows where metric > mean
    top_share_pct: float | None   # top row's value as % of total (additive only)
    is_additive: bool
    notes: list[str] = field(default_factory=list)


def compute_stats(rows: list[dict[str, Any]]) -> SummaryStats | None:
    """
    Compute summary statistics for a query result.

    Returns None when stats would be meaningless:
      - 0 or 1 rows
      - no numeric columns
      - all columns are non-numeric strings
    """
    if len(rows) <= 1:
        return None

    numeric_cols = _find_numeric_columns(rows)
    if not numeric_cols:
        return None

    label_col = _find_label_column(rows, numeric_cols)
    primary_metric = _pick_primary_metric(numeric_cols)
    is_additive = _is_additive_metric(primary_metric)

    values = [_to_float(r.get(primary_metric)) for r in rows]
    values = [v for v in values if v is not None]
    if not values:
        return None

    total = sum(values) if is_additive else None
    mean = sum(values) / len(values)
    min_v = min(values)
    max_v = max(values)

    # Labels for min/max rows
    min_label: str | None = None
    max_label: str | None = None
    if label_col:
        for r in rows:
            v = _to_float(r.get(primary_metric))
            if v is not None:
                if v == min_v and min_label is None:
                    min_label = _label_str(r.get(label_col))
                if v == max_v and max_label is None:
                    max_label = _label_str(r.get(label_col))

    # % of rows above mean
    above = sum(1 for v in values if v > mean)
    pct_above_mean = above / len(values) if len(values) > 1 else None

    # Top share only makes sense for additive metrics with a non-zero total
    top_share_pct: float | None = None
    if is_additive and total and total != 0:
        top_share_pct = (max_v / total) * 100

    notes = _generate_notes(
        rows=rows,
        primary_metric=primary_metric,
        label_col=label_col,
        values=values,
        mean=mean,
        max_v=max_v,
        max_label=max_label,
        total=total,
        top_share_pct=top_share_pct,
        is_additive=is_additive,
        pct_above_mean=pct_above_mean,
    )

    return SummaryStats(
        row_count=len(rows),
        primary_metric=primary_metric,
        total=round(total, 2) if total is not None else None,
        mean=round(mean, 4),
        min_value=round(min_v, 4),
        min_label=min_label,
        max_value=round(max_v, 4),
        max_label=max_label,
        pct_above_mean=round(pct_above_mean, 4) if pct_above_mean is not None else None,
        top_share_pct=round(top_share_pct, 2) if top_share_pct is not None else None,
        is_additive=is_additive,
        notes=notes,
    )


# ── helpers ──────────────────────────────────────────────────────────────────

def _find_numeric_columns(rows: list[dict]) -> list[str]:
    """Return column names whose values are numeric in at least one row."""
    cols: list[str] = []
    if not rows:
        return cols
    for key in rows[0]:
        if key == "_id":
            continue
        for r in rows:
            v = r.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cols.append(key)
                break
    return cols


def _find_label_column(rows: list[dict], numeric_cols: list[str]) -> str | None:
    """Return the first non-numeric column (likely the _id / dimension label)."""
    if not rows:
        return None
    for key in rows[0]:
        if key not in numeric_cols:
            return key
    return None


def _pick_primary_metric(numeric_cols: list[str]) -> str:
    """
    Choose the primary metric column.

    Preference order:
    1. Non-additive measure column (avg, score, rate, mean) — this is what the user
       asked about; supplementary counts in the same result are secondary
    2. Additive measure column (revenue, total, sum, amount, sales, spend)
    3. First numeric column that is not a plain "count" column
    4. First numeric column
    """
    # Prefer avg/score/rate — the measure the user actually asked about
    for col in numeric_cols:
        lc = col.lower()
        if any(kw in lc for kw in _NON_ADDITIVE_KEYWORDS):
            return col
    # Then revenue/total/sum
    for col in numeric_cols:
        lc = col.lower()
        if any(kw in lc for kw in _ADDITIVE_KEYWORDS) and "count" not in lc:
            return col
    # Then any additive (including count columns)
    for col in numeric_cols:
        lc = col.lower()
        if any(kw in lc for kw in _ADDITIVE_KEYWORDS):
            return col
    return numeric_cols[0]


def _is_additive_metric(col_name: str) -> bool:
    """
    Return True when summing the column across rows yields a meaningful total.

    Revenue, count, sales → additive (share-of-total is meaningful).
    Averages, scores, rates → non-additive (share-of-total is nonsense).
    """
    lc = col_name.lower()
    if any(kw in lc for kw in _NON_ADDITIVE_KEYWORDS):
        return False
    # Default: treat as additive unless name suggests otherwise
    return True


def _to_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and not math.isnan(v) and not math.isinf(v):
        return float(v)
    return None


def _label_str(v: Any) -> str | None:
    if v is None:
        return None
    return str(v)[:60]


def _generate_notes(
    *,
    rows: list[dict],
    primary_metric: str,
    label_col: str | None,
    values: list[float],
    mean: float,
    max_v: float,
    max_label: str | None,
    total: float | None,
    top_share_pct: float | None,
    is_additive: bool,
    pct_above_mean: float | None,
) -> list[str]:
    notes: list[str] = []

    # Note 1 — top item's share of total (additive only)
    if top_share_pct is not None and max_label and top_share_pct >= 10:
        metric_display = _human_metric_short(primary_metric)
        notes.append(
            f"{max_label} accounts for {top_share_pct:.1f}% of total {metric_display}"
        )

    # Note 2 — top vs. mean ratio (ranking context)
    if mean > 0 and max_v > mean * 1.5 and max_label:
        ratio = max_v / mean
        metric_display = _human_metric_short(primary_metric)
        notes.append(
            f"Top performer is {ratio:.1f}x above the platform average {metric_display}"
            if not is_additive
            else f"{max_label} is {ratio:.1f}x the average {metric_display}"
        )

    # Note 3 — % above mean (interesting when skewed)
    if pct_above_mean is not None and is_additive and not notes:
        n_above = round(pct_above_mean * len(values))
        metric_display = _human_metric_short(primary_metric)
        if n_above < len(values):
            notes.append(
                f"{n_above} of {len(values)} entries exceed the average {metric_display} "
                f"of {_fmt_number(mean)}"
            )

    # Non-additive: note platform average and spread if no note yet
    if not is_additive and not notes:
        metric_display = _human_metric_short(primary_metric)
        if pct_above_mean is not None:
            n_above = round(pct_above_mean * len(values))
            notes.append(
                f"{n_above} of {len(values)} entries score above the platform average "
                f"{metric_display} of {_fmt_number(mean)}"
            )

    return notes[:2]  # cap at 2 notes


def _human_metric(col_name: str) -> str:
    """Convert snake_case column name to a readable label."""
    return col_name.replace("_", " ")


def _human_metric_short(col_name: str) -> str:
    """Readable label without redundant leading aggregation words.

    Strips 'total ' → avoids 'of total total_revenue'.
    Strips 'avg '   → avoids 'average avg_score'.
    """
    label = col_name.replace("_", " ")
    for prefix in ("total ", "avg ", "sum "):
        if label.startswith(prefix):
            label = label[len(prefix):]
            break
    return label


def _fmt_number(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}k"
    return f"{v:.2f}"
