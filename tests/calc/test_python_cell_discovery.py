# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Python cell discovery helpers."""

from __future__ import annotations

from plugin.calc.python.cell_discovery import (
    _MAX_PYTHON_CELLS_FOUND,
    discover_python_cells_on_sheet,
    is_py_formula_text,
)
from plugin.calc.python.formula_edit import PyFormula
from plugin.tests.testing_utils import CalcDocStub, CalcSheetStub
import pytest


def test_py_formula_parse_addin_prefixes():
    wa_raw = '=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PY("result = 1")'
    parsed_wa = PyFormula.parse(wa_raw)
    assert parsed_wa is not None
    assert parsed_wa.code == "result = 1"
    assert parsed_wa.spelling == "py"
    assert is_py_formula_text(wa_raw)

    lp_raw = '=ORG.EXTENSION.LIBREPY.PYTHONFUNCTION.PYTHON("result = 2")'
    parsed_lp = PyFormula.parse(lp_raw)
    assert parsed_lp is not None
    assert parsed_lp.code == "result = 2"
    assert parsed_lp.spelling == "python"
    assert is_py_formula_text(lp_raw)

    collab_raw = '=ORG.COLLABORAOFFICE.SHEET.ADDIN.PYTHONCOMPUTEFUNCTIONS.GETPY("result = 3")'
    parsed_collab = PyFormula.parse(collab_raw)
    assert parsed_collab is not None
    assert parsed_collab.code == "result = 3"
    assert parsed_collab.spelling == "getpy"
    assert is_py_formula_text(collab_raw)


def test_short_py_formula():
    assert is_py_formula_text('=PY("result = 3")')
    parsed = PyFormula.parse('=PY("result = 3")')
    assert parsed is not None and parsed.code == "result = 3"
    empty = PyFormula.parse('=PY("")')
    assert empty is not None and empty.code == ""
    assert is_py_formula_text('=PY("")')
    assert not is_py_formula_text("=SUM(A1:A2)")
    assert not is_py_formula_text('=PY("x"+1)')
    assert PyFormula.parse("=SUM(A1:A2)") is None


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
    from plugin.calc.python.formula_edit import PyFormula

    calls: list[str] = []

    def _record(formula):
        calls.append(formula)
        return None

    monkeypatch.setattr(PyFormula, "parse", _record)
    assert cd.is_py_formula_text("=SUM(A1:A2)") is False
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

