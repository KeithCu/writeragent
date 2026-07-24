# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Python cell discovery helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin.calc.python.cell_discovery import (
    canonicalize_py_formula_for_parse,
    extract_code_from_formula,
    is_py_formula_text,
    list_python_cells_on_sheet,
)


def test_canonicalize_writeragent_addin_prefix():
    raw = '=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY("result = 1")'
    assert canonicalize_py_formula_for_parse(raw).upper().startswith("=PYTHON(")
    assert is_py_formula_text(raw)
    assert extract_code_from_formula(raw) == "result = 1"


def test_canonicalize_librepy_addin_prefix():
    raw = '=ORG.EXTENSION.LIBREPY.PYTHONFUNCTION.PYTHON("result = 2")'
    assert is_py_formula_text(raw)
    assert extract_code_from_formula(raw) == "result = 2"


def test_short_py_formula():
    assert is_py_formula_text('=PY("result = 3")')
    assert extract_code_from_formula('=PY("result = 3")') == "result = 3"
    assert not is_py_formula_text("=SUM(A1:A2)")


def test_list_python_cells_on_sheet_filters_non_py():
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"

    py_cell = MagicMock()
    py_cell.getFormula.return_value = '=PY("result = 1")'
    other = MagicMock()
    other.getFormula.return_value = "=A1+1"

    def get_cell(col, row):
        if row == 0 and col == 0:
            return py_cell
        return other

    sheet.getCellByPosition.side_effect = get_cell

    range_obj = MagicMock()
    range_obj.getRangeAddress.return_value = SimpleNamespace(
        StartRow=0, EndRow=1, StartColumn=0, EndColumn=0
    )
    enum = MagicMock()
    enum.getCount.return_value = 1
    enum.getByIndex.return_value = range_obj
    sheet.queryContentCells.return_value = enum

    found = list_python_cells_on_sheet(sheet)
    assert len(found) == 1
    assert found[0].address == "Sheet1.A1"
    assert found[0].code == "result = 1"


def test_list_python_cells_on_sheet_bulk_get_formulas():
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"

    range_obj = MagicMock()
    range_obj.getRangeAddress.return_value = SimpleNamespace(
        StartRow=0, EndRow=1, StartColumn=0, EndColumn=1
    )
    range_obj.getFormulas.return_value = (
        ('=PY("result = 1")', "=SUM(A1:A2)"),
        ("=B1+1", '=PY("result = 2")'),
    )

    enum = MagicMock()
    enum.getCount.return_value = 1
    enum.getByIndex.return_value = range_obj
    sheet.queryContentCells.return_value = enum

    found = list_python_cells_on_sheet(sheet)
    assert len(found) == 2
    assert found[0].address == "Sheet1.A1"
    assert found[0].code == "result = 1"
    assert found[1].address == "Sheet1.B2"
    assert found[1].code == "result = 2"


