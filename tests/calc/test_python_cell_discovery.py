# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Python cell discovery helpers."""

from __future__ import annotations

from plugin.calc.python.cell_discovery import (
    _MAX_PYTHON_CELLS_FOUND,
    canonicalize_py_formula_for_parse,
    discover_python_cells_on_sheet,
    extract_code_from_formula,
    is_py_formula_text,
    list_python_cells_on_sheet,
)
from plugin.tests.testing_utils import CalcDocStub


def test_canonicalize_writeragent_addin_prefix():
    raw = '=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY("result = 1")'
    assert canonicalize_py_formula_for_parse(raw).upper().startswith("=PYTHON(")
    assert is_py_formula_text(raw)
    assert extract_code_from_formula(raw) == "result = 1"


def test_canonicalize_librepy_addin_prefix():
    raw = '=ORG.EXTENSION.LIBREPY.PYTHONFUNCTION.PYTHON("result = 2")'
    assert is_py_formula_text(raw)
    assert extract_code_from_formula(raw) == "result = 2"


def test_canonicalize_collabora_getpy_prefix():
    raw = '=ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY("result = 1")'
    assert canonicalize_py_formula_for_parse(raw).upper().startswith("=PY(")
    assert is_py_formula_text(raw)
    assert extract_code_from_formula(raw) == "result = 1"


def test_short_py_formula():
    assert is_py_formula_text('=PY("result = 3")')
    assert extract_code_from_formula('=PY("result = 3")') == "result = 3"
    assert not is_py_formula_text("=SUM(A1:A2)")


def test_list_python_cells_on_sheet_filters_non_py():
    doc = CalcDocStub(
        data=(
            ('=PY("result = 1")',),
            ("=A1+1",),
        )
    )
    sheet = doc.getSheets().getByName("Sheet1")

    found = list_python_cells_on_sheet(sheet)
    assert len(found) == 1
    assert found[0].address == "Sheet1.A1"
    assert found[0].code == "result = 1"


def test_list_python_cells_on_sheet_bulk_get_formulas():
    doc = CalcDocStub(
        data=(
            ('=PY("result = 1")', "=SUM(A1:A2)"),
            ("=B1+1", '=PY("result = 2")'),
        )
    )
    sheet = doc.getSheets().getByName("Sheet1")

    found = list_python_cells_on_sheet(sheet)
    assert len(found) == 2
    assert found[0].address == "Sheet1.A1"
    assert found[0].code == "result = 1"
    assert found[1].address == "Sheet1.B2"
    assert found[1].code == "result = 2"


def test_discover_exact_100_is_not_truncated():
    rows = tuple((f'=PY("x{i}")',) for i in range(_MAX_PYTHON_CELLS_FOUND))
    doc = CalcDocStub(data=rows)
    sheet = doc.getSheets().getByName("Sheet1")
    discovery = discover_python_cells_on_sheet(sheet)
    assert len(discovery.cells) == _MAX_PYTHON_CELLS_FOUND
    assert discovery.truncated is False
    assert list_python_cells_on_sheet(sheet) == discovery.cells


def test_discover_101st_sets_truncated():
    rows = tuple((f'=PY("x{i}")',) for i in range(_MAX_PYTHON_CELLS_FOUND + 1))
    doc = CalcDocStub(data=rows)
    sheet = doc.getSheets().getByName("Sheet1")
    discovery = discover_python_cells_on_sheet(sheet)
    assert len(discovery.cells) == _MAX_PYTHON_CELLS_FOUND
    assert discovery.truncated is True
