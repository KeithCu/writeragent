# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc vision egress (HTML and structured grids)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.vision_egress import (
    _vision_structure_calc_layout,
    calc_output_anchor_from_graphic,
    insert_vision_html_into_calc,
    insert_vision_structure_into_calc,
)
from plugin.framework.errors import DocumentDisposedError, ToolExecutionError


def test_calc_output_anchor_from_graphic_no_selection():
    with patch("plugin.calc.vision_egress.selected_graphic_object", return_value=None):
        with pytest.raises(ToolExecutionError) as exc:
            calc_output_anchor_from_graphic(MagicMock())
        assert exc.value.code == "NO_IMAGE_SELECTED"


def test_calc_output_anchor_from_graphic_returns_row_below():
    doc = MagicMock()
    doc.getSheets.return_value.getByIndex.return_value.getName.return_value = "Sheet1"
    shape = MagicMock()
    anchor = MagicMock()
    anchor.getCellAddress.return_value = MagicMock(Sheet=0, Column=2, Row=4)
    shape.getPropertyValue.return_value = anchor

    with patch("plugin.calc.vision_egress.selected_graphic_object", return_value=shape):
        sheet_name, col, row = calc_output_anchor_from_graphic(doc)
    assert (sheet_name, col, row) == ("Sheet1", 2, 5)


def test_calc_output_anchor_uses_named_graphic_not_selection():
    doc = MagicMock()
    doc.getSheets.return_value.getByIndex.return_value.getName.return_value = "Sheet1"
    shape = MagicMock()
    anchor = MagicMock()
    anchor.getCellAddress.return_value = MagicMock(Sheet=0, Column=0, Row=1)
    shape.getPropertyValue.return_value = anchor
    with patch("plugin.calc.vision_egress.get_graphic_object_by_name", return_value=shape) as by_name, patch(
        "plugin.calc.vision_egress.selected_graphic_object"
    ) as selected:
        sheet_name, col, row = calc_output_anchor_from_graphic(doc, image_name="Photo1")
    assert (sheet_name, col, row) == ("Sheet1", 0, 2)
    by_name.assert_called_once()
    selected.assert_not_called()


def test_insert_html_activates_anchor_sheet():
    doc = MagicMock()
    ctx = MagicMock()
    bridge = MagicMock()
    mock_sheet = MagicMock()
    bridge.get_sheet.return_value = mock_sheet

    with patch("plugin.calc.vision_egress.CalcBridge", return_value=bridge), patch(
        "plugin.calc.vision_egress.insert_cell_html_rich"
    ) as mock_rich, patch(
        "plugin.calc.vision_egress.calc_output_anchor_from_graphic", return_value=("Sheet1", 1, 3)
    ):
        insert_vision_html_into_calc(doc, ctx, "<p>hello</p>")
    bridge.get_sheet.assert_called_once_with("Sheet1")
    doc.getCurrentController.return_value.setActiveSheet.assert_called_once_with(mock_sheet)
    mock_rich.assert_called_once_with(doc, ctx, "B4", "<p>hello</p>")


def test_calc_output_anchor_missing_name_raises_image_not_found():
    with patch("plugin.calc.vision_egress.get_graphic_object_by_name", return_value=None):
        with pytest.raises(ToolExecutionError) as exc:
            calc_output_anchor_from_graphic(MagicMock(), image_name="Missing")
    assert exc.value.code == "IMAGE_NOT_FOUND"


def test_calc_anchor_disposed_property_raises_document_disposed():
    class DisposedException(Exception):
        pass

    shape = MagicMock()
    shape.getPropertyValue.side_effect = DisposedException("gone")
    with patch("plugin.calc.vision_egress.selected_graphic_object", return_value=shape):
        with pytest.raises(DocumentDisposedError):
            calc_output_anchor_from_graphic(MagicMock())


def test_calc_anchor_disposed_cell_address_is_not_no_output_anchor():
    class DisposedException(Exception):
        pass

    shape = MagicMock()
    anchor = MagicMock()
    anchor.getCellAddress.side_effect = DisposedException("gone")
    shape.getPropertyValue.return_value = anchor
    with patch("plugin.calc.vision_egress.selected_graphic_object", return_value=shape):
        with pytest.raises(DocumentDisposedError):
            calc_output_anchor_from_graphic(MagicMock())


def test_calc_anchor_ordinary_address_error_stays_no_output_anchor():
    shape = MagicMock()
    anchor = MagicMock()
    anchor.getCellAddress.side_effect = ValueError("not a cell")
    shape.getPropertyValue.return_value = anchor
    with patch("plugin.calc.vision_egress.selected_graphic_object", return_value=shape):
        with pytest.raises(ToolExecutionError) as exc:
            calc_output_anchor_from_graphic(MagicMock())
    assert exc.value.code == "NO_OUTPUT_ANCHOR"


def test_calc_anchor_runtime_property_is_not_document_disposed():
    class RuntimeException(Exception):
        pass

    shape = MagicMock()
    shape.getPropertyValue.side_effect = RuntimeException("fail")
    with patch("plugin.calc.vision_egress.selected_graphic_object", return_value=shape):
        with pytest.raises(ToolExecutionError) as exc:
            calc_output_anchor_from_graphic(MagicMock())
    assert exc.value.code == "NO_OUTPUT_ANCHOR"


def test_calc_anchor_runtime_cell_address_is_not_document_disposed():
    class RuntimeException(Exception):
        pass

    shape = MagicMock()
    anchor = MagicMock()
    anchor.getCellAddress.side_effect = RuntimeException("fail")
    shape.getPropertyValue.return_value = anchor
    with patch("plugin.calc.vision_egress.selected_graphic_object", return_value=shape):
        with pytest.raises(ToolExecutionError) as exc:
            calc_output_anchor_from_graphic(MagicMock())
    assert exc.value.code == "NO_OUTPUT_ANCHOR"


def test_vision_structure_layout_tables_and_blocks():
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
    grid, _ = _vision_structure_calc_layout(result)
    assert grid[0] == ["extract_structure"]
    assert ["Title line"] in grid
    assert ["Item", "Qty"] in grid
    assert ["Widget", "2"] in grid
    assert len(grid) > 1


@patch("plugin.calc.vision_egress.CellManipulator")
@patch("plugin.calc.vision_egress.CalcBridge")
@patch("plugin.calc.vision_egress.calc_output_anchor_from_graphic", return_value=("Sheet1", 1, 4))
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
    assert manipulator.write_formula_range.call_args[0][0] == "Sheet1.B5"


@patch("plugin.calc.vision_egress.CellManipulator")
@patch("plugin.calc.vision_egress.CalcBridge")
@patch("plugin.calc.vision_egress.calc_output_anchor_from_graphic", return_value=("Sheet1", 0, 0))
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
    manipulator.merge_cells.assert_called_once_with("Sheet1.A4:C4", center=False)


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
    grid, _ = _vision_structure_calc_layout(result)
    assert ["000123", "=SUM(A1)"] in grid
    assert all(not str(cell).startswith("'") for row in grid for cell in row)


def test_insert_asks_for_literal_text_write():
    manipulator = MagicMock()
    with patch("plugin.calc.vision_egress.CellManipulator", return_value=manipulator), patch(
        "plugin.calc.vision_egress.CalcBridge"
    ), patch("plugin.calc.vision_egress.calc_output_anchor_from_graphic", return_value=("Sheet1", 0, 0)):
        insert_vision_structure_into_calc(
            MagicMock(),
            MagicMock(),
            {"helper": "extract_structure", "tables": [{"name": "t", "columns": ["Id"], "rows": [["000123"]]}]},
        )
    assert manipulator.write_formula_range.call_args.kwargs["literal_text"] is True
    grid = manipulator.write_formula_range.call_args.args[1]
    assert ["000123"] in grid


def test_oversized_rowspan_does_not_cover_truncation_note():
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


def test_vision_structure_preserves_string_cells_without_numeric_coercion():
    result = {
        "helper": "extract_structure",
        "tables": [{"name": "table_1", "columns": ["Code"], "rows": [["0123"], [None]]}],
    }
    grid, _ = _vision_structure_calc_layout(result)
    assert ["0123"] in grid
    assert [""] in grid


@patch("plugin.calc.vision_egress.CellManipulator")
@patch("plugin.calc.vision_egress.CalcBridge")
@patch("plugin.calc.vision_egress.calc_output_anchor_from_graphic", return_value=("Sheet1", 0, 0))
def test_insert_vision_structure_title_only_raises_vision_error(mock_anchor, mock_bridge, mock_manipulator_cls):
    manipulator = MagicMock()
    mock_manipulator_cls.return_value = manipulator
    result = {"helper": "extract_structure"}
    with pytest.raises(ToolExecutionError) as exc_info:
        insert_vision_structure_into_calc(MagicMock(), MagicMock(), result)
    assert exc_info.value.code == "VISION_ERROR"
    manipulator.write_formula_range.assert_not_called()


def test_multiple_tables_get_table_n_names():
    result = {
        "helper": "extract_structure",
        "tables": [
            {"columns": ["ColA"], "rows": [["1"]]},
            {"columns": ["ColB"], "rows": [["2"]]},
        ],
    }
    grid, _ = _vision_structure_calc_layout(result)
    assert ["table_1"] in grid
    assert ["ColA"] in grid
    assert ["1"] in grid
    assert ["table_2"] in grid
    assert ["ColB"] in grid
    assert ["2"] in grid


def test_truncation_note_is_last_in_table():
    result = {
        "helper": "extract_structure",
        "tables": [
            {
                "name": "t",
                "columns": ["Col"],
                "rows": [["1"], ["2"]],
                "truncated": True,
                "total_rows": 50,
            }
        ],
    }
    grid, _ = _vision_structure_calc_layout(result)
    assert grid[-1] == ["(showing first rows; 50 total)"]


def test_table_without_spans_has_no_merges():
    result = {
        "helper": "extract_structure",
        "tables": [
            {
                "name": "t",
                "columns": ["ColA", "ColB"],
                "rows": [["1", "2"]],
            }
        ],
    }
    _grid, merges = _vision_structure_calc_layout(result)
    assert merges == []


def test_span_ignores_non_numeric_offsets():
    result = {
        "helper": "extract_structure",
        "tables": [
            {
                "name": "t",
                "columns": ["A", "B"],
                "rows": [["1", "2"]],
                "spans": [{"row": "x", "col": 0, "colspan": 2}],
            }
        ],
    }
    _grid, merges = _vision_structure_calc_layout(result)
    assert merges == []


def test_span_ignores_negative_origin():
    result = {
        "helper": "extract_structure",
        "tables": [
            {
                "name": "t",
                "columns": ["A", "B"],
                "rows": [["1", "2"]],
                "spans": [{"row": 0, "col": -2, "colspan": 2}],
            }
        ],
    }
    _grid, merges = _vision_structure_calc_layout(result)
    assert merges == []


def test_span_colspan_is_clamped_to_table_width():
    result = {
        "helper": "extract_structure",
        "tables": [
            {
                "name": "t",
                "columns": ["A", "B", "C"],
                "rows": [["1", "2", "3"]],
                "spans": [{"row": 0, "col": 0, "rowspan": 1, "colspan": 999}],
            }
        ],
    }
    _grid, merges = _vision_structure_calc_layout(result)
    assert len(merges) == 1
    _r1, c1, _r2, c2 = merges[0]
    assert c1 == 0
    assert c2 == 2


def test_span_starting_past_last_data_row_is_skipped():
    result = {
        "helper": "extract_structure",
        "tables": [
            {
                "name": "t",
                "columns": ["A", "B"],
                "rows": [["1", "2"]],
                "spans": [{"row": 99, "col": 0, "rowspan": 1, "colspan": 2}],
            }
        ],
    }
    _grid, merges = _vision_structure_calc_layout(result)
    assert merges == []


@patch("plugin.calc.vision_egress.CellManipulator")
@patch("plugin.calc.vision_egress.CalcBridge")
@patch("plugin.calc.vision_egress.calc_output_anchor_from_graphic", return_value=("Sheet1", 0, 0))
def test_error_result_has_no_table_and_raises_vision_error(mock_anchor, mock_bridge, mock_manipulator_cls):
    manipulator = MagicMock()
    mock_manipulator_cls.return_value = manipulator
    doc = MagicMock()
    ctx = MagicMock()
    result = {"status": "error", "message": "OCR failed"}
    with pytest.raises(ToolExecutionError) as exc_info:
        insert_vision_structure_into_calc(doc, ctx, result)
    assert exc_info.value.code == "VISION_ERROR"
    manipulator.write_formula_range.assert_not_called()
