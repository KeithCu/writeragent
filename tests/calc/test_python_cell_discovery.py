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
    is_py_formula_text,
    python_code_from_formula,
)
from plugin.calc.python.formula_edit import normalize_formula_string
from plugin.tests.testing_utils import CalcDocStub, CalcSheetStub
import pytest


@pytest.mark.parametrize(
    "value, value_2",
    [
        pytest.param('=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY("result = 1")', "=PYTHON(", id="test_canonicalize_writeragent_addin_prefix"),
        pytest.param('=ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY("result = 1")', "=PY(", id="test_canonicalize_collabora_getpy_prefix"),
    ],
)
def test_canonicalize_writeragent_addin_prefix(value, value_2):
    raw = value
    assert canonicalize_py_formula_for_parse(raw).upper().startswith(value_2)
    assert is_py_formula_text(raw)
    assert python_code_from_formula(raw) == "result = 1"


def test_canonicalize_librepy_addin_prefix():
    raw = '=ORG.EXTENSION.LIBREPY.PYTHONFUNCTION.PYTHON("result = 2")'
    assert is_py_formula_text(raw)
    assert python_code_from_formula(raw) == "result = 2"


def test_canonicalize_py_formula_for_parse_variations():
    lower_wa = '=org.extension.writeragent.pythonfunction.py("x")'
    assert canonicalize_py_formula_for_parse(lower_wa).upper().startswith("=PYTHON(")
    assert python_code_from_formula(lower_wa) == "x"

    spaced_lp = '=  ORG.EXTENSION.LIBREPY.PYTHONFUNCTION.PYTHON ("y")'
    assert canonicalize_py_formula_for_parse(spaced_lp) == '=PYTHON("y")'
    assert python_code_from_formula(spaced_lp) == "y"

    py_formula = '=PY("z")'
    assert canonicalize_py_formula_for_parse(py_formula) == normalize_formula_string(py_formula)

    python_formula = '=PYTHON("z")'
    assert canonicalize_py_formula_for_parse(python_formula) == normalize_formula_string(python_formula)


def test_short_py_formula():
    assert is_py_formula_text('=PY("result = 3")')
    assert python_code_from_formula('=PY("result = 3")') == "result = 3"
    assert python_code_from_formula('=PY("")') == ""
    assert is_py_formula_text('=PY("")')
    assert not is_py_formula_text("=SUM(A1:A2)")
    assert python_code_from_formula("=SUM(A1:A2)") is None


def test_discover_python_cells_on_sheet_filters_non_py():
    doc = CalcDocStub(
        data=(
            ('=PY("result = 1")',),
            ("=A1+1",),
        )
    )
    sheet = doc.getSheets().getByName("Sheet1")

    found = discover_python_cells_on_sheet(sheet).cells
    assert len(found) == 1
    assert found[0].address == "Sheet1.A1"
    assert found[0].code == "result = 1"


def test_discover_python_cells_on_sheet_bulk_get_formulas():
    doc = CalcDocStub(
        data=(
            ('=PY("result = 1")', "=SUM(A1:A2)"),
            ("=B1+1", '=PY("result = 2")'),
        )
    )
    sheet = doc.getSheets().getByName("Sheet1")

    found = discover_python_cells_on_sheet(sheet).cells
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


def test_discover_101st_sets_truncated():
    rows = tuple((f'=PY("x{i}")',) for i in range(_MAX_PYTHON_CELLS_FOUND + 1))
    doc = CalcDocStub(data=rows)
    sheet = doc.getSheets().getByName("Sheet1")
    discovery = discover_python_cells_on_sheet(sheet)
    assert len(discovery.cells) == _MAX_PYTHON_CELLS_FOUND
    assert discovery.truncated is True


@pytest.mark.parametrize(
    "sheet_name, expected",
    [
        ("Sheet1", "Sheet1.A1"),
        ("Data.2024", "'Data.2024'.A1"),
        ("Data Sheet", "'Data Sheet'.A1"),
        ("O'Brien", "'O''Brien'.A1"),
    ],
)
def test_cell_address_quotes_sheet_name(sheet_name, expected):
    from plugin.calc.navigation import normalize_cell_address

    sheet = CalcSheetStub(sheet_name, data=(('=PY("x")',),))
    found = discover_python_cells_on_sheet(sheet).cells
    assert [c.address for c in found] == [expected]
    # Navigation accepts the quoted form; it drops quotes a plain space does not need.
    assert normalize_cell_address(expected) is not None


def test_non_py_formula_is_rejected_before_parsing(monkeypatch):
    import plugin.calc.python.cell_discovery as cd

    calls: list[str] = []

    def _record(formula):
        calls.append(formula)
        return formula

    monkeypatch.setattr(cd, "canonicalize_py_formula_for_parse", _record)
    assert cd.python_code_from_formula("=SUM(A1:A2)") is None
    assert calls == []


def test_scan_cap_sets_truncated(monkeypatch):
    import plugin.calc.python.cell_discovery as cd

    class _Ranges:
        def getCount(self):
            return 1

        def getByIndex(self, index):
            return object()

    class _Sheet:
        def getName(self):
            return "Sheet1"

        def queryContentCells(self, flags):
            return _Ranges()

    def _rows(sheet, cell_range):
        for row in range(cd._MAX_CELLS_TO_SCAN + 1):
            yield row, 0, "=SUM(A1)"

    monkeypatch.setattr(cd, "iter_range_formula_texts", _rows)
    discovery = cd.discover_python_cells_on_sheet(_Sheet())
    assert discovery.cells == []
    assert discovery.truncated is True

