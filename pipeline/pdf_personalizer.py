"""
pdf_personalizer.py
Raise My Presence — Kit Cover Personalization (Block 4)

Stamps 3 customer-specific tokens onto the kit cover (page 1) at delivery time:
    - {{business_name}}
    - {{business_city}}
    - {{issue_date}}

Approach: industry-standard pypdf + reportlab overlay pattern.
    1. Open the locale-keyed master kit PDF (kit_us.pdf, kit_uk.pdf, etc).
    2. Generate a single-page overlay PDF in memory containing 3 white
       rectangles (to mask the placeholder tokens) plus the substituted text.
    3. Merge the overlay onto page 1 of the master.
    4. Write the merged PDF to the output path.

The white rectangles hide the literal `{{business_name}}` placeholder text in
the master, so the substitution font doesn't need to match the kit's font
exactly (operator can swap to a kit-matching TTF in v6 polish without
re-doing coordinate measurement work).

CALIBRATION REQUIRED before first paid send:
The coordinates in _TOKEN_LAYOUT below are first-pass estimates. They MUST be
measured against the actual v5 kit PDF before live sends. Calibration helper
provided — run:

    python pdf_personalizer.py measure <path-to-v5.pdf> calibration-grid.pdf

This renders a red coordinate grid over the cover. Open the result, read the
(x, y) pairs of the 3 placeholder tokens off the grid, update _TOKEN_LAYOUT.
Then verify with:

    python pdf_personalizer.py render <path-to-v5.pdf> test-personalized.pdf \\
        --name "Smith's Bakery" --city "Sydney"
"""

import io
import logging
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import white, black
from reportlab.pdfgen import canvas

logger = logging.getLogger(__name__)

# US Letter (8.5" x 11" at 72 dpi = 612 x 792 points).
# All 4 locale kits exported at US Letter dimensions; confirm at first export.
PAGE_WIDTH, PAGE_HEIGHT = letter

# Cover page is the first page of every kit PDF (zero-indexed).
COVER_PAGE_INDEX = 0


class TokenLayout(NamedTuple):
    """Layout spec for one personalization token on the cover page.

    Coordinates are in PDF points (1 pt = 1/72 inch), origin = bottom-left.

    mask_x, mask_y: bottom-left corner of the white rectangle that hides the
                    underlying placeholder text in the master PDF.
    mask_w, mask_h: width / height of the masking rectangle.
    text_x, text_y: anchor for the substituted text. Anchor semantics depend
                    on text_align (left = baseline-left, center = baseline-center,
                    right = baseline-right).
    font_size:      reportlab font size (points).
    text_align:     'left' | 'center' | 'right'.
    """
    mask_x: float
    mask_y: float
    mask_w: float
    mask_h: float
    text_x: float
    text_y: float
    font_size: float
    text_align: str = "left"


# ============================================================================
# CALIBRATION CONSTANTS — measure against actual v5 kit PDF before shipping
# ============================================================================
# First-pass estimates below assume a centered cover layout on US Letter.
# UPDATE after running `python pdf_personalizer.py measure <kit.pdf> grid.pdf`.

_TOKEN_LAYOUT = {
    "business_name": TokenLayout(
        mask_x=130, mask_y=445, mask_w=352, mask_h=24,
        text_x=306, text_y=450, font_size=14, text_align="center",
    ),
    "business_city": TokenLayout(
        mask_x=130, mask_y=415, mask_w=352, mask_h=20,
        text_x=306, text_y=420, font_size=11, text_align="center",
    ),
    "issue_date": TokenLayout(
        mask_x=130, mask_y=385, mask_w=352, mask_h=20,
        text_x=306, text_y=390, font_size=11, text_align="center",
    ),
}

# Substitution font. Helvetica is a reportlab built-in (no TTF embed needed).
# For visual parity with the kit (Fraunces / Atelier serif), embed a TTF in
# v6 polish. Font change is a one-line edit here; no coordinate rework needed.
_FONT = "Helvetica"
_FONT_COLOR = black


def personalize_cover(
    source_pdf: Path,
    output_pdf: Path,
    business_name: str,
    business_city: str,
    issue_date: datetime,
) -> Path:
    """
    Stamp 3 personalization tokens onto the cover of source_pdf, write to output_pdf.

    Args:
        source_pdf:    locale-keyed master kit PDF (e.g., kit_us.pdf).
        output_pdf:    destination path for the personalized copy.
        business_name: customer's business name (Stripe name_collection field).
        business_city: customer's city (Stripe billing address city).
        issue_date:    delivery date; formatted as "May 18, 2026".

    Returns:
        Path to the written personalized PDF (same as output_pdf arg).

    Raises:
        FileNotFoundError: source_pdf does not exist.
        ValueError: source_pdf has no pages.
    """
    source_pdf = Path(source_pdf)
    output_pdf = Path(output_pdf)

    if not source_pdf.exists():
        raise FileNotFoundError(f"Source kit PDF not found: {source_pdf}")

    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    # Cross-platform date formatting (avoids %-d / %#d portability split).
    issue_date_str = f"{issue_date.strftime('%B')} {issue_date.day}, {issue_date.year}"

    overlay_bytes = _build_overlay(business_name, business_city, issue_date_str)

    reader = PdfReader(str(source_pdf))
    if len(reader.pages) == 0:
        raise ValueError(f"Source PDF has no pages: {source_pdf}")

    overlay_reader = PdfReader(io.BytesIO(overlay_bytes))
    overlay_page = overlay_reader.pages[0]

    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if i == COVER_PAGE_INDEX:
            page.merge_page(overlay_page)
        writer.add_page(page)

    with open(output_pdf, "wb") as f:
        writer.write(f)

    logger.info(
        f"Personalized PDF written: {output_pdf.name} "
        f"(business={business_name!r}, city={business_city!r}, date={issue_date_str!r})"
    )
    return output_pdf


def _build_overlay(business_name: str, business_city: str, issue_date_str: str) -> bytes:
    """Generate a single-page overlay PDF with 3 masks + 3 text strings."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)

    # Phase 1: paint white masks to hide underlying placeholder text.
    c.setFillColor(white)
    c.setStrokeColor(white)
    for layout in _TOKEN_LAYOUT.values():
        c.rect(layout.mask_x, layout.mask_y, layout.mask_w, layout.mask_h, fill=1, stroke=0)

    # Phase 2: draw substituted text on top of masks.
    c.setFillColor(_FONT_COLOR)
    _draw_aligned_text(c, _TOKEN_LAYOUT["business_name"], business_name)
    _draw_aligned_text(c, _TOKEN_LAYOUT["business_city"], business_city)
    _draw_aligned_text(c, _TOKEN_LAYOUT["issue_date"], issue_date_str)

    c.showPage()
    c.save()
    return buf.getvalue()


def _draw_aligned_text(c: canvas.Canvas, layout: TokenLayout, text: str) -> None:
    """Draw text on canvas c with alignment per layout spec."""
    c.setFont(_FONT, layout.font_size)
    if layout.text_align == "center":
        c.drawCentredString(layout.text_x, layout.text_y, text)
    elif layout.text_align == "right":
        c.drawRightString(layout.text_x, layout.text_y, text)
    else:
        c.drawString(layout.text_x, layout.text_y, text)


# ============================================================================
# Calibration helper — render coordinate grid over cover for measurement
# ============================================================================

def render_calibration_grid(source_pdf: Path, output_pdf: Path) -> Path:
    """
    Render the cover of source_pdf with a red coordinate grid overlay
    (50pt spacing, labelled). Open the result in any PDF viewer, read the
    (x, y) coordinates of each placeholder token off the grid, then update
    the _TOKEN_LAYOUT constants above.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont("Helvetica", 6)
    c.setStrokeColorRGB(1, 0, 0)
    c.setFillColorRGB(1, 0, 0)
    c.setLineWidth(0.25)

    # Vertical grid lines every 50pt with x-coordinate labels along the bottom.
    for x in range(0, int(PAGE_WIDTH) + 1, 50):
        c.line(x, 0, x, PAGE_HEIGHT)
        c.drawString(x + 1, 3, str(x))
    # Horizontal grid lines every 50pt with y-coordinate labels along the left.
    for y in range(0, int(PAGE_HEIGHT) + 1, 50):
        c.line(0, y, PAGE_WIDTH, y)
        c.drawString(3, y + 1, str(y))

    c.showPage()
    c.save()
    overlay_bytes = buf.getvalue()

    reader = PdfReader(str(source_pdf))
    overlay_reader = PdfReader(io.BytesIO(overlay_bytes))
    overlay_page = overlay_reader.pages[0]

    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if i == COVER_PAGE_INDEX:
            page.merge_page(overlay_page)
        writer.add_page(page)

    output_pdf = Path(output_pdf)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    with open(output_pdf, "wb") as f:
        writer.write(f)

    return output_pdf


# ============================================================================
# CLI — calibration + test rendering
# ============================================================================

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    parser = argparse.ArgumentParser(
        description="PDF cover personalizer + calibration helper",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_measure = sub.add_parser("measure", help="Render coordinate grid for token calibration")
    p_measure.add_argument("source", type=Path, help="path to master kit PDF")
    p_measure.add_argument("output", type=Path, help="path for calibration grid output")

    p_render = sub.add_parser("render", help="Render a personalized PDF with test values")
    p_render.add_argument("source", type=Path, help="path to master kit PDF")
    p_render.add_argument("output", type=Path, help="path for personalized output")
    p_render.add_argument("--name", default="Test Business", help="business_name override")
    p_render.add_argument("--city", default="Sydney", help="business_city override")

    args = parser.parse_args()

    if args.cmd == "measure":
        out = render_calibration_grid(args.source, args.output)
        print(f"Calibration grid written: {out}")
        print("Open it, read the (x, y) coordinates of each token, update _TOKEN_LAYOUT.")
    elif args.cmd == "render":
        out = personalize_cover(
            args.source, args.output,
            business_name=args.name,
            business_city=args.city,
            issue_date=datetime.now(),
        )
        print(f"Test personalized PDF written: {out}")
