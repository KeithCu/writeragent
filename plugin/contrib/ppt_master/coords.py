# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""SVG viewBox pixels → LibreOffice 1/100 mm coordinates.

UPSTREAM NOTE (WriterAgent addition — not in upstream):
  Upstream svg_to_pptx uses EMU (English Metric Units) via drawingml_utils.EMU_PER_PX
  and pptx_dimensions slide sizing for OOXML. LibreOffice UNO expects 1/100 mm (hmm).

  # Upstream (venv install — not used on the default UNO export path):
  #   from svg_to_pptx.drawingml_utils import EMU_PER_PX
  #   from svg_to_pptx.pptx_dimensions import slide_emu_size
"""

from __future__ import annotations

# Default 16:9 slide size in 1/100 mm (matches common Impress 25.4cm × 14.288cm).
DEFAULT_SLIDE_WIDTH_HMM = 25400
DEFAULT_SLIDE_HEIGHT_HMM = 14288
DEFAULT_VIEWBOX_WIDTH_PX = 1280.0
DEFAULT_VIEWBOX_HEIGHT_PX = 720.0


def parse_viewbox(viewbox: str | None) -> tuple[float, float, float, float]:
    """Return min_x, min_y, width_px, height_px from an SVG viewBox string."""
    if not viewbox:
        return 0.0, 0.0, DEFAULT_VIEWBOX_WIDTH_PX, DEFAULT_VIEWBOX_HEIGHT_PX
    parts = viewbox.replace(",", " ").split()
    if len(parts) != 4:
        return 0.0, 0.0, DEFAULT_VIEWBOX_WIDTH_PX, DEFAULT_VIEWBOX_HEIGHT_PX
    try:
        min_x, min_y, w, h = (float(p) for p in parts)
        if w <= 0 or h <= 0:
            return 0.0, 0.0, DEFAULT_VIEWBOX_WIDTH_PX, DEFAULT_VIEWBOX_HEIGHT_PX
        return min_x, min_y, w, h
    except (TypeError, ValueError):
        return 0.0, 0.0, DEFAULT_VIEWBOX_WIDTH_PX, DEFAULT_VIEWBOX_HEIGHT_PX


def px_to_hmm(
    px: float,
    *,
    slide_hmm: int,
    viewbox_px: float,
    viewbox_min: float = 0.0,
) -> int:
    """Map a coordinate along one axis from SVG user units to 1/100 mm."""
    if viewbox_px <= 0:
        return int(px)
    rel = (px - viewbox_min) / viewbox_px
    return int(round(rel * slide_hmm))


def slide_dims_for_viewbox(
    vb_w: float,
    vb_h: float,
    *,
    width_hmm: int = DEFAULT_SLIDE_WIDTH_HMM,
) -> tuple[int, int]:
    """Preserve aspect ratio: fix width in hmm, derive height."""
    if vb_w <= 0 or vb_h <= 0:
        return width_hmm, DEFAULT_SLIDE_HEIGHT_HMM
    height_hmm = int(round(width_hmm * vb_h / vb_w))
    return width_hmm, height_hmm
