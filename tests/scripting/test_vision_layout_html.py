# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for bbox layout HTML builder."""

from __future__ import annotations

from plugin.scripting.vision_layout_html import html_from_layout_blocks


def test_two_column_band_renders_side_by_side_cells():
    blocks = [
        {"type": "text", "text": "Left event", "box": [10, 100, 180, 20]},
        {"type": "text", "text": "Right event", "box": [420, 102, 180, 20]},
    ]
    html = html_from_layout_blocks(blocks, {})
    assert "Left event" in html
    assert "Right event" in html
    assert html.count("<td") >= 2


def test_full_width_heading_not_split_into_columns():
    blocks = [
        {"type": "section_header", "text": "AKIHABARA", "box": [10, 10, 580, 40]},
        {"type": "text", "text": "Left", "box": [10, 100, 180, 20]},
        {"type": "text", "text": "Right", "box": [420, 100, 180, 20]},
    ]
    html = html_from_layout_blocks(blocks, {})
    assert "<h2>AKIHABARA</h2>" in html
    assert "Left" in html and "Right" in html


def test_empty_blocks_returns_empty_string():
    assert html_from_layout_blocks([], {}) == ""
