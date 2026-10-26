"""
PowerPoint generator — produces a downloadable .pptx from a QueryResult.

Slide structure:
  Slide 1 — Title slide: report title + timestamp
  Slide 2 — Chart slide: matplotlib chart image
  Slide 3 — Data table: up to 20 rows

Uses ``python-pptx`` with a blank layout (index 6). All generation is
synchronous and offloaded to a thread-pool executor.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pptx import Presentation

    from app.mcp.base import QueryResult

logger = structlog.get_logger(__name__)

_EMU_PER_INCH = 914400


class PPTGenerator:
    """
    Generates a three-slide .pptx from a QueryResult.

    Slide 1: Title + timestamp
    Slide 2: Matplotlib chart
    Slide 3: Data table (≤ 20 rows)
    """

    SLIDE_WIDTH_EMU: int  = 12192000    # 13.33 inches (widescreen 16:9)
    SLIDE_HEIGHT_EMU: int = 6858000     # 7.5 inches
    BLANK_LAYOUT_INDEX: int = 6

    async def generate(
        self,
        result: "QueryResult",
        chart_path: Path | None,
        title: str,
        output_path: Path,
    ) -> Path:
        """
        Generate a .pptx file and write it to *output_path*.

        Args:
            result:      QueryResult for the data table slide.
            chart_path:  Matplotlib chart PNG for slide 2 (or None).
            title:       Presentation title (slide 1 heading).
            output_path: Absolute path for the .pptx output.

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
            output_path,
        )
        logger.info("PPT generated", path=str(output_path))
        return output_path

    def _generate_sync(
        self,
        result: "QueryResult",
        chart_path: Path | None,
        title: str,
        output_path: Path,
    ) -> None:
        """Synchronous .pptx build — called from thread-pool."""
        from pptx import Presentation

        prs = Presentation()
        prs.slide_width  = self.SLIDE_WIDTH_EMU
        prs.slide_height = self.SLIDE_HEIGHT_EMU

        self._add_title_slide(prs, title)
        self._add_chart_slide(prs, chart_path)
        self._add_table_slide(prs, result)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        prs.save(str(output_path))

    # ── Slide builders ────────────────────────────────────────────────────────

    def _add_title_slide(
        self,
        prs: "Presentation",
        title: str,
    ) -> None:
        """Slide 1: Dark background + centred title text + timestamp."""
        from pptx.dml.color import RGBColor
        from pptx.util import Inches, Pt

        slide = prs.slides.add_slide(prs.slide_layouts[self.BLANK_LAYOUT_INDEX])
        self._add_fallback_background(slide)

        # Dark semi-transparent title bar (drawn as a filled rectangle)
        W = self.SLIDE_WIDTH_EMU
        H = self.SLIDE_HEIGHT_EMU
        bar = slide.shapes.add_shape(
            1,                          # MSO_SHAPE_TYPE.RECTANGLE
            left=0,
            top=int(H * 0.3),
            width=W,
            height=int(H * 0.4),
        )
        bar.fill.solid()
        bar.fill.fore_color.rgb = RGBColor(0x0A, 0x1F, 0x3D)
        bar.line.fill.background()
        bar.fill.fore_color.theme_color = None  # plain RGB, no theme

        # Title text
        txBox = slide.shapes.add_textbox(
            left=Inches(1),
            top=int(H * 0.33),
            width=W - Inches(2),
            height=int(H * 0.25),
        )
        tf = txBox.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = title
        p.font.size = Pt(36)
        p.font.bold = True
        p.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

        # Timestamp subtitle
        ts_box = slide.shapes.add_textbox(
            left=Inches(1),
            top=int(H * 0.60),
            width=W - Inches(2),
            height=int(H * 0.1),
        )
        ts_box.text_frame.text = datetime.now(UTC).strftime("Generated: %Y-%m-%d %H:%M UTC")
        ts_box.text_frame.paragraphs[0].font.size = Pt(14)
        ts_box.text_frame.paragraphs[0].font.color.rgb = RGBColor(0xCC, 0xDD, 0xEE)

    def _add_chart_slide(
        self,
        prs: "Presentation",
        chart_path: Path | None,
    ) -> None:
        """Slide 2: Dark background + matplotlib chart (skipped if no chart)."""
        if not chart_path or not chart_path.exists():
            return

        from pptx.util import Inches

        slide = prs.slides.add_slide(prs.slide_layouts[self.BLANK_LAYOUT_INDEX])
        self._add_fallback_background(slide)

        W = self.SLIDE_WIDTH_EMU
        H = self.SLIDE_HEIGHT_EMU

        self._add_slide_title_bar(slide, "Data Insight", W, H)
        slide.shapes.add_picture(
            str(chart_path),
            left=Inches(0.5),
            top=int(H * 0.18),
            width=int(W * 0.92),
            height=int(H * 0.76),
        )

    def _add_table_slide(
        self,
        prs: "Presentation",
        result: "QueryResult",
    ) -> None:
        """Slide 3: Dark background + data table (≤ 20 rows)."""
        from pptx.dml.color import RGBColor
        from pptx.util import Inches, Pt

        slide = prs.slides.add_slide(prs.slide_layouts[self.BLANK_LAYOUT_INDEX])
        self._add_fallback_background(slide)

        W = self.SLIDE_WIDTH_EMU
        H = self.SLIDE_HEIGHT_EMU

        self._add_slide_title_bar(slide, "Data Table", W, H)

        top_offset = int(H * 0.16)
        rows_to_show = result.rows[:20]
        if not rows_to_show:
            return

        n_rows = len(rows_to_show) + 1   # +1 for header
        n_cols = len(result.columns)

        table_h = H - top_offset - int(H * 0.02)
        table = slide.shapes.add_table(
            n_rows, n_cols,
            left=Inches(0.3),
            top=top_offset,
            width=W - Inches(0.6),
            height=table_h,
        ).table

        # Header row
        for col_i, col_name in enumerate(result.columns):
            cell = table.cell(0, col_i)
            cell.text = col_name
            cell.fill.solid()
            cell.fill.fore_color.rgb = RGBColor(0x0A, 0x1F, 0x3D)
            p = cell.text_frame.paragraphs[0]
            p.font.bold = True
            p.font.size = Pt(9)
            p.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

        # Data rows
        for row_i, row in enumerate(rows_to_show, start=1):
            for col_i, col_name in enumerate(result.columns):
                cell = table.cell(row_i, col_i)
                cell.text = str(row.get(col_name, ""))
                p = cell.text_frame.paragraphs[0]
                p.font.size = Pt(8)
                if row_i % 2 == 0:
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = RGBColor(0xEE, 0xF2, 0xF7)

    # ── Layout helpers ────────────────────────────────────────────────────────

    def _add_background(self, slide, bg_path: Path) -> None:
        """Place a full-bleed image as the slide background (bottom of z-order)."""
        pic = slide.shapes.add_picture(
            str(bg_path),
            left=0,
            top=0,
            width=self.SLIDE_WIDTH_EMU,
            height=self.SLIDE_HEIGHT_EMU,
        )
        # Move the picture to the back so text boxes appear above it
        slide.shapes._spTree.remove(pic._element)
        slide.shapes._spTree.insert(2, pic._element)

    def _add_fallback_background(self, slide) -> None:
        """Solid dark-blue background when no AI image is available."""
        from pptx.dml.color import RGBColor
        bg = slide.shapes.add_shape(
            1,  # RECTANGLE
            left=0, top=0,
            width=self.SLIDE_WIDTH_EMU,
            height=self.SLIDE_HEIGHT_EMU,
        )
        bg.fill.solid()
        bg.fill.fore_color.rgb = RGBColor(0x0D, 0x1B, 0x2A)
        bg.line.fill.background()
        slide.shapes._spTree.remove(bg._element)
        slide.shapes._spTree.insert(2, bg._element)

    def _add_slide_title_bar(self, slide, text: str, W: int, H: int) -> None:
        """Narrow coloured title bar at the top of a content slide."""
        from pptx.dml.color import RGBColor
        from pptx.util import Pt

        bar = slide.shapes.add_shape(
            1,
            left=0, top=0,
            width=W,
            height=int(H * 0.14),
        )
        bar.fill.solid()
        bar.fill.fore_color.rgb = RGBColor(0x0A, 0x1F, 0x3D)
        bar.line.fill.background()

        txBox = slide.shapes.add_textbox(
            left=int(W * 0.02),
            top=int(H * 0.02),
            width=int(W * 0.96),
            height=int(H * 0.10),
        )
        p = txBox.text_frame.paragraphs[0]
        p.text = text
        p.font.size = Pt(18)
        p.font.bold = True
        p.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
