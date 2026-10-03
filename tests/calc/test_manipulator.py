# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc cell manipulator helpers (no soffice)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugin.calc import CalcError
from plugin.calc.manipulator import CellManipulator


def _used(start_row: int, end_row: int, end_col: int) -> SimpleNamespace:
    return SimpleNamespace(StartRow=start_row, EndRow=end_row, EndColumn=end_col)


def test_array_probe_sits_two_columns_right_of_used_area():
    col, row = CellManipulator._array_probe_origin(_used(0, 20, 3), avoid_col=1)
    assert (col, row) == (5, 0)


def test_array_probe_clears_the_target_column():
    col, row = CellManipulator._array_probe_origin(_used(0, 20, 3), avoid_col=10)
    assert (col, row) == (12, 0)


def test_array_probe_shifts_below_used_rows_when_clamped_onto_xfd():
    # Used area already at XFD. min(..., 16383) used to overwrite rows 0–1.
    col, row = CellManipulator._array_probe_origin(_used(0, 10, 16383), avoid_col=5)
    assert col == 16383
    assert row == 11


def test_array_probe_stays_on_row_zero_when_xfd_used_rows_start_lower():
    # Data only on row 5 of XFD: rows 0–1 are outside the used rectangle.
    col, row = CellManipulator._array_probe_origin(_used(5, 5, 16383), avoid_col=0)
    assert (col, row) == (16383, 0)


def test_array_probe_does_not_shift_when_clamp_is_still_outside_used_columns():
    col, row = CellManipulator._array_probe_origin(_used(0, 10, 16381), avoid_col=0)
    assert (col, row) == (16383, 0)


def test_array_probe_refuses_when_xfd_has_no_rows_left():
    with pytest.raises(CalcError, match="column XFD"):
        CellManipulator._array_probe_origin(_used(0, 1_048_575, 16383), avoid_col=0)


def test_measure_array_does_not_write_the_used_xfd_cells():
    # The size probe must land under the used area, then clear those same cells.
    written: list[tuple[int, int, str]] = []
    cleared: list[tuple[int, int]] = []

    class _Cursor:
        def gotoEndOfUsedArea(self, _expand):
            return None

        def getRangeAddress(self):
            return _used(0, 4, 16383)

    class _Probe:
        def __init__(self, col: int, row: int) -> None:
            self.col = col
            self.row = row

        def setArrayFormula(self, formula: str) -> None:
            written.append((self.col, self.row, formula))

        def clearContents(self, _flags: int) -> None:
            cleared.append((self.col, self.row))

    class _Cell:
        Error = 0

        def getValue(self) -> float:
            return 3.0

    class _Sheet:
        def createCursor(self) -> _Cursor:
            return _Cursor()

        def getCellRangeByPosition(self, col: int, row: int, _c2: int, _r2: int) -> _Probe:
            return _Probe(col, row)

        def getCellByPosition(self, _col: int, _row: int) -> _Cell:
            return _Cell()

    rows, cols = CellManipulator(MagicMock())._measure_array(_Sheet(), "=A1:A3", avoid_col=1)

    assert (rows, cols) == (3, 3)
    # Measure writes, then the finally block clears the same two cells.
    assert [pos[:2] for pos in written] == [(16383, 5), (16383, 6), (16383, 5), (16383, 6)]
    assert written[0][2].startswith("=ROWS(")
    assert written[1][2].startswith("=COLUMNS(")
    assert written[2][2] == "" and written[3][2] == ""
    assert cleared == [(16383, 5), (16383, 6)]
    assert all(row not in (0, 1) for _col, row, _formula in written)
