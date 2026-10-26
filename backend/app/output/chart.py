"""
Chart renderer — converts a QueryResult into a PNG/JPEG chart image.

Supported chart types (ChartType enum):
  BAR   — vertical bar chart (auto-flips horizontal when labels are long)
  LINE  — line chart (time-series or sequential)
  PIE   — pie / donut chart with legend (proportions)
  TABLE — styled tabular grid rendered as an image

All rendering is synchronous via matplotlib and offloaded to a thread-pool
executor to avoid blocking the async event loop.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

from app.models.schemas import ChartType

if TYPE_CHECKING:
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure

    import pandas as pd

    from app.mcp.base import QueryResult

logger = structlog.get_logger(__name__)

# ── Dark-theme palette (matches the frontend CSS variables) ───────────────────
_BG    = "#0f0f1c"   # figure + axes background
_TEXT  = "#b4b4cc"   # tick labels, axis labels
_TITLE = "#e2e2f0"   # chart title
_GRID  = "#1e1e2e"   # grid lines
_SPINE = "#2a2a3e"   # axis borders
_BAR   = "#7c3aed"   # primary bar fill
_LINE_COLORS = ["#7c3aed", "#06b6d4", "#10b981", "#f59e0b", "#ef4444"]

# Auto-flip to horizontal bars when average label length exceeds this
_HORIZ_THRESHOLD = 14

# Strip these filler prefixes from chart titles
_FILLER_RE = re.compile(
    r"^(tell me |show me |give me |list |what is the |what are the |"
    r"what is |what are |find |get |display |provide |i want to see )",
    re.IGNORECASE,
)


class ChartRenderError(Exception):
    """Raised when chart rendering fails (empty data, incompatible types, etc.)."""


class ChartRenderer:
    """
    Renders a QueryResult as a chart image using matplotlib.

    Design notes:
      - All matplotlib calls are synchronous; run via run_in_executor to avoid
        blocking the event loop.
      - Each render call creates a new Figure and closes it immediately after
        saving, preventing memory leaks.
      - Figure + axes styling is applied directly to the Figure/Axes objects,
        not via plt.rcParams, so concurrent renders from different threads
        cannot interfere with each other.
    """

    DEFAULT_DPI: int = 150

    _DATE_KEYWORDS = ("date", "year", "month", "time", "day", "quarter", "week", "period")

    @staticmethod
    def pick_chart_type(result: "QueryResult", intent_type: "ChartType | None") -> "ChartType":
        """
        Choose an appropriate chart type for the result when the user did not
        explicitly request one.

        Rules (in priority order):
          1. Intent type set → use it directly
          2. 0 or 1 rows → TABLE
          3. Any column name contains a date/time keyword → LINE
          4. 2-20 rows → BAR (handles both short and long-label cases via auto-flip)
          5. >20 rows → TABLE
        """
        if intent_type is not None:
            return intent_type
        if not result.rows:
            return ChartType.TABLE
        n = len(result.rows)
        if n == 1:
            return ChartType.TABLE
        cols_lower = [c.lower() for c in result.columns]
        if any(any(kw in c for kw in ChartRenderer._DATE_KEYWORDS) for c in cols_lower):
            return ChartType.LINE
        if n <= 20:
            return ChartType.BAR
        return ChartType.TABLE

    async def render(
        self,
        result: "QueryResult",
        chart_type: ChartType,
        title: str,
        output_path: Path,
        image_format: Literal["png", "jpeg"] = "png",
    ) -> Path:
        if not result.rows:
            raise ChartRenderError("Cannot render chart: QueryResult contains no rows")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._render_sync,
            result,
            chart_type,
            title,
            output_path,
            image_format,
        )
        logger.info("Chart rendered", chart_type=chart_type.value, path=str(output_path))
        return output_path

    def _render_sync(
        self,
        result: "QueryResult",
        chart_type: ChartType,
        title: str,
        output_path: Path,
        image_format: str,
    ) -> None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig: "Figure | None" = None
        try:
            # Dynamic figure size — computed inside try so any error is captured
            n_rows = len(result.rows)
            if chart_type == ChartType.BAR and n_rows > 0 and result.columns:
                # Use non-numeric column for labels (handles any column order)
                dim_col = _pick_dim_col(result)
                labels = [str(r.get(dim_col, "")) for r in result.rows]
                if _avg_label_len(labels) > _HORIZ_THRESHOLD:
                    fig_h = max(5.5, n_rows * 0.48 + 1.8)
                    fig_w = 12
                else:
                    fig_h = 6
                    fig_w = max(10, min(16, n_rows * 1.1 + 3))
            else:
                fig_w, fig_h = 12, 6

            fig, ax = plt.subplots(figsize=(fig_w, fig_h))
            fig.patch.set_facecolor(_BG)
            ax.set_facecolor(_BG)

            cleaned_title = _clean_title(title)
            dispatch = {
                ChartType.BAR:   self._render_bar,
                ChartType.LINE:  self._render_line,
                ChartType.PIE:   self._render_pie,
                ChartType.TABLE: self._render_table,
            }
            render_fn = dispatch.get(chart_type)
            if render_fn is None:
                raise ChartRenderError(f"Unsupported chart type: {chart_type}")

            render_fn(ax, result, cleaned_title)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(
                str(output_path),
                format=image_format,
                dpi=self.DEFAULT_DPI,
                bbox_inches="tight",
                facecolor=_BG,
            )
        finally:
            if fig is not None:
                plt.close(fig)

    # ── Chart-type renderers ──────────────────────────────────────────────────

    def _render_bar(self, ax: "Axes", result: "QueryResult", title: str) -> None:
        import matplotlib.ticker as mticker

        df = self._dataframe_from_result(result)
        if df.shape[1] < 2:
            raise ChartRenderError("Bar chart requires at least 2 columns")

        x_col, y_col = _pick_xy(df)
        labels = [str(v) for v in df[x_col]]
        values = df[y_col].astype(float)
        max_val = float(values.max()) if len(values) else 1.0

        _style_axes(ax)

        if _avg_label_len(labels) > _HORIZ_THRESHOLD:
            # ── Horizontal bars (long labels) ─────────────────────────────────
            y_pos = list(range(len(labels)))
            bars = ax.barh(
                [_truncate(l, 35) for l in labels[::-1]],
                values.tolist()[::-1],
                color=_BAR, alpha=0.88, height=0.62,
            )
            ax.set_xlabel(_label(y_col), color=_TEXT, fontsize=11)
            ax.xaxis.set_major_formatter(
                mticker.FuncFormatter(lambda x, _: _fmt_val(x))
            )
            ax.grid(axis="x", color=_GRID, linewidth=0.5, zorder=0)
            ax.tick_params(axis="y", labelcolor=_TEXT, labelsize=9)
            ax.tick_params(axis="x", labelcolor=_TEXT, labelsize=9)
            # Value annotations at bar end
            for bar in bars:
                w = bar.get_width()
                ax.text(
                    w + max_val * 0.012,
                    bar.get_y() + bar.get_height() / 2,
                    _fmt_val(w),
                    va="center", ha="left",
                    color=_TEXT, fontsize=8.5,
                )
        else:
            # ── Vertical bars (short labels) ──────────────────────────────────
            bars = ax.bar(
                range(len(labels)),
                values.tolist(),
                color=_BAR, alpha=0.88, width=0.62,
                zorder=3,
            )
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(
                [_truncate(l, 22) for l in labels],
                rotation=40, ha="right", fontsize=9, color=_TEXT,
            )
            ax.set_xlabel(_label(x_col), color=_TEXT, fontsize=11)
            ax.set_ylabel(_label(y_col), color=_TEXT, fontsize=11)
            ax.yaxis.set_major_formatter(
                mticker.FuncFormatter(lambda x, _: _fmt_val(x))
            )
            ax.grid(axis="y", color=_GRID, linewidth=0.5, zorder=0)
            ax.tick_params(axis="y", labelcolor=_TEXT, labelsize=9)
            # Value annotations on top
            for bar in bars:
                h = bar.get_height()
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    h + max_val * 0.012,
                    _fmt_val(h),
                    ha="center", va="bottom",
                    color=_TEXT, fontsize=8.5,
                )

        ax.set_title(title, color=_TITLE, fontsize=13, fontweight="bold", pad=14)

    def _render_line(self, ax: "Axes", result: "QueryResult", title: str) -> None:
        df = self._dataframe_from_result(result)
        if df.shape[1] < 2:
            raise ChartRenderError("Line chart requires at least 2 columns")

        x_col, _ = _pick_xy(df)
        y_cols = [c for c in df.columns if c != x_col]
        x_vals = [str(v) for v in df[x_col]]

        _style_axes(ax)
        ax.grid(axis="y", color=_GRID, linewidth=0.5, zorder=0)

        for i, y_col in enumerate(y_cols):
            color = _LINE_COLORS[i % len(_LINE_COLORS)]
            ax.plot(
                range(len(x_vals)),
                df[y_col].astype(float),
                marker="o", markersize=5, linewidth=2,
                color=color, label=_label(str(y_col)),
                zorder=3,
            )

        ax.set_xticks(range(len(x_vals)))
        ax.set_xticklabels(
            [_truncate(v, 20) for v in x_vals],
            rotation=40, ha="right", fontsize=9, color=_TEXT,
        )
        ax.set_xlabel(_label(x_col), color=_TEXT, fontsize=11)
        ax.tick_params(axis="y", labelcolor=_TEXT, labelsize=9)
        ax.set_title(title, color=_TITLE, fontsize=13, fontweight="bold", pad=14)
        if len(y_cols) > 1:
            ax.legend(facecolor=_BG, edgecolor=_SPINE, labelcolor=_TEXT, fontsize=9)

    def _render_pie(self, ax: "Axes", result: "QueryResult", title: str) -> None:
        import matplotlib.pyplot as plt

        df = self._dataframe_from_result(result)
        if df.shape[1] < 2:
            raise ChartRenderError("Pie chart requires at least 2 columns")

        x_col, y_col = _pick_xy(df)
        labels = [str(v) for v in df[x_col]]
        values = df[y_col].astype(float)
        # Purple-to-cyan gradient palette
        colors = plt.cm.cool([i / max(len(labels) - 1, 1) * 0.7 + 0.15 for i in range(len(labels))])

        wedges, _, autotexts = ax.pie(
            values, labels=None, autopct="%1.1f%%", startangle=140,
            colors=colors, pctdistance=0.82,
            wedgeprops={"linewidth": 0.8, "edgecolor": _BG},
        )
        for at in autotexts:
            at.set_color(_TEXT)
            at.set_fontsize(8)

        ax.legend(
            wedges, [_truncate(l, 30) for l in labels],
            loc="center left", bbox_to_anchor=(1.0, 0.5),
            facecolor=_BG, edgecolor=_SPINE, labelcolor=_TEXT, fontsize=8.5,
        )
        ax.set_facecolor(_BG)
        ax.set_title(title, color=_TITLE, fontsize=13, fontweight="bold", pad=14)

    def _render_table(self, ax: "Axes", result: "QueryResult", title: str) -> None:
        ax.axis("off")
        ax.set_facecolor(_BG)
        df = self._dataframe_from_result(result)
        rows = df.head(30)

        col_labels = [_label(c) for c in df.columns.tolist()]
        cell_text = [[_truncate(str(v), 40) for v in row] for row in rows.values.tolist()]

        table = ax.table(
            cellText=cell_text,
            colLabels=col_labels,
            loc="center",
            cellLoc="left",
        )
        table.auto_set_font_size(False)
        table.set_fontsize(9)
        table.scale(1, 1.5)

        for (row, col), cell in table.get_celld().items():
            if row == 0:
                cell.set_facecolor("#14141f")
                cell.set_text_props(color=_TITLE, fontweight="bold")
            else:
                cell.set_facecolor(_BG if row % 2 == 0 else "#11111c")
                cell.set_text_props(color=_TEXT)
            cell.set_edgecolor(_SPINE)

        ax.set_title(title, color=_TITLE, fontsize=13, fontweight="bold", pad=14)

    # ── Data helpers ──────────────────────────────────────────────────────────

    def _dataframe_from_result(self, result: "QueryResult") -> "pd.DataFrame":
        import pandas as pd
        return pd.DataFrame(result.rows, columns=result.columns)


# ── Module-level helpers ──────────────────────────────────────────────────────

def _style_axes(ax: "Axes") -> None:
    """Apply dark-theme styling to an Axes object in-place."""
    ax.set_facecolor(_BG)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("bottom", "left"):
        ax.spines[spine].set_color(_SPINE)


def _pick_xy(df: "pd.DataFrame") -> tuple[str, str]:
    """
    Return (x_col, y_col) regardless of column order.

    Prefers: non-numeric column as x (category axis), numeric as y (value axis).
    Falls back to first two columns if all columns share the same dtype.
    """
    numeric = df.select_dtypes(include="number").columns.tolist()
    non_numeric = df.select_dtypes(exclude="number").columns.tolist()
    if numeric and non_numeric:
        return non_numeric[0], numeric[0]
    if len(df.columns) >= 2:
        return df.columns[0], df.columns[1]
    raise ChartRenderError("Bar/line/pie chart requires at least 2 columns with distinct dtypes")


def _pick_dim_col(result: "QueryResult") -> str:
    """Return the dimension (category/string) column name from a QueryResult."""
    import pandas as pd
    df = pd.DataFrame(result.rows[:1], columns=result.columns)
    non_numeric = df.select_dtypes(exclude="number").columns.tolist()
    return non_numeric[0] if non_numeric else result.columns[0]


def _avg_label_len(labels: list[str]) -> float:
    return sum(len(l) for l in labels) / len(labels) if labels else 0


def _truncate(s: str, max_len: int) -> str:
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _label(col_name: str) -> str:
    """Convert a snake_case/raw column name to a human-readable axis label."""
    if col_name == "_id":
        return "Category"
    return col_name.replace("_", " ").title()


def _fmt_val(v: float) -> str:
    """Format a numeric value with compact suffixes (1.2M, 45.3K, 0.85)."""
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if abs(v) >= 10_000:
        return f"{v / 1_000:.0f}K"
    if abs(v) >= 1_000:
        return f"{v / 1_000:.1f}K"
    if abs(v) < 10 and v != int(v):
        return f"{v:.2f}"
    return f"{v:,.0f}"


def _clean_title(raw: str) -> str:
    """Strip filler verbs from the user query to create a clean chart title."""
    s = raw.strip().rstrip("?")
    s = _FILLER_RE.sub("", s).strip()
    # Capitalise first letter, keep the rest as-is (preserve proper nouns)
    if not s:
        return raw
    truncated = s[:72]
    return truncated[0].upper() + truncated[1:]
