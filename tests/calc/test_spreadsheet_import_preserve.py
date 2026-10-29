# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import preserve output model."""

from __future__ import annotations

from unittest.mock import MagicMock

from plugin.calc.spreadsheet_import.ingest import ingest_from_arrays
from plugin.calc.spreadsheet_import.models import CellRecord, OutputCell, OutputSheetModel, SheetModel
from plugin.calc.spreadsheet_import.preserve import (
    _safe_number_format,
    apply_output_to_sheet,
    build_output_model,
    enrich_number_formats,
)


def test_build_output_preserves_constants():
    model = SheetModel(
        sheet_name="S",
        used_range="A1:B1",
        cells={
            "A1": CellRecord("A1", "constant", 42.0, None, 123),
            "B1": CellRecord("B1", "constant", "hello", None, 456),
        },
    )
    output = build_output_model(model)
    assert output.cells["A1"].value == 42.0
    assert output.cells["A1"].formula is None
    assert output.cells["A1"].number_format == 123
    assert output.cells["B1"].value == "hello"
    assert output.cells["B1"].number_format == 456


def test_build_output_leaves_calc_formula_unchanged():
    model = ingest_from_arrays(
        sheet_name="S",
        start_col=0,
        start_row=0,
        data_array=[[10.0, 20.0]],
        formula_array=[["=SUM(A1)", ""]],
    )
    output = build_output_model(model)
    assert output.cells["A1"].formula == "=SUM(A1)"
    assert output.cells["B1"].value == 20.0


def test_build_output_normalizes_py_formula():
    model = ingest_from_arrays(
        sheet_name="S",
        start_col=0,
        start_row=0,
        data_array=[[1.0, 2.0]],
        formula_array=[["", '=PYTHON("np.sum(data)",A1)']],
    )
    output = build_output_model(model)
    assert output.cells["B1"].formula is not None
    assert output.cells["B1"].formula.startswith("=PY(")
    assert ";" in output.cells["B1"].formula


def test_build_output_passes_through_prompt_and_array():
    model = SheetModel(
        sheet_name="S",
        used_range="A1:B1",
        cells={
            "A1": CellRecord("A1", "prompt", "x", '=PROMPT("hi")', None),
            "B1": CellRecord("B1", "array_formula", 6.0, "{=SUM(A1:A2)}", None),
        },
    )
    output = build_output_model(model)
    assert output.cells["A1"].formula == '=PROMPT("hi")'
    assert output.cells["B1"].formula == "{=SUM(A1:A2)}"


def test_build_output_empty_cells():
    model = ingest_from_arrays(
        sheet_name="S",
        start_col=0,
        start_row=0,
        data_array=[[""]],
        formula_array=[[""]],
    )
    output = build_output_model(model)
    assert output.cells["A1"].value is None
    assert output.cells["A1"].formula is None


def test_safe_number_format_returns_int_or_none():
    cell = MagicMock()
    cell.getPropertyValue.return_value = 123
    assert _safe_number_format(cell) == 123
    cell.getPropertyValue.return_value = None
    assert _safe_number_format(cell) is None


def test_enrich_number_formats_updates_constants():
    model = SheetModel(
        sheet_name="S",
        used_range="A1:B1",
        cells={
            "A1": CellRecord("A1", "constant", 42.0, None, None),
            "B1": CellRecord("B1", "formula", "=SUM(1)", None, None),
        },
    )
    sheet = MagicMock()
    cell_a1 = MagicMock()
    cell_a1.getPropertyValue.return_value = 4
    sheet.getCellByPosition.return_value = cell_a1

    enrich_number_formats(sheet, model)
    assert model.cells["A1"].number_format == 4
    assert model.cells["B1"].number_format is None


def test_apply_output_to_sheet():
    sheet = MagicMock()
    cells: dict[tuple[int, int], MagicMock] = {}

    def get_cell(c: int, r: int) -> MagicMock:
        if (c, r) not in cells:
            cells[(c, r)] = MagicMock()
        return cells[(c, r)]

    sheet.getCellByPosition.side_effect = get_cell

    output = OutputSheetModel(
        sheet_name="S",
        used_range="A1:E1",
        cells={
            "A1": OutputCell(address="A1", value=None, formula="=SUM(1, 2)", number_format=None),
            "B1": OutputCell(address="B1", value=None, formula="{=A1:A2}", number_format=None),
            "C1": OutputCell(address="C1", value=None, formula=None, number_format=None),
            "D1": OutputCell(address="D1", value=42.5, formula=None, number_format=5),
            "E1": OutputCell(address="E1", value="hello", formula=None, number_format=None),
        },
    )

    apply_output_to_sheet(sheet, output)

    cells[(0, 0)].setFormula.assert_called_once_with("=SUM(1, 2)")
    cells[(1, 0)].setFormula.assert_called_once_with("{=A1:A2}")
    assert (2, 0) not in cells
    cells[(3, 0)].setValue.assert_called_once_with(42.5)
    cells[(3, 0)].setPropertyValue.assert_called_once_with("NumberFormat", 5)
    cells[(4, 0)].setString.assert_called_once_with("hello")


def test_apply_output_empty_model_does_not_raise():
    model = ingest_from_arrays(sheet_name="S", start_col=0, start_row=0, data_array=[], formula_array=[])
    output = build_output_model(model)
    sheet = MagicMock()
    apply_output_to_sheet(sheet, output)
    assert sheet.getCellByPosition.call_count == 0


def test_apply_output_ragged_model_writes_only_existing_cells():
    model = ingest_from_arrays(
        sheet_name="S",
        start_col=0,
        start_row=0,
        data_array=[[1.0, 2.0], [3.0]],
        formula_array=[["", ""], [""]],
    )
    output = build_output_model(model)
    sheet = MagicMock()
    apply_output_to_sheet(sheet, output)
    assert sheet.getCellByPosition.call_count == 3
    assert ("B2" in output.cells) is False


def test_apply_output_keeps_text_that_looks_numeric():
    sheet = MagicMock()
    cells: dict[tuple[int, int], MagicMock] = {}

    def get_cell(c: int, r: int) -> MagicMock:
        if (c, r) not in cells:
            cells[(c, r)] = MagicMock()
        return cells[(c, r)]

    sheet.getCellByPosition.side_effect = get_cell

    output = OutputSheetModel(
        sheet_name="S",
        used_range="A1:G1",
        cells={
            "A1": OutputCell(address="A1", value="00123", formula=None, number_format=None),
            "B1": OutputCell(address="B1", value="42", formula=None, number_format=None),
            "C1": OutputCell(address="C1", value="=00123", formula=None, number_format=None),
            "D1": OutputCell(address="D1", value=7, formula=None, number_format=None),
            "E1": OutputCell(address="E1", value=0, formula=None, number_format=None),
            "F1": OutputCell(address="F1", value=True, formula=None, number_format=None),
            "G1": OutputCell(address="G1", value=False, formula=None, number_format=None),
        },
    )

    apply_output_to_sheet(sheet, output)

    cells[(0, 0)].setString.assert_called_once_with("00123")
    cells[(0, 0)].setValue.assert_not_called()

    cells[(1, 0)].setString.assert_called_once_with("42")
    cells[(1, 0)].setValue.assert_not_called()

    cells[(2, 0)].setString.assert_called_once_with("=00123")
    cells[(2, 0)].setFormula.assert_not_called()

    cells[(3, 0)].setValue.assert_called_once_with(7)
    cells[(3, 0)].setString.assert_not_called()

    cells[(4, 0)].setValue.assert_called_once_with(0)
    cells[(4, 0)].setString.assert_not_called()

    cells[(5, 0)].setValue.assert_called_once_with(1.0)
    cells[(5, 0)].setString.assert_not_called()

    cells[(6, 0)].setValue.assert_called_once_with(0.0)
    cells[(6, 0)].setString.assert_not_called()


