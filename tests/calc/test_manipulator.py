# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc cell manipulator helpers (no soffice)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import CalcError
from plugin.calc.manipulator import CellManipulator, _coerce_array_flag


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
        with pytest.raises(CalcError, match=r"Array is 2x3 but range is 2x2"):
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


def test_classify_write_cell_boolean():
    manipulator = CellManipulator(MagicMock())
    w = manipulator._classify_write_cell(True, None, None)
    assert w.data == 1.0
    assert w.formula == ""
    assert w.kind == "number"

    w_f = manipulator._classify_write_cell(False, None, None)
    assert w_f.data == 0.0
    assert w_f.formula == ""
    assert w_f.kind == "number"

    w_str = manipulator._classify_write_cell("True", None, None)
    assert w_str.data == "True"
    assert w_str.formula == ""
    assert w_str.kind == "text"


def test_classify_write_cell_matrix():
    manipulator = CellManipulator(MagicMock())
    # int / float
    w_int = manipulator._classify_write_cell(42, None, None)
    assert w_int.data == 42 and w_int.formula == "" and w_int.kind == "number"
    w_float = manipulator._classify_write_cell(3.14, None, None)
    assert w_float.data == 3.14 and w_float.formula == "" and w_float.kind == "number"

    # numeric string
    w_numstr = manipulator._classify_write_cell("123.45", None, None)
    assert w_numstr.data == 123.45 and w_numstr.formula == "" and w_numstr.kind == "number"

    # formula
    w_form = manipulator._classify_write_cell("=A1+1", None, None)
    assert w_form.data == "" and w_form.formula == "=A1+1" and w_form.kind == "formula"

    # apostrophe text
    w_apos = manipulator._classify_write_cell("'00123", None, None)
    assert w_apos.data == "00123" and w_apos.formula == "" and w_apos.kind == "forced_text"

    # literal text
    w_lit = manipulator._classify_write_cell("00123", None, None, literal_text=True)
    assert w_lit.data == "00123" and w_lit.formula == "" and w_lit.kind == "forced_text"

    # ISO date/time
    mock_formatter = MagicMock()
    mock_formatter.detectNumberFormat.return_value = 101
    mock_formatter.convertStringToNumber.return_value = 45000.0
    w_iso = manipulator._classify_write_cell("2024-01-01", mock_formatter, 0)
    assert w_iso.data == 45000.0 and w_iso.kind == "temporal" and w_iso.detected_key == 101


def test_oversized_write_raises_limit():
    from plugin.calc.manipulator import MAX_WRITE_CELLS

    bridge = MagicMock()
    huge_addr = SimpleNamespace(StartColumn=0, EndColumn=16383, StartRow=0, EndRow=1048575, Sheet=0)
    mock_sheet = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.getRangeAddress.return_value = SimpleNamespace(EndColumn=0, EndRow=0)
    mock_sheet.createCursor.return_value = mock_cursor
    bridge.doc.getSheets.return_value.getByIndex.return_value = mock_sheet
    bridge.resolve_range_or_address.return_value = MagicMock()
    manipulator = CellManipulator(bridge)

    with patch("plugin.calc.manipulator._uno_range_address", return_value=huge_addr):
        with pytest.raises(CalcError, match="write limit"):
            manipulator.write_formula_range("A1:XFD1048576", "1")

        # Empty clear of huge range still works (clear_range is uncapped)
        manipulator.clear_range("A1:XFD1048576")

    # Exactly MAX_WRITE_CELLS is accepted (mocking setDataArray/write path)
    exact_addr = SimpleNamespace(StartColumn=0, EndColumn=MAX_WRITE_CELLS - 1, StartRow=0, EndRow=0, Sheet=0)
    mock_range = MagicMock()
    mock_sheet = MagicMock()
    mock_range.getSpreadsheet.return_value = mock_sheet
    mock_range.getDataArray.return_value = ()
    bridge.resolve_range_or_address.return_value = mock_range
    bridge.doc.getNumberFormats.return_value = MagicMock()
    bridge.doc.getSheets.return_value.getByIndex.return_value = mock_sheet

    with patch("plugin.calc.manipulator._uno_range_address", return_value=exact_addr):
        with patch.object(manipulator, "_resolve_document_locale", return_value=MagicMock()):
            res = manipulator.write_formula_range("A1:range", ["1"] * MAX_WRITE_CELLS)
            assert "filled with" in res


def test_array_preview_capped():
    bridge = MagicMock()
    doc = MagicMock()
    bridge.doc = doc
    manipulator = CellManipulator(bridge)

    sheet = MagicMock()
    preview_data = tuple((float(i),) * 10 for i in range(3))
    mock_preview_rng = MagicMock()
    mock_preview_rng.getDataArray.return_value = preview_data
    sheet.getCellRangeByPosition.return_value = mock_preview_rng

    anchor = MagicMock()
    anchor.Error = 0
    anchor.getFormula.return_value = ""
    anchor.getString.return_value = ""
    sheet.getCellByPosition.return_value = anchor

    res = manipulator._write_array_formula(
        sheet,
        "=MUNIT(20)",
        start=(0, 0),
        end=(19, 2),
        premeasured_array_size=(3, 20),
    )
    assert res["rows"] == 3
    assert res["cols"] == 20
    assert len(res["preview"]) == 3
    assert len(res["preview"][0]) == 10
    assert res.get("preview_truncated") is True


def test_sort_range_bounds_and_valid_types():
    from unittest.mock import patch
    bridge = MagicMock()
    mock_range = MagicMock()
    mock_addr = SimpleNamespace(StartColumn=0, EndColumn=1, StartRow=0, EndRow=10)
    bridge.resolve_range_or_address.return_value = mock_range

    manipulator = CellManipulator(bridge)
    with (
        patch("plugin.calc.manipulator._uno_range_address", return_value=mock_addr),
        patch("plugin.calc.manipulator.TableSortField", create=True),
    ):
        with pytest.raises(CalcError, match=r"0 to 1"):
            manipulator.sort_range("A1:B10", sort_column=2)
        mock_range.createSortDescriptor.assert_not_called()

        mock_range.createSortDescriptor.return_value = []
        res = manipulator.sort_range("A1:B10", sort_column=0, ascending=True, has_header=True)
        assert "sorted" in res
        mock_range.sort.assert_called_once()


def test_quiet_probe_cleanup():
    from plugin.calc.manipulator import _quiet_probe_cleanup

    DisposedException = type("DisposedException", (Exception,), {})

    def raise_disposed():
        raise DisposedException("disposed")

    with pytest.raises(DisposedException):
        _quiet_probe_cleanup(raise_disposed, "test_disposed")

    def raise_val_error():
        raise ValueError("swallowed")

    # Does not raise
    _quiet_probe_cleanup(raise_val_error, "test_swallowed")


def test_coerce_array_flag_string_regression():
    # Item 3: String like "false" must not count as True.
    assert _coerce_array_flag("false") is False
    assert _coerce_array_flag("False") is False
    assert _coerce_array_flag(" FALSE ") is False
    assert _coerce_array_flag("true") is True
    assert _coerce_array_flag("True") is True
    assert _coerce_array_flag(False) is False
    assert _coerce_array_flag(True) is True
    assert _coerce_array_flag(None) is None


def test_prepare_array_formula_string_false_does_not_force_array():
    # Regression: array="false" must coerce to False, disabling forced array behavior
    bridge = MagicMock()
    manipulator = CellManipulator(bridge)
    res = manipulator.prepare_array_formula_if_needed("A1:A5", "=A1+1", array="false")
    assert res is None
    res_sort = manipulator.prepare_array_formula_if_needed("A1:A5", "=SORT(B1:B5)", array="false")
    assert res_sort is None

