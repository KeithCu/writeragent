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


@patch("plugin.calc.vision_egress.CellManipulator")
@patch("plugin.calc.vision_egress.CalcBridge")
@patch("plugin.calc.vision_egress.calc_output_anchor_from_graphic", return_value=(0, 0))
def test_insert_vision_structure_merges_colspan(mock_anchor, mock_bridge, mock_manipulator_cls):
    manipulator = MagicMock()
    mock_manipulator_cls.return_value = manipulator
    result = {
        "helper": "extract_structure",
        "tables": [
            {
                "name": "table_1",
                "columns": ["ASSETS:", "", ""],
                "rows": [["Cash", "1", "2"]],
                "spans": [{"row": 0, "col": 0, "rowspan": 1, "colspan": 3}],
            }
        ],
    }
    insert_vision_structure_into_calc(MagicMock(), MagicMock(), result)
    manipulator.merge_cells.assert_called_once_with("A4:C4", center=False)


def test_format_keeps_formula_text_and_zero_padded_ids():
    result = {
        "helper": "extract_structure",
        "tables": [
            {
                "name": "ids",
                "columns": ["Code", "Expr"],
                "rows": [["000123", "=SUM(A1)"]],
            }
        ],
    }
    grid = format_vision_structure_for_calc(result)
    assert ["000123", "=SUM(A1)"] in grid
    assert all(not str(cell).startswith("'") for row in grid for cell in row)


def test_insert_asks_for_literal_text_write():
    from plugin.calc.vision_egress import insert_vision_structure_into_calc

    manipulator = MagicMock()
    with patch("plugin.calc.vision_egress.CellManipulator", return_value=manipulator), patch(
        "plugin.calc.vision_egress.CalcBridge"
    ), patch("plugin.calc.vision_egress.calc_output_anchor_from_graphic", return_value=(0, 0)):
        insert_vision_structure_into_calc(
            MagicMock(),
            MagicMock(),
            {"helper": "extract_structure", "tables": [{"name": "t", "columns": ["Id"], "rows": [["000123"]]}]},
        )
    assert manipulator.write_formula_range.call_args.kwargs["literal_text"] is True
    grid = manipulator.write_formula_range.call_args.args[1]
    assert ["000123"] in grid


def test_oversized_rowspan_does_not_cover_truncation_note():
    from plugin.calc.vision_egress import _vision_structure_calc_layout

    result = {
        "helper": "extract_structure",
        "tables": [
            {
                "name": "t",
                "columns": ["H", ""],
                "rows": [["a", "b"], ["c", "d"]],
                "spans": [{"row": 0, "col": 0, "rowspan": 50, "colspan": 1}],
                "truncated": True,
                "total_rows": 9,
            }
        ],
    }
    grid, merges = _vision_structure_calc_layout(result)
    note_at = next(i for i, row in enumerate(grid) if row and str(row[0]).startswith("(showing"))
    assert merges
    for r1, _c1, r2, _c2 in merges:
        assert r2 < note_at
        assert r1 <= r2


def test_literal_text_write_uses_setstring():
    from types import SimpleNamespace

    from plugin.calc.manipulator import CellManipulator

    addr = SimpleNamespace(StartColumn=0, EndColumn=1, StartRow=0, EndRow=0, Sheet=0)
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = addr
    sheet = MagicMock()
    cell_range.getSpreadsheet.return_value = sheet
    sheet.getCellRangeByPosition.return_value = cell_range
    cells: dict[tuple[int, int], MagicMock] = {}

    def get_cell(col, row):
        cells.setdefault((col, row), MagicMock())
        return cells[(col, row)]

    sheet.getCellByPosition.side_effect = get_cell
    formats = MagicMock()
    formats.getStandardIndex.return_value = 0
    doc = MagicMock()
    doc.getNumberFormats.return_value = formats
    doc.getPropertyValue.return_value = SimpleNamespace(Language="en", Country="US", Variant="")
    bridge = MagicMock()
    bridge.resolve_range_or_address.return_value = cell_range
    bridge.get_active_document.return_value = doc
    CellManipulator(bridge).write_formula_range("A1", [["=SUM(A1)", "000123"]], literal_text=True)
    assert cells[(0, 0)].setString.call_args[0][0] == "=SUM(A1)"
    assert cells[(1, 0)].setString.call_args[0][0] == "000123"
    cells[(0, 0)].setFormula.assert_not_called()
    cells[(1, 0)].setFormula.assert_not_called()
    flat = [cell for row in cell_range.setDataArray.call_args[0][0] for cell in row]
    assert "=SUM(A1)" not in flat
    assert "000123" not in flat
    assert 123.0 not in flat
