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
    idx: int
    raw: dict[str, Any]
    box: tuple[int, int, int, int]
    html: str


def _box_xywh(block: dict[str, Any]) -> tuple[int, int, int, int]:
    # Non-numeric or short box lists raise TypeError/ValueError, so parse
    # coordinates as numbers and treat missing entries as 0.
    raw = block.get("box") if isinstance(block, dict) else None
    if not isinstance(raw, (list, tuple)):
        return (0, 0, 0, 0)
    vals: list[int] = []
    for i in range(4):
        if i < len(raw):
            try:
                vals.append(int(float(raw[i])))
            except (ValueError, TypeError):
                vals.append(0)
        else:
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
    return block.box[2] > 0 and block.box[3] > 0 and block.box != (0, 0, 0, 0)


def _sort_key(block: _LayoutBlock) -> tuple[int, int]:
    return (block.box[1], block.box[0])


def _block_center_x(block: _LayoutBlock) -> float:
    return float(block.box[0] + block.box[2] / 2.0)


def _block_bottom(block: _LayoutBlock) -> float:
    return float(block.box[1] + block.box[3])


def _page_width(blocks: list[_LayoutBlock]) -> float:
    edges = [b.box[0] + b.box[2] for b in blocks if b.box[2] > 0]
    return float(max(edges)) if edges else 800.0


def _is_full_width(block: _LayoutBlock, page_w: float) -> bool:
    return block.box[2] >= page_w * 0.55


def _render_blocks(blocks: list[_LayoutBlock]) -> str:
    return "".join(b.html for b in blocks if b.html)


def _group_bands(blocks: list[_LayoutBlock]) -> list[list[_LayoutBlock]]:
    if not blocks:
        return []
    # A zero box [0,0,0,0] has no position. Leave those blocks in input order
    # and keep them out of spatial bands, or they sort to y=0 at the top.
    zero_indices = {i for i, b in enumerate(blocks) if not _has_box(b)}
    boxed_blocks = [b for i, b in enumerate(blocks) if i not in zero_indices]
    sorted_boxed = sorted(boxed_blocks, key=_sort_key)

    ordered: list[_LayoutBlock] = []
    boxed_iter = iter(sorted_boxed)
    for i in range(len(blocks)):
        if i in zero_indices:
            ordered.append(blocks[i])
        else:
            ordered.append(next(boxed_iter))

    heights = [max(b.box[3], 1) for b in ordered if _has_box(b)]
    median_h = sorted(heights)[len(heights) // 2] if heights else 20
    gap = max(median_h * 0.6, 8.0)

    bands: list[list[_LayoutBlock]] = [[ordered[0]]]
    for block in ordered[1:]:
        if not _has_box(block) or not _has_box(bands[-1][-1]):
            bands.append([block])
            continue
        prev_bottom = max(_block_bottom(item) for item in bands[-1])
        if block.box[1] - prev_bottom <= gap:
            bands[-1].append(block)
        else:
            bands.append([block])
    return bands


def _render_band(band: list[_LayoutBlock], page_w: float, split_x: float) -> str:
    if len(band) == 1:
        return _render_blocks(band)

    # Pull full-width blocks out of a band as sequential elements. Flattening
    # the whole band by (y, x) interleaves columns. Split narrow blocks into
    # columns only when their extents have a positive horizontal gap; without
    # that gutter, centered single-column text becomes a two-column table.
    ordered = sorted(band, key=_sort_key)
    out_parts: list[str] = []
    narrow_run: list[_LayoutBlock] = []

    def _flush_narrow() -> None:
        if not narrow_run:
            return
        if len(narrow_run) == 1:
            out_parts.append(_render_blocks(narrow_run))
            narrow_run.clear()
            return
        left = [b for b in narrow_run if _block_center_x(b) < split_x]
        right = [b for b in narrow_run if _block_center_x(b) >= split_x]
        if left and right:
            left_max_right = max(b.box[0] + b.box[2] for b in left)
            right_min_left = min(b.box[0] for b in right)
            if right_min_left - left_max_right >= 8.0:
                left_html = _render_blocks(sorted(left, key=_sort_key))
                right_html = _render_blocks(sorted(right, key=_sort_key))
                out_parts.append(_TWO_COLUMN_TABLE_TEMPLATE.format(left=left_html, right=right_html))
                narrow_run.clear()
                return
        out_parts.append(_render_blocks(sorted(narrow_run, key=_sort_key)))
        narrow_run.clear()

    for block in ordered:
        if _is_full_width(block, page_w):
            _flush_narrow()
            out_parts.append(_render_blocks([block]))
        else:
            narrow_run.append(block)
    _flush_narrow()

    return "".join(out_parts)


def html_from_layout_blocks(blocks: list[dict[str, Any]]) -> str:
    """Return a body HTML fragment from structure blocks using bbox column bands."""
    parsed: list[_LayoutBlock] = []
    for idx, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "").lower() == "table":
            continue
        if str(block.get("text") or "").strip():
            box = _box_xywh(block)
            html = _block_to_html(block)
            parsed.append(_LayoutBlock(idx=idx, raw=block, box=box, html=html))
    if not parsed:
        return ""

    page_w = _page_width(parsed)
    split_x = page_w * 0.5
    parts = [_render_band(band, page_w, split_x) for band in _group_bands(parsed)]
    return "\n".join(part for part in parts if part)
