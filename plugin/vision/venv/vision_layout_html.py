# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build layout-aware HTML from vision structure blocks (bbox columns for LO import)."""

from __future__ import annotations

import html as html_module
from typing import Any, NamedTuple

_HEADING_TYPES: frozenset[str] = frozenset({"title", "section_header", "heading", "h1", "h2"})

_TWO_COLUMN_TABLE_TEMPLATE = (
    '<table style="width:100%;border:none;border-collapse:collapse;">'
    "<tr>"
    '<td style="width:50%;vertical-align:top;border:none;padding:0 8px 0 0;">{left}</td>'
    '<td style="width:50%;vertical-align:top;border:none;padding:0 0 0 8px;">{right}</td>'
    "</tr></table>"
)


class _LayoutBlock(NamedTuple):
    box: tuple[int, int, int, int]
    html: str


def _box_xywh(block: dict[str, Any]) -> tuple[int, int, int, int]:
    # Missing, short, or non-numeric coordinates are 0. Non-dicts are a zero box.
    raw = block.get("box") if isinstance(block, dict) else None
    if not isinstance(raw, (list, tuple)):
        return (0, 0, 0, 0)
    vals: list[int] = []
    for item in (list(raw) + [0, 0, 0, 0])[:4]:
        try:
            vals.append(int(float(item)))
        except (ValueError, TypeError):
            vals.append(0)
    return (vals[0], vals[1], vals[2], vals[3])


def _block_to_html(block: dict[str, Any]) -> str:
    block_type = str(block.get("type") or "text").strip().lower()
    text = html_module.escape(str(block.get("text") or "").strip())
    if not text:
        return ""
    if block_type in _HEADING_TYPES:
        return f"<h2>{text}</h2>"
    return f"<p>{text}</p>"


def _has_box(block: _LayoutBlock) -> bool:
    return block.box[2] > 0 and block.box[3] > 0


def _block_center_x(block: _LayoutBlock) -> float:
    return float(block.box[0] + block.box[2] / 2.0)


def _block_bottom(block: _LayoutBlock) -> float:
    return float(block.box[1] + block.box[3])


def _render_blocks(blocks: list[_LayoutBlock]) -> str:
    return "".join(b.html for b in blocks if b.html)


def _group_bands(blocks: list[_LayoutBlock]) -> list[list[_LayoutBlock]]:
    # A zero box [0,0,0,0] has no position. Leave those blocks in input order
    # and keep them out of spatial bands, or they sort to y=0 at the top.
    boxed = iter(sorted((b for b in blocks if _has_box(b)), key=lambda b: (b.box[1], b.box[0])))
    ordered = [next(boxed) if _has_box(b) else b for b in blocks]

    heights = [max(b.box[3], 1) for b in ordered if _has_box(b)]
    median_h = sorted(heights)[len(heights) // 2] if heights else 20
    gap = max(median_h * 0.6, 8.0)

    bands: list[list[_LayoutBlock]] = [[ordered[0]]]
    band_bottom = _block_bottom(ordered[0])
    for block in ordered[1:]:
        if (
            _has_box(block)
            and _has_box(bands[-1][-1])
            and block.box[1] - band_bottom <= gap
        ):
            bands[-1].append(block)
            band_bottom = max(band_bottom, _block_bottom(block))
        else:
            bands.append([block])
            band_bottom = _block_bottom(block)
    return bands


# Flattening a band by (y, x) interleaves columns; centered single-column text
# has no gutter. Split into columns only when extents have a positive gap.
def _render_narrow_run(run: list[_LayoutBlock], split_x: float) -> str:
    left = [b for b in run if _block_center_x(b) < split_x]
    right = [b for b in run if _block_center_x(b) >= split_x]
    if left and right:
        left_max_right = max(b.box[0] + b.box[2] for b in left)
        right_min_left = min(b.box[0] for b in right)
        left_top, left_bottom = min(b.box[1] for b in left), max(_block_bottom(b) for b in left)
        right_top, right_bottom = min(b.box[1] for b in right), max(_block_bottom(b) for b in right)
        if right_min_left - left_max_right >= 8.0 and min(left_bottom, right_bottom) > max(left_top, right_top):
            return _TWO_COLUMN_TABLE_TEMPLATE.format(left=_render_blocks(left), right=_render_blocks(right))
    return _render_blocks(run)


def _render_band(band: list[_LayoutBlock], page_w: float, split_x: float) -> str:
    parts: list[str] = []
    narrow: list[_LayoutBlock] = []
    for block in band:
        if block.box[2] >= page_w * 0.55:
            parts.append(_render_narrow_run(narrow, split_x))
            narrow = []
            parts.append(block.html)
        else:
            narrow.append(block)
    parts.append(_render_narrow_run(narrow, split_x))
    return "".join(parts)


def html_from_layout_blocks(blocks: list[dict[str, Any]]) -> str:
    """Return a body HTML fragment from structure blocks using bbox column bands."""
    parsed: list[_LayoutBlock] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "").lower() == "table":
            continue
        html = _block_to_html(block)
        if html:
            parsed.append(_LayoutBlock(box=_box_xywh(block), html=html))
    if not parsed:
        return ""

    edges = [b.box[0] + b.box[2] for b in parsed if b.box[2] > 0]
    page_w = float(max(edges)) if edges else 800.0
    split_x = page_w * 0.5
    parts = [_render_band(band, page_w, split_x) for band in _group_bands(parsed)]
    return "\n".join(part for part in parts if part)
