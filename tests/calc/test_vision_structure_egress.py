# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for structured Calc vision egress."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc.vision_egress import (
    format_vision_structure_for_calc,
    insert_vision_structure_into_calc,
    structure_calc_grid_has_content,
)


def test_format_vision_structure_for_calc_tables_and_blocks():
    result = {
        "helper": "extract_structure",
        "blocks": [{"type": "text", "text": "Title line", "box": [0, 0, 100, 10]}],
        "tables": [
            {
                "name": "table_1",
                "columns": ["Item", "Qty"],
                "rows": [["Widget", "2"]],
                "truncated": False,
                "total_rows": 1,
            }
        ],
    }
    grid = format_vision_structure_for_calc(result)
    assert grid[0] == ["extract_structure"]
    assert ["Title line"] in grid
    assert ["Item", "Qty"] in grid
    assert ["Widget", "2"] in grid
    assert structure_calc_grid_has_content(grid)


@patch("plugin.calc.vision_egress.CellManipulator")
@patch("plugin.calc.vision_egress.CalcBridge")
@patch("plugin.calc.vision_egress.calc_output_anchor_from_graphic", return_value=(1, 4))
def test_insert_vision_structure_into_calc(mock_anchor, mock_bridge, mock_manipulator_cls):
    doc = MagicMock()
    ctx = MagicMock()
    manipulator = MagicMock()
    mock_manipulator_cls.return_value = manipulator
    result = {
        "helper": "extract_structure",
        "tables": [{"name": "table_1", "columns": ["A"], "rows": [["1"]]}],
    }
    rows = insert_vision_structure_into_calc(doc, ctx, result)
    assert rows >= 3
    manipulator.write_formula_range.assert_called_once()
    assert manipulator.write_formula_range.call_args[0][0] == "B5"
