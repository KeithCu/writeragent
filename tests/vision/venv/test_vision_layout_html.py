# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for bbox layout HTML builder."""

from __future__ import annotations

from plugin.vision.venv.vision_layout_html import _box_xywh, html_from_layout_blocks


def test_two_column_band_renders_side_by_side_cells():
    blocks = [
        {"type": "text", "text": "Left event", "box": [10, 100, 180, 20]},
        {"type": "text", "text": "Right event", "box": [420, 102, 180, 20]},
    ]
    html = html_from_layout_blocks(blocks)
    assert "Left event" in html
    assert "Right event" in html
    assert html.count("<td") >= 2


def test_full_width_heading_not_split_into_columns():
    blocks = [
        {"type": "section_header", "text": "AKIHABARA", "box": [10, 10, 580, 40]},
        {"type": "text", "text": "Left", "box": [10, 100, 180, 20]},
        {"type": "text", "text": "Right", "box": [420, 100, 180, 20]},
    ]
    html = html_from_layout_blocks(blocks)
    assert "<h2>AKIHABARA</h2>" in html
    assert "Left" in html and "Right" in html


def test_title_above_two_columns_not_interleaved():
    blocks = [
        {"type": "section_header", "text": "MAIN TITLE", "box": [10, 10, 600, 30]},
        {"type": "text", "text": "Left line 1", "box": [10, 50, 180, 20]},
        {"type": "text", "text": "Right line 1", "box": [420, 52, 180, 20]},
        {"type": "text", "text": "Left line 2", "box": [10, 75, 180, 20]},
        {"type": "text", "text": "Right line 2", "box": [420, 77, 180, 20]},
    ]
    html = html_from_layout_blocks(blocks)
    assert "<h2>MAIN TITLE</h2>" in html
    assert "<table" in html
    # Ensure left column content is grouped together and right column is grouped together
    left_cell_start = html.index('padding:0 8px 0 0;">')
    right_cell_start = html.index('padding:0 0 0 8px;">')
    left_part = html[left_cell_start:right_cell_start]
    right_part = html[right_cell_start:]
    assert "Left line 1" in left_part and "Left line 2" in left_part
    assert "Right line 1" in right_part and "Right line 2" in right_part
    assert "Right line 1" not in left_part


def test_short_centered_single_column_not_made_two_columns():
    # Two centered blocks spanning across the midline without a horizontal gap
    blocks = [
        {"type": "text", "text": "Dear Recipient,", "box": [240, 100, 300, 20]},
        {"type": "text", "text": "Thank you for writing.", "box": [260, 130, 280, 20]},
    ]
    html = html_from_layout_blocks(blocks)
    assert "<table" not in html
    assert "<p>Dear Recipient,</p>" in html
    assert "<p>Thank you for writing.</p>" in html


def test_zero_box_blocks_kept_in_original_order():
    blocks = [
        {"type": "text", "text": "First paragraph", "box": [10, 50, 200, 20]},
        {"type": "text", "text": "Second paragraph (zero box)", "box": [0, 0, 0, 0]},
        {"type": "text", "text": "Third paragraph", "box": [10, 150, 200, 20]},
    ]
    html = html_from_layout_blocks(blocks)
    pos_first = html.index("First paragraph")
    pos_second = html.index("Second paragraph (zero box)")
    pos_third = html.index("Third paragraph")
    assert pos_first < pos_second < pos_third


def test_box_xywh_tolerant():
    assert _box_xywh({"box": None}) == (0, 0, 0, 0)
    assert _box_xywh({"box": ["10", "bad", 30.5]}) == (10, 0, 30, 0)
    assert _box_xywh({"box": []}) == (0, 0, 0, 0)
    assert _box_xywh({}) == (0, 0, 0, 0)
    assert _box_xywh("not a dict") == (0, 0, 0, 0)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_empty_blocks_returns_empty_string():
    assert html_from_layout_blocks([]) == ""


def test_footer_under_body_is_not_a_column_table():
    blocks = [
        {"type": "text", "text": "Body line", "box": [10, 100, 250, 20]},
        {"type": "text", "text": "Page 1", "box": [420, 124, 60, 10]},
    ]
    html = html_from_layout_blocks(blocks)
    assert "<table" not in html
    assert html.index("Body line") < html.index("Page 1")


def test_non_dict_table_and_blank_blocks_are_skipped():
    blocks = [
        {"type": "table", "text": "raw", "box": [10, 10, 100, 20]},
        {"type": "text", "text": "   ", "box": [10, 40, 100, 20]},
        {"type": "text", "text": "Kept", "box": [10, 70, 100, 20]},
    ]
    assert html_from_layout_blocks(blocks) == "<p>Kept</p>"


def test_title_above_two_columns_golden():
    blocks = [
        {"type": "section_header", "text": "MAIN TITLE", "box": [10, 10, 600, 40]},
        {"type": "text", "text": "Left line 1", "box": [10, 100, 180, 20]},
        {"type": "text", "text": "Right line 1", "box": [420, 102, 180, 20]},
        {"type": "text", "text": "Left line 2", "box": [10, 125, 180, 20]},
        {"type": "text", "text": "Right line 2", "box": [420, 127, 180, 20]},
    ]
    expected = (
        "<h2>MAIN TITLE</h2>\n"
        '<table style="width:100%;border:none;border-collapse:collapse;"><tr>'
        '<td style="width:50%;vertical-align:top;border:none;padding:0 8px 0 0;"><p>Left line 1</p><p>Left line 2</p></td>'
        '<td style="width:50%;vertical-align:top;border:none;padding:0 0 0 8px;"><p>Right line 1</p><p>Right line 2</p></td>'
        "</tr></table>"
    )
    assert html_from_layout_blocks(blocks) == expected
