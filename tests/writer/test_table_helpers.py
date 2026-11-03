# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.writer.table_helpers."""

from __future__ import annotations

from typing import Any

import pytest

from plugin.writer.table_helpers import _set_cell_string, fill_table_cells, table_dims


class DisposedException(Exception):
    pass


class _FakeBand:
    def __init__(self, count: int) -> None:
        self._count = count

    def getCount(self) -> int:
        return self._count


class _FakeTable:
    def __init__(self, rows: int, cols: int, cell_factory: Any = None) -> None:
        self.rows_band = _FakeBand(rows)
        self.cols_band = _FakeBand(cols)
        self.writes: list[tuple[int, int, str]] = []
        self._cell_factory = cell_factory

    def getRows(self) -> _FakeBand:
        return self.rows_band

    def getColumns(self) -> _FakeBand:
        return self.cols_band

    def getCellByPosition(self, col: int, row: int) -> Any:
        if self._cell_factory is not None:
            return self._cell_factory(self, col, row)

        class _Cell:
            def __init__(self, table: _FakeTable, c: int, r: int) -> None:
                self.table = table
                self.c = c
                self.r = r

            def setString(self, text: str) -> None:
                self.table.writes.append((self.c, self.r, text))

        return _Cell(self, col, row)


def test_set_cell_string_writes_via_get_text():
    inner_writes: list[str] = []
    outer_writes: list[str] = []

    class InnerText:
        def setString(self, text: str) -> None:
            inner_writes.append(text)

    class Cell:
        def getText(self) -> InnerText:
            return InnerText()

        def setString(self, text: str) -> None:
            outer_writes.append(text)

    cell = Cell()
    _set_cell_string(cell, "hello")
    assert inner_writes == ["hello"]
    assert outer_writes == []


def test_set_cell_string_uses_set_string_without_get_text():
    writes: list[str] = []

    class Cell:
        def setString(self, text: str) -> None:
            writes.append(text)

    cell = Cell()
    _set_cell_string(cell, "hello")
    assert writes == ["hello"]


def test_set_cell_string_write_failure_propagates():
    outer_writes: list[str] = []

    class InnerText:
        def setString(self, text: str) -> None:
            raise RuntimeError("write failed")

    class Cell:
        def getText(self) -> InnerText:
            return InnerText()

        def setString(self, text: str) -> None:
            outer_writes.append(text)

    cell = Cell()
    with pytest.raises(RuntimeError, match="write failed"):
        _set_cell_string(cell, "hello")
    assert outer_writes == []


def test_set_cell_string_disposal_propagates():
    class Cell:
        def getText(self) -> Any:
            raise DisposedException("object disposed")

    cell = Cell()
    with pytest.raises(DisposedException, match="object disposed"):
        _set_cell_string(cell, "hello")


def test_fill_table_cells_write_failure_is_not_a_success():
    class FailingCell:
        def setString(self, text: str) -> None:
            raise RuntimeError("cell write failed")

    def cell_factory(table: _FakeTable, col: int, row: int) -> Any:
        if col == 1:
            return FailingCell()

        class OkCell:
            def setString(self, text: str) -> None:
                table.writes.append((col, row, text))

        return OkCell()

    table = _FakeTable(rows=1, cols=2, cell_factory=cell_factory)
    with pytest.raises(RuntimeError, match="cell write failed"):
        fill_table_cells(table, [["a", "b"]])
    assert table.writes == [(0, 0, "a")]


def test_fill_table_cells_writes_grid_and_counts():
    table = _FakeTable(rows=2, cols=2)
    written = fill_table_cells(table, [["a", None], ["c", "d"]])
    assert written == 4
    assert table.writes == [(0, 0, "a"), (1, 0, ""), (0, 1, "c"), (1, 1, "d")]


def test_fill_table_cells_allows_partial_grid():
    table = _FakeTable(rows=2, cols=2)
    written = fill_table_cells(table, [["a"]])
    assert written == 1
    assert table.writes == [(0, 0, "a")]


def test_fill_table_cells_rejects_non_list_row_before_any_write():
    table = _FakeTable(rows=2, cols=2)
    with pytest.raises(ValueError, match="data row 1 is not a list"):
        fill_table_cells(table, [["a"], "oops"])
    assert table.writes == []


def test_fill_table_cells_rejects_too_many_rows_before_any_write():
    table = _FakeTable(rows=2, cols=2)
    with pytest.raises(ValueError, match="data has 3 rows but table only has 2"):
        fill_table_cells(table, [["a"], ["b"], ["c"]])
    assert table.writes == []


def test_fill_table_cells_rejects_wide_row_before_any_write():
    table = _FakeTable(rows=2, cols=2)
    with pytest.raises(ValueError, match=r"data row 0 has 3 columns but table only has 2"):
        fill_table_cells(table, [["a", "b", "c"]])
    assert table.writes == []


def test_table_dims_returns_band_counts():
    table = _FakeTable(rows=2, cols=3)
    assert table_dims(table) == (2, 3)
