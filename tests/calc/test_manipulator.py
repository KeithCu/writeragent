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
    col, row = CellManipulator._array_probe_origin(_used(0, 20, 3), avoid_col=1, avoid_start_row=0, avoid_end_row=0)
    assert (col, row) == (5, 0)


def test_array_probe_clears_the_target_column():
    col, row = CellManipulator._array_probe_origin(_used(0, 20, 3), avoid_col=10, avoid_start_row=0, avoid_end_row=0)
    assert (col, row) == (12, 0)


def test_array_probe_shifts_below_used_rows_when_clamped_onto_xfd():
    # Used area already at XFD. min(..., 16383) used to overwrite rows 0–1.
    col, row = CellManipulator._array_probe_origin(_used(0, 10, 16383), avoid_col=5, avoid_start_row=0, avoid_end_row=0)
    assert col == 16383
    assert row == 11


def test_array_probe_stays_on_row_zero_when_xfd_used_rows_start_lower():
    # Data only on row 5 of XFD: rows 0–1 are outside the used rectangle.
    col, row = CellManipulator._array_probe_origin(_used(5, 5, 16383), avoid_col=0, avoid_start_row=0, avoid_end_row=0)
    assert (col, row) == (16383, 0)


def test_array_probe_does_not_shift_when_clamp_is_still_outside_used_columns():
    col, row = CellManipulator._array_probe_origin(_used(0, 10, 16381), avoid_col=0, avoid_start_row=0, avoid_end_row=0)
    assert (col, row) == (16383, 0)


def test_array_probe_refuses_when_xfd_has_no_rows_left():
    with pytest.raises(CalcError, match="column XFD"):
        CellManipulator._array_probe_origin(_used(0, 1_048_575, 16383), avoid_col=0, avoid_start_row=0, avoid_end_row=0)


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

    rows, cols = CellManipulator(MagicMock())._measure_array(_Sheet(), "=A1:A3", avoid_col=1, avoid_start_row=0, avoid_end_row=0)

    assert (rows, cols) == (3, 3)
    # Measure writes, then the finally block clears the same two cells.
    assert [pos[:2] for pos in written] == [(16383, 5), (16383, 6), (16383, 5), (16383, 6)]
    assert written[0][2].startswith("=ROWS(")
    assert written[1][2].startswith("=COLUMNS(")
    assert written[2][2] == "" and written[3][2] == ""
    assert cleared == [(16383, 5), (16383, 6)]
    assert all(row not in (0, 1) for _col, row, _formula in written)

def test_write_formula_range_rejects_wide_array():
    bridge = MagicMock()
    rng = MagicMock()
    addr = MagicMock()
    addr.StartColumn = 0
    addr.EndColumn = 1
    addr.StartRow = 0
    addr.EndRow = 1
    rng.getRangeAddress.return_value = addr
    bridge.resolve_range_or_address.return_value = rng
    manipulator = CellManipulator(bridge)
    from unittest.mock import patch
    with patch("plugin.calc.manipulator._uno_range_address", return_value=addr):
        with pytest.raises(CalcError, match="is too wide"):
            manipulator.write_formula_range("A1:B2", [[1, 2, 3], [4, 5, 6]])
def test_write_array_formula_refusal_leaves_prior_array():
    # Old array A1:B1. The new result is 1x3, and C1 already has a value.
    # Refusing must not clear A1:B1. B1 belongs to the array being replaced,
    # so the first reported occupied cell is C1, not B1.
    state = {"array": "=OLD"}
    writes: list[tuple[tuple[int, int, int, int], str]] = []

    class _Cell:
        def __init__(self, formula: str = "", text: str = "") -> None:
            self._formula = formula
            self._text = text

        def getFormula(self) -> str:
            return self._formula

        def getString(self) -> str:
            return self._text

    cells = {
        (0, 0): _Cell("=OLD"),
        (1, 0): _Cell("=OLD"),
        (2, 0): _Cell("", "keep"),
    }

    class _Cursor:
        def collapseToCurrentArray(self) -> None:
            return None

        def getRangeAddress(self) -> SimpleNamespace:
            return SimpleNamespace(StartColumn=0, StartRow=0, EndColumn=1, EndRow=0)

    class _Sheet:
        def createCursorByRange(self, _rng: object) -> _Cursor:
            return _Cursor()

        def getCellByPosition(self, col: int, row: int) -> _Cell:
            return cells[(col, row)]

        def getCellRangeByPosition(self, c1: int, r1: int, c2: int, r2: int):
            box = (c1, r1, c2, r2)

            class _Rng:
                def getArrayFormula(self) -> str:
                    if box == (0, 0, 1, 0):
                        return state["array"]
                    return ""

                def setArrayFormula(self, formula: str) -> None:
                    writes.append((box, formula))
                    if box == (0, 0, 1, 0):
                        state["array"] = formula

            return _Rng()

    manipulator = CellManipulator(MagicMock())
    manipulator._measure_array = MagicMock(return_value=(1, 3))

    with pytest.raises(CalcError, match=r"first: C1"):
        manipulator._write_array_formula(_Sheet(), "=NEW", (0, 0), (0, 0))

    assert state["array"] == "=OLD"
    assert writes == []


def test_write_array_formula_explicit_rejects_large_range():
    manipulator = CellManipulator(MagicMock())
    sheet = MagicMock()
    manipulator._measure_array = MagicMock(return_value=(2, 2))

    # 1000 * 1000 = 1000000 > MAX_ARRAY_CELLS (which is 100000)
    start = (0, 0)
    end = (999, 999)

    with pytest.raises(CalcError, match="limit"):
        manipulator._write_array_formula(sheet, "=1", start, end)

def test_re_raise_disposed_exception():
    manipulator = CellManipulator(MagicMock())
    sheet = MagicMock()

    # We mock getCellRangeByName to raise a RuntimeError matching disposed exception
    class DisposedException(RuntimeError):
        pass

    sheet.getCellRangeByName.side_effect = DisposedException("A mock disposed error")

    from plugin.framework.errors import is_disposed_exception
    assert is_disposed_exception(DisposedException("A mock disposed error"))

    with pytest.raises(DisposedException):
        manipulator.safe_get_cell_value(sheet, "A1")

def test_manipulator_hint_mapping():
    from plugin.calc.manipulator import CellManipulator
    from unittest.mock import MagicMock

    bridge = MagicMock()
    manip = CellManipulator(bridge)
