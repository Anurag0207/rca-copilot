"""
PDF generator — produces a downloadable A4 PDF from a QueryResult.

Layout (top to bottom):
  Page 1: Title + timestamp + query context + matplotlib chart
  Page 2: Up to 50 rows of tabular data

Uses ``reportlab`` for pure-Python PDF generation with no system dependencies.
All generation is synchronous and offloaded to a thread-pool executor.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from app.mcp.base import QueryResult

logger = structlog.get_logger(__name__)


class PDFGenerator:
    """
    Generates a formatted A4 PDF report from a QueryResult.

    Uses reportlab's Platypus (story-based) layout engine for structured
    content composition.
    """

    PAGE_SIZE_NAME: str = "A4"
    MARGIN_PT: float = 54.0     # 0.75-inch margin

    async def generate(
        self,
        result: "QueryResult",
        chart_path: Path | None,
        title: str,
        query_context: str,
        output_path: Path,
        insight_text: str | None = None,
        stats: "Any | None" = None,
    ) -> Path:
        """
        Generate an A4 PDF and write it to *output_path*.

        Args:
            result:        QueryResult to present in the report.
            chart_path:    Pre-rendered matplotlib chart PNG (or None).
            title:         Report title.
            query_context: HQL/SQL string shown as report context.
            output_path:   Absolute file path for the output PDF.
            insight_text:  Plain-English insight paragraph (shown bold at top).
            stats:         SummaryStats object — rendered as a key-stats table.

        Returns:
            output_path on success.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._generate_sync,
            result,
            chart_path,
            title,
            query_context,
            output_path,
            insight_text,
            stats,
        )
        logger.info("PDF generated", path=str(output_path))
        return output_path

    def _generate_sync(
        self,
        result: "QueryResult",
        chart_path: Path | None,
        title: str,
        query_context: str,
        output_path: Path,
        insight_text: str | None = None,
        stats: "Any | None" = None,
    ) -> None:
        """Synchronous PDF build using reportlab Platypus — runs in thread-pool."""
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            Image,
            PageBreak,
            Paragraph,
            Preformatted,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)

        page_w, page_h = A4
        content_w = page_w - 2 * self.MARGIN_PT

        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=A4,
            leftMargin=self.MARGIN_PT,
            rightMargin=self.MARGIN_PT,
            topMargin=self.MARGIN_PT,
            bottomMargin=self.MARGIN_PT,
        )

        styles = getSampleStyleSheet()
        style_title = ParagraphStyle(
            "ReportTitle",
            parent=styles["Title"],
            fontSize=22,
            spaceAfter=6,
        )
        style_sub = ParagraphStyle(
            "Sub",
            parent=styles["Normal"],
            fontSize=9,
            textColor=colors.grey,
            spaceAfter=12,
        )
        style_insight = ParagraphStyle(
            "Insight",
            parent=styles["Normal"],
            fontSize=11,
            leading=16,
            fontName="Helvetica-Bold",
            spaceBefore=8,
            spaceAfter=12,
            textColor=colors.HexColor("#1a1a2e"),
        )
        style_code = ParagraphStyle(
            "QueryCode",
            parent=styles["Code"],
            fontSize=8,
            leading=11,
            backColor=colors.HexColor("#f4f4f4"),
            borderColor=colors.HexColor("#cccccc"),
            borderWidth=1,
            borderPad=6,
            spaceAfter=10,
        )
        style_section = ParagraphStyle(
            "Section",
            parent=styles["Heading2"],
            fontSize=13,
            spaceBefore=14,
            spaceAfter=6,
        )

        story = []

        # ── Page 1: Title + insight + stats + chart ───────────────────────────
        story.append(Paragraph(title, style_title))
        story.append(
            Paragraph(
                f"Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
                style_sub,
            )
        )

        # Insight paragraph (bold, at the top)
        if insight_text:
            story.append(Paragraph(insight_text, style_insight))

        # Summary stats table
        if stats is not None:
            stat_rows = _build_stats_table_data(stats)
            if stat_rows:
                story.append(Paragraph("Key Statistics", style_section))
                st = Table(stat_rows, colWidths=[content_w * 0.45, content_w * 0.55])
                st.setStyle(
                    TableStyle([
                        ("FONTNAME",   (0, 0), (0, -1), "Helvetica-Bold"),
                        ("FONTSIZE",   (0, 0), (-1, -1), 9),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
                        ("ROWBACKGROUNDS", (0, 0), (-1, -1),
                         [colors.white, colors.HexColor("#f4f6fa")]),
                        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
                    ])
                )
                story.append(st)
                story.append(Spacer(1, 0.15 * inch))

        if query_context:
            story.append(Paragraph("Query", style_section))
            display_query = query_context[:800] + ("…" if len(query_context) > 800 else "")
            story.append(Preformatted(display_query, style_code))

        if chart_path and chart_path.exists():
            story.append(Paragraph("Chart", style_section))
            chart_h = content_w * 0.55
            story.append(Image(str(chart_path), width=content_w, height=chart_h))
            story.append(Spacer(1, 0.2 * inch))

        # ── Page 2: Data table ────────────────────────────────────────────────
        story.append(PageBreak())
        story.append(Paragraph("Data", style_section))

        rows_to_show = result.rows[:50]
        if rows_to_show:
            table_data = [result.columns] + [
                [str(row.get(c, "")) for c in result.columns]
                for row in rows_to_show
            ]
            col_count = len(result.columns)
            col_width = content_w / max(col_count, 1)

            t = Table(
                table_data,
                colWidths=[col_width] * col_count,
                repeatRows=1,
            )
            t.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3a5c")),
                        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
                        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTSIZE",   (0, 0), (-1, 0), 9),
                        ("FONTSIZE",   (0, 1), (-1, -1), 8),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2f7")]),
                        ("GRID",       (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        ("LEFTPADDING",  (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            story.append(t)

            if len(result.rows) > 50:
                story.append(Spacer(1, 0.1 * inch))
                story.append(
                    Paragraph(
                        f"Showing 50 of {len(result.rows)} rows.",
                        style_sub,
                    )
                )
        else:
            story.append(Paragraph("No data rows returned.", styles["Normal"]))

        doc.build(story)


def _build_stats_table_data(stats: Any) -> list[list[str]]:
    """Convert a SummaryStats object into [label, value] rows for the PDF table."""
    rows: list[list[str]] = []
    if stats.row_count is not None:
        rows.append(["Row count", str(stats.row_count)])
    if stats.primary_metric:
        rows.append(["Primary metric", stats.primary_metric.replace("_", " ")])
    if stats.total is not None:
        rows.append(["Total", f"{stats.total:,.2f}"])
    if stats.mean is not None:
        rows.append(["Mean", f"{stats.mean:,.4f}"])
    if stats.max_value is not None:
        label = f" ({stats.max_label})" if stats.max_label else ""
        rows.append(["Maximum", f"{stats.max_value:,.4f}{label}"])
    if stats.min_value is not None:
        label = f" ({stats.min_label})" if stats.min_label else ""
        rows.append(["Minimum", f"{stats.min_value:,.4f}{label}"])
    if stats.top_share_pct is not None:
        rows.append(["Top item share", f"{stats.top_share_pct:.1f}%"])
    return rows
