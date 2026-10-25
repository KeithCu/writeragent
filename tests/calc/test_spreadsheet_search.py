# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for shared Calc cell search helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from plugin.calc.spreadsheet_search import search_spreadsheet_cells


class _FakeCell:
    def __init__(self, col: int, row: int, val: str) -> None:
        self._col = col
        self._row = row
        self._val = val

    def getCellAddress(self) -> SimpleNamespace:
        return SimpleNamespace(Column=self._col, Row=self._row)

    def getString(self) -> str:
        return self._val


class _FakeSearchDescriptor:
    def __init__(self) -> None:
        self.SearchString = ""
        self.SearchRegularExpression = False
        self.SearchCaseSensitive = False


class _FakeSheet:
    def __init__(self, name: str, cells: list[_FakeCell] | None = None) -> None:
        self._name = name
        self._cells = cells or []
        self.find_next_calls = 0

    def getName(self) -> str:
        return self._name

    def createSearchDescriptor(self) -> _FakeSearchDescriptor:
        return _FakeSearchDescriptor()

    def findFirst(self, sd: Any) -> _FakeCell | None:
        if not self._cells:
            return None
        return self._cells[0]

    def findNext(self, cell: Any, sd: Any) -> _FakeCell | None:
        self.find_next_calls += 1
        try:
            idx = self._cells.index(cell)
        except ValueError:
            return None
        if idx + 1 < len(self._cells):
            return self._cells[idx + 1]
        return None


class _FakeSheetsCollection:
    def __init__(self, sheets: dict[str, _FakeSheet]) -> None:
        self._sheets = sheets

    def getElementNames(self) -> tuple[str, ...]:
        return tuple(self._sheets.keys())

    def getByName(self, name: str) -> _FakeSheet:
        return self._sheets[name]

    def hasByName(self, name: str) -> bool:
        return name in self._sheets


class _FakeController:
    def __init__(self, active_sheet: _FakeSheet) -> None:
        self._active_sheet = active_sheet

    def getActiveSheet(self) -> _FakeSheet:
        return self._active_sheet


class _FakeDoc:
    def __init__(self, sheets: list[_FakeSheet]) -> None:
        self._sheets = {s.getName(): s for s in sheets}
        self._controller = _FakeController(sheets[0] if sheets else _FakeSheet("Empty"))

    def getSheets(self) -> _FakeSheetsCollection:
        return _FakeSheetsCollection(self._sheets)

    def getCurrentController(self) -> _FakeController:
        return self._controller


def test_search_returns_one_row_per_adjacent_cell() -> None:
    # Regression for ScTable::SearchAll joining adjacent hits into ScCellRangeObj
    # which lacks getCellAddress()/getString().
    sheet = _FakeSheet("Sheet1", [_FakeCell(0, 0, "valA1"), _FakeCell(0, 1, "valA2")])
    doc = _FakeDoc([sheet])

    matches = search_spreadsheet_cells(doc, "val")
    assert len(matches) == 2
    assert matches[0] == {"sheet": "Sheet1", "cell": "A1", "value": "valA1"}
    assert matches[1] == {"sheet": "Sheet1", "cell": "A2", "value": "valA2"}


def test_search_respects_max_results() -> None:
    sheet = _FakeSheet(
        "Sheet1",
        [_FakeCell(0, 0, "1"), _FakeCell(0, 1, "2"), _FakeCell(0, 2, "3")],
    )
    doc = _FakeDoc([sheet])

    matches = search_spreadsheet_cells(doc, "pattern", max_results=2)
    assert len(matches) == 2
    assert [m["cell"] for m in matches] == ["A1", "A2"]
    # findNext was called to fetch cell 2 and cell 3, but loop exited before a 3rd append
    assert sheet.find_next_calls == 2


def test_search_stops_between_hits() -> None:
    sheet = _FakeSheet(
        "Sheet1",
        [_FakeCell(0, 0, "first"), _FakeCell(0, 1, "second")],
    )
    doc = _FakeDoc([sheet])

    # Stop after the first hit has been found and advanced
    matches = search_spreadsheet_cells(
        doc, "p", stop_checker=lambda: sheet.find_next_calls >= 1
    )
    assert len(matches) == 1
    assert matches[0]["cell"] == "A1"


def test_search_targets_named_sheet() -> None:
    sheet1 = _FakeSheet("First", [_FakeCell(0, 0, "one")])
    sheet2 = _FakeSheet("Second", [_FakeCell(1, 1, "two")])
    doc = _FakeDoc([sheet1, sheet2])

    matches = search_spreadsheet_cells(doc, "p", all_sheets=False, sheet_name="Second")
    assert len(matches) == 1
    assert matches[0] == {"sheet": "Second", "cell": "B2", "value": "two"}


def test_search_all_sheets_skips_underscore_sheets() -> None:
    sheet_vis = _FakeSheet("Visible", [_FakeCell(0, 0, "vis")])
    sheet_hid = _FakeSheet("_Hidden", [_FakeCell(0, 0, "hid")])
    doc = _FakeDoc([sheet_vis, sheet_hid])

    matches = search_spreadsheet_cells(doc, "p", all_sheets=True)
    assert len(matches) == 1
    assert matches[0]["sheet"] == "Visible"


def test_blank_pattern_returns_empty() -> None:
    sheet = _FakeSheet("Sheet1", [_FakeCell(0, 0, "anything")])
    doc = _FakeDoc([sheet])

    matches = search_spreadsheet_cells(doc, "")
    assert matches == []


def test_long_hit_value_is_truncated() -> None:
    long_val = "x" * 201
    sheet = _FakeSheet("Sheet1", [_FakeCell(0, 0, long_val)])
    doc = _FakeDoc([sheet])

    matches = search_spreadsheet_cells(doc, "x")
    assert len(matches) == 1
    assert matches[0]["value"] == "x" * 200 + "…"
    assert matches[0]["cell"] == "A1"


def test_value_at_the_cap_is_unchanged() -> None:
    cap_val = "x" * 200
    sheet = _FakeSheet("Sheet1", [_FakeCell(0, 0, cap_val)])
    doc = _FakeDoc([sheet])

    matches = search_spreadsheet_cells(doc, "x")
    assert len(matches) == 1
    assert matches[0]["value"] == cap_val


def test_short_value_is_unchanged() -> None:
    sheet = _FakeSheet("Sheet1", [_FakeCell(0, 0, "apple")])
    doc = _FakeDoc([sheet])

    matches = search_spreadsheet_cells(doc, "apple")
    assert len(matches) == 1
    assert matches[0]["value"] == "apple"
