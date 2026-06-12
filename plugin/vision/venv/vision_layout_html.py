# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build layout-aware HTML from vision structure blocks (bbox columns for LO import)."""

from __future__ import annotations

import html as html_module
from typing import Any

_HEADING_TYPES = frozenset({"title", "section_header", "header", "heading", "h1", "h2"})


def _box_xywh(block: dict[str, Any]) -> tuple[int, int, int, int]:
    raw = block.get("box") or [0, 0, 0, 0]
    values = [int(raw[i]) if i < len(raw) else 0 for i in range(4)]
    return values[0], values[1], values[2], values[3]


def _block_center_x(block: dict[str, Any]) -> float:
    x, _y, w, _h = _box_xywh(block)
    return x + w / 2.0


def _block_bottom(block: dict[str, Any]) -> float:
    _x, y, _w, h = _box_xywh(block)
    return y + h


def _page_width(blocks: list[dict[str, Any]]) -> float:
    edges = [_box_xywh(block)[0] + _box_xywh(block)[2] for block in blocks if _box_xywh(block)[2] > 0]
    return float(max(edges)) if edges else 800.0


def _is_full_width(block: dict[str, Any], page_w: float) -> bool:
    _x, _y, w, _h = _box_xywh(block)
    return w >= page_w * 0.55


def _block_to_html(block: dict[str, Any]) -> str:
    block_type = str(block.get("type") or "text").strip().lower()
    text = html_module.escape(str(block.get("text") or "").strip())
    if not text:
        return ""
    if block_type in _HEADING_TYPES:
        return f"<h2>{text}</h2>"
    return f"<p>{text}</p>"


def _group_bands(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if not blocks:
        return []
    ordered = sorted(blocks, key=lambda block: (_box_xywh(block)[1], _box_xywh(block)[0]))
    heights = [max(_box_xywh(block)[3], 1) for block in ordered]
    median_h = sorted(heights)[len(heights) // 2]
    gap = max(median_h * 0.6, 8.0)

    bands: list[list[dict[str, Any]]] = [[ordered[0]]]
    for block in ordered[1:]:
        prev_bottom = max(_block_bottom(item) for item in bands[-1])
        if _box_xywh(block)[1] - prev_bottom <= gap:
            bands[-1].append(block)
        else:
            bands.append([block])
    return bands


def _render_band(band: list[dict[str, Any]], page_w: float, split_x: float) -> str:
    if any(_is_full_width(block, page_w) for block in band) or len(band) == 1:
        return "".join(
            _block_to_html(block)
            for block in sorted(band, key=lambda item: (_box_xywh(item)[1], _box_xywh(item)[0]))
            if _block_to_html(block)
        )

    left = [block for block in band if _block_center_x(block) < split_x]
    right = [block for block in band if _block_center_x(block) >= split_x]
    if left and right:
        left_html = "".join(
            _block_to_html(block)
            for block in sorted(left, key=lambda item: (_box_xywh(item)[1], _box_xywh(item)[0]))
            if _block_to_html(block)
        )
        right_html = "".join(
            _block_to_html(block)
            for block in sorted(right, key=lambda item: (_box_xywh(item)[1], _box_xywh(item)[0]))
            if _block_to_html(block)
        )
        return (
            '<table style="width:100%;border:none;border-collapse:collapse;">'
            "<tr>"
            f'<td style="width:50%;vertical-align:top;border:none;padding:0 8px 0 0;">{left_html}</td>'
            f'<td style="width:50%;vertical-align:top;border:none;padding:0 0 0 8px;">{right_html}</td>'
            "</tr></table>"
        )

    return "".join(
        _block_to_html(block)
        for block in sorted(band, key=lambda item: (_box_xywh(item)[1], _box_xywh(item)[0]))
        if _block_to_html(block)
    )


def html_from_layout_blocks(blocks: list[dict[str, Any]], params: dict[str, Any] | None = None) -> str:
    """Return a body HTML fragment from structure blocks using bbox column bands."""
    del params  # reserved for column_mode / column_gap_px (§21)
    text_blocks: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "").lower() == "table":
            continue
        if str(block.get("text") or "").strip():
            text_blocks.append(block)
    if not text_blocks:
        return ""

    page_w = _page_width(text_blocks)
    split_x = page_w * 0.5
    parts = [_render_band(band, page_w, split_x) for band in _group_bands(text_blocks)]
    return "\n".join(part for part in parts if part)
