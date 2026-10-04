# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin.calc.formula_dep_chain import _MAX_PRECEDENT_CELLS, _precedents_via_formula_query, _resolve_sheet_and_cell, fetch_formula_dep_chain
from plugin.tests.testing_utils import CalcDocStub


def test_resolve_sheet_and_cell_with_sheet_prefix():
    doc = CalcDocStub()
    sheet = doc.getSheets().getByName("Sheet1")
    # No active controller path for prefixed addresses — still resolves via getSheets.
    doc.CurrentController = None
    doc._controller = None

    resolved = _resolve_sheet_and_cell(doc, "Sheet1.B2")
    assert resolved is not None
    got_sheet, col, row = resolved
    assert got_sheet is sheet
    assert col == 1
    assert row == 1


def test_fetch_formula_dep_chain_uses_command_values_when_available():
    doc = CalcDocStub(command_values='{"commandValues": {"root": {"address": "B2"}}}')

    with patch("plugin.calc.navigation.navigate_to_cell"):
        chain = fetch_formula_dep_chain(doc, MagicMock(), "B2")

    assert chain is not None
    assert chain.get("source") == "uno_formula_dep_chain"
    assert chain.get("cell") == "B2"


def test_fetch_formula_dep_chain_falls_back_to_formula_query():
    doc = CalcDocStub()

    fallback = {"source": "formula_query", "precedents": [{"address": "A1", "type": "value"}]}
    with patch("plugin.calc.navigation.navigate_to_cell"), patch(
        "plugin.calc.formula_dep_chain._precedents_via_formula_query",
        return_value=fallback,
    ):
        chain = fetch_formula_dep_chain(doc, MagicMock(), "A1")

    assert chain is not None
    assert chain.get("source") == "formula_query"
    assert chain.get("precedents")


def test_fetch_formula_dep_chain_continues_when_navigation_raises():
    # A disposed doc, headless run, or missing controller can throw in
    # navigate_to_cell. That must not skip getCommandValues.
    doc = CalcDocStub(command_values='{"commandValues": {"root": {"address": "B2"}}}')

    with patch("plugin.calc.navigation.navigate_to_cell", side_effect=RuntimeError("disposed")):
        chain = fetch_formula_dep_chain(doc, MagicMock(), "B2")

    assert chain is not None
    assert chain.get("source") == "uno_formula_dep_chain"
    assert chain.get("cell") == "B2"


def test_fetch_formula_dep_chain_falls_back_when_navigation_raises():
    doc = CalcDocStub()
    fallback = {"source": "formula_query", "precedents": [{"address": "A1"}], "truncated": False}

    with patch("plugin.calc.navigation.navigate_to_cell", side_effect=RuntimeError("headless")), patch(
        "plugin.calc.formula_dep_chain._precedents_via_formula_query",
        return_value=fallback,
    ):
        chain = fetch_formula_dep_chain(doc, MagicMock(), "A1")

    assert chain is not None
    assert chain.get("source") == "formula_query"
    assert chain.get("truncated") is False


def _sheet_with_precedent_ranges(monkeypatch, range_addresses, calls: list[tuple[int, int]]):
    """Sheet whose ``XFormulaQuery`` reports *range_addresses*.

    Pytest has no live UNO runtime, so the ``com.sun.star`` import inside
    the fallback is pointed at a stub. Snapshots are counted directly —
    the cap must stop calling them, not merely drop the results.
    """
    star = sys.modules.get("com.sun.star")
    if star is None:
        star = ModuleType("com.sun.star")
        star.__path__ = []
        monkeypatch.setitem(sys.modules, "com.sun.star", star)
    sheet_mod = ModuleType("com.sun.star.sheet")

    class XFormulaQuery:
        pass

    sheet_mod.XFormulaQuery = XFormulaQuery
    monkeypatch.setitem(sys.modules, "com.sun.star.sheet", sheet_mod)
    monkeypatch.setattr(star, "sheet", sheet_mod, raising=False)

    def _snapshot(_sheet, col, row):
        calls.append((col, row))
        return {"address": f"{col},{row}"}

    monkeypatch.setattr("plugin.calc.formula_dep_chain._cell_snapshot", _snapshot)

    class _Query:
        def queryPrecedents(self, _all_levels):
            return SimpleNamespace(getRangeAddresses=lambda: range_addresses)

    cell_range = MagicMock()
    cell_range.queryInterface.return_value = _Query()

    class _Sheet:
        def getCellRangeByPosition(self, *_args):
            return cell_range

    return _Sheet()


def test_precedents_via_formula_query_caps_cell_snapshots(monkeypatch):
    # Three columns by 4000 rows is 12_000 cells, then a later range.
    # The walk must stop at the cap and must not read either the rest of
    # the first range or the second (a full-column ref would be billions).
    calls: list[tuple[int, int]] = []
    sheet = _sheet_with_precedent_ranges(
        monkeypatch,
        [
            SimpleNamespace(StartColumn=0, EndColumn=2, StartRow=0, EndRow=3999),
            SimpleNamespace(StartColumn=5, EndColumn=5, StartRow=9, EndRow=9),
        ],
        calls,
    )

    result = _precedents_via_formula_query(sheet, 0, 0)

    assert result["truncated"] is True
    assert len(result["precedents"]) == _MAX_PRECEDENT_CELLS
    assert len(calls) == _MAX_PRECEDENT_CELLS
    assert calls[0] == (0, 0)
    # 9999 cells fill rows 0..3332 (3 columns); the 10000th is row 3333, col 0.
    assert calls[-1] == (0, 3333)
    assert (5, 9) not in calls


def test_precedents_via_formula_query_uses_precedent_sheet(monkeypatch):
    # queryPrecedents reports Sheet=1. The snapshot must read that sheet,
    # not the formula sheet, and the address must name it.
    star = sys.modules.get("com.sun.star")
    if star is None:
        star = ModuleType("com.sun.star")
        star.__path__ = []
        monkeypatch.setitem(sys.modules, "com.sun.star", star)
    sheet_mod = ModuleType("com.sun.star.sheet")

    class XFormulaQuery:
        pass

    sheet_mod.XFormulaQuery = XFormulaQuery
    monkeypatch.setitem(sys.modules, "com.sun.star.sheet", sheet_mod)
    monkeypatch.setattr(star, "sheet", sheet_mod, raising=False)

    table_mod = ModuleType("com.sun.star.table")

    class CellContentType:
        EMPTY = 0
        VALUE = 1
        TEXT = 2
        FORMULA = 3

    table_mod.CellContentType = CellContentType
    monkeypatch.setitem(sys.modules, "com.sun.star.table", table_mod)
    monkeypatch.setattr(star, "table", table_mod, raising=False)

    class _Cell:
        def __init__(self, marker: str) -> None:
            self.marker = marker

        def getType(self) -> int:
            return 1

        def getError(self) -> int:
            return 0

        def getValue(self) -> str:
            return self.marker

        def getString(self) -> str:
            return self.marker

    class _Sheet:
        def __init__(self, name: str, marker: str) -> None:
            self.name = name
            self.marker = marker
            self.reads: list[tuple[int, int]] = []

        def getName(self) -> str:
            return self.name

        def getCellByPosition(self, col: int, row: int) -> _Cell:
            self.reads.append((col, row))
            return _Cell(self.marker)

    formula_sheet = _Sheet("Sheet1", "FROM-FORMULA-SHEET")
    data_sheet = _Sheet("Data", "FROM-DATA")

    class _QueryRange:
        def queryInterface(self, _kind: object):
            return _Query()

    class _Query:
        def queryPrecedents(self, _all_levels: bool):
            addr = SimpleNamespace(Sheet=1, StartColumn=0, EndColumn=0, StartRow=0, EndRow=0)
            return SimpleNamespace(getRangeAddresses=lambda: [addr])

    formula_sheet.getCellRangeByPosition = lambda *_args: _QueryRange()  # type: ignore[attr-defined]

    class _Sheets:
        def getByIndex(self, index: int) -> _Sheet:
            return (formula_sheet, data_sheet)[int(index)]

    class _Doc:
        def getSheets(self) -> _Sheets:
            return _Sheets()

    result = _precedents_via_formula_query(formula_sheet, 0, 0, _Doc())

    assert result["truncated"] is False
    assert len(result["precedents"]) == 1
    assert result["precedents"][0]["value"] == "FROM-DATA"
    assert result["precedents"][0]["address"] == "Data.A1"
    assert data_sheet.reads == [(0, 0)]
    assert formula_sheet.reads == []


def test_precedents_via_formula_query_small_range_is_not_truncated(monkeypatch):
    calls: list[tuple[int, int]] = []
    sheet = _sheet_with_precedent_ranges(
        monkeypatch,
        [
            SimpleNamespace(StartColumn=0, EndColumn=1, StartRow=0, EndRow=1),
            SimpleNamespace(StartColumn=3, EndColumn=3, StartRow=0, EndRow=0),
        ],
        calls,
    )

    result = _precedents_via_formula_query(sheet, 0, 0)

    assert result["truncated"] is False
    assert len(result["precedents"]) == 5
    assert calls == [(0, 0), (1, 0), (0, 1), (1, 1), (3, 0)]
