# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import preserve output model."""

from __future__ import annotations

from unittest.mock import MagicMock

from plugin.calc.spreadsheet_import.ingest import ingest_from_arrays
from plugin.calc.spreadsheet_import.models import CellRecord, SheetModel
from plugin.calc.spreadsheet_import.preserve import (
    _safe_number_format,
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
    assert len(output.py_extracts) == 1
    assert output.py_extracts[0].changed is True


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
