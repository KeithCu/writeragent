# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc cell manipulator helpers (no soffice)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import CalcError, UnoObjectError
from plugin.calc.manipulator import CellManipulator, WritePlan


def _used(start_row: int, end_row: int, end_col: int) -> SimpleNamespace:
    return SimpleNamespace(StartRow=start_row, EndRow=end_row, EndColumn=end_col)


@pytest.mark.parametrize(
    ("used_rect", "avoid_col", "expected"),
    [
        # Two columns right of used area
        ((0, 20, 3), 1, (5, 0)),
        # Clears the target column
        ((0, 20, 3), 10, (12, 0)),
        # Used area already at XFD: shift below used rows instead of overwriting rows 0-1
        ((0, 10, 16383), 5, (16383, 11)),
        # Data only on row 5 of XFD: rows 0-1 are outside the used rectangle
        ((5, 5, 16383), 0, (16383, 0)),
        # Clamp is still outside used columns: stay on row 0
        ((0, 10, 16381), 0, (16383, 0)),
    ],
)
def test_array_probe_origin(used_rect, avoid_col, expected):
    col, row = CellManipulator._array_probe_origin(_used(*used_rect), avoid_col=avoid_col, avoid_start_row=0, avoid_end_row=0)
    assert (col, row) == expected


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
        def __init__(self, formula: str = "") -> None:
            self._formula = formula

        def getFormula(self) -> str:
            return self._formula

    formulas = {
        (0, 0): "=OLD",
        (1, 0): "=OLD",
        (2, 0): "keep",
    }
    cells = {pos: _Cell(text) for pos, text in formulas.items()}

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

                def getFormulaArray(self) -> tuple[tuple[str, ...], ...]:
                    return tuple(
                        tuple(formulas.get((col, row), "") for col in range(c1, c2 + 1))
                        for row in range(r1, r2 + 1)
                    )

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


def test_classify_write_cell_data_text_keeps_formula_and_apostrophe_text():
    bridge = MagicMock()
    manipulator = CellManipulator(bridge)
    w_form = manipulator._classify_write_cell("=A1+1", None, None, data_text=True)
    assert w_form.kind == "forced_text" and w_form.formula == "" and w_form.data == "=A1+1"

    w_apos = manipulator._classify_write_cell("'tis", None, None, data_text=True)
    assert w_apos.kind == "forced_text" and w_apos.formula == "" and w_apos.data == "'tis"

    w_num = manipulator._classify_write_cell("42", None, None, data_text=True)
    assert w_num.kind == "number" and w_num.data == 42.0

    w_default = manipulator._classify_write_cell("=A1+1", None, None)
    assert w_default.kind == "formula" and w_default.formula == "=A1+1"


def test_literal_text_write_uses_setstring():
    addr = SimpleNamespace(StartColumn=0, EndColumn=1, StartRow=0, EndRow=0, Sheet=0)
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = addr
    sheet = MagicMock()
    cell_range.getSpreadsheet.return_value = sheet
    sheet.getCellRangeByPosition.return_value = cell_range
    cells: dict[tuple[int, int], MagicMock] = {}

    def get_cell(col, row):
        cells.setdefault((col, row), MagicMock())
        return cells[(col, row)]

    sheet.getCellByPosition.side_effect = get_cell
    formats = MagicMock()
    formats.getStandardIndex.return_value = 0
    doc = MagicMock()
    doc.getNumberFormats.return_value = formats
    doc.getPropertyValue.return_value = SimpleNamespace(Language="en", Country="US", Variant="")
    bridge = MagicMock()
    bridge.resolve_range_or_address.return_value = cell_range
    bridge.doc = doc
    CellManipulator(bridge).write_formula_range("A1", [["=SUM(A1)", "000123"]], literal_text=True)
    assert cells[(0, 0)].setString.call_args[0][0] == "=SUM(A1)"
    assert cells[(1, 0)].setString.call_args[0][0] == "000123"
    cells[(0, 0)].setFormula.assert_not_called()
    cells[(1, 0)].setFormula.assert_not_called()
    flat = [cell for row in cell_range.setDataArray.call_args[0][0] for cell in row]
    assert "=SUM(A1)" not in flat
    assert "000123" not in flat
    assert 123.0 not in flat


def test_data_text_write_uses_setstring_and_keeps_numbers():
    addr = SimpleNamespace(StartColumn=0, EndColumn=2, StartRow=0, EndRow=0, Sheet=0)
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = addr
    sheet = MagicMock()
    cell_range.getSpreadsheet.return_value = sheet
    sheet.getCellRangeByPosition.return_value = cell_range
    cells: dict[tuple[int, int], MagicMock] = {}

    def get_cell(col, row):
        cells.setdefault((col, row), MagicMock())
        return cells[(col, row)]

    sheet.getCellByPosition.side_effect = get_cell
    formats = MagicMock()
    formats.getStandardIndex.return_value = 0
    doc = MagicMock()
    doc.getNumberFormats.return_value = formats
    doc.getPropertyValue.return_value = SimpleNamespace(Language="en", Country="US", Variant="")
    bridge = MagicMock()
    bridge.resolve_range_or_address.return_value = cell_range
    bridge.doc = doc
    CellManipulator(bridge).write_formula_range("A1", [["=SUM(A1)", "'x", 42.0]], data_text=True)
    assert cells[(0, 0)].setString.call_args[0][0] == "=SUM(A1)"
    assert cells[(1, 0)].setString.call_args[0][0] == "'x"
    cells[(0, 0)].setFormula.assert_not_called()
    cells[(1, 0)].setFormula.assert_not_called()
    cells[(2, 0)].setFormula.assert_not_called()
    row_data = list(cell_range.setDataArray.call_args[0][0][0])
    assert row_data == ["", "", 42.0]


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
        with patch("plugin.calc.manipulator.resolve_document_locale", return_value=MagicMock()):
            res = manipulator.write_formula_range("A1:range", ["1"] * MAX_WRITE_CELLS)
            assert "filled with" in res["message"]


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
    bridge = MagicMock()
    mock_range = MagicMock()
    mock_addr = SimpleNamespace(StartColumn=0, EndColumn=1, StartRow=0, EndRow=10)
    bridge.resolve_range_or_address.return_value = mock_range

    manipulator = CellManipulator(bridge)
    with (
        patch("plugin.calc.manipulator._uno_range_address", return_value=mock_addr),
        patch("plugin.calc.manipulator.TableSortField", create=True) as tsf_cls,
    ):
        with pytest.raises(CalcError, match=r"0 to 1"):
            manipulator.range_sort("A1:B10", sort_column=2)
        mock_range.createSortDescriptor.assert_not_called()

        p_sort_fields = SimpleNamespace(Name="SortFields", Value=())
        p_contains_header = SimpleNamespace(Name="ContainsHeader", Value=False)
        mock_range.createSortDescriptor.return_value = [p_sort_fields, p_contains_header]

        with patch("uno.Any") as any_mock:
            mock_key_range = mock_range.getCellRangeByPosition.return_value
            mock_key_range.getDataArray.return_value = (("a",), ("b",))
            res = manipulator.range_sort("A1:B10", sort_column=0, ascending=True, has_header=True)
            mock_range.sort.assert_called_once()
            any_mock.assert_called_once_with("[]com.sun.star.table.TableSortField", (tsf_cls.return_value,))
            assert p_sort_fields.Value == any_mock.return_value
            assert p_contains_header.Value is True
            mock_range.getCellRangeByPosition.assert_called_with(0, 1, 0, 10)
            assert "no rows moved" in res

            mock_key_range.getDataArray.side_effect = [(("b",), ("a",)), (("a",), ("b",))]
            res_moved = manipulator.range_sort("A1:B10", sort_column=0, ascending=True, has_header=True)
            assert "sorted ascending by column 0" in res_moved
            assert "no rows moved" not in res_moved


def test_sort_range_skips_key_read_for_single_data_row():
    bridge = MagicMock()
    mock_range = MagicMock()
    mock_addr = SimpleNamespace(StartColumn=0, EndColumn=1, StartRow=0, EndRow=1)
    bridge.resolve_range_or_address.return_value = mock_range
    mock_range.createSortDescriptor.return_value = []

    manipulator = CellManipulator(bridge)
    with (
        patch("plugin.calc.manipulator._uno_range_address", return_value=mock_addr),
        patch("plugin.calc.manipulator.TableSortField", create=True),
        patch("uno.Any"),
    ):
        res = manipulator.range_sort("A1:B2", sort_column=0, ascending=True, has_header=True)
        mock_range.getCellRangeByPosition.assert_not_called()
        mock_range.sort.assert_called_once()
        assert "no rows moved" in res


def test_quiet_probe_cleanup():
    from plugin.calc.manipulator import quiet_probe_cleanup

    DisposedException = type("DisposedException", (Exception,), {})

    def raise_disposed():
        raise DisposedException("disposed")

    with pytest.raises(DisposedException):
        quiet_probe_cleanup(raise_disposed, "test_disposed")

    def raise_val_error():
        raise ValueError("swallowed")

    # Does not raise
    quiet_probe_cleanup(raise_val_error, "test_swallowed")


def test_plan_write_array_false_does_not_force_array():
    bridge = MagicMock()
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=4, Sheet=0)
    bridge.resolve_range_or_address.return_value = cell_range
    manipulator = CellManipulator(bridge)

    plan = manipulator.plan_write("A1:A5", "=A1+1", array=False)
    assert plan.kind == "cells"
    assert plan.array_size is None

    plan_sort = manipulator.plan_write("A1:A5", "=SORT(B1:B5)", array=False)
    assert plan_sort.kind == "cells"
    assert plan_sort.array_size is None


def test_commit_write_always_returns_a_message_record():
    bridge = MagicMock()
    manipulator = CellManipulator(bridge)

    with patch.object(manipulator, "clear_range") as mock_clear:
        clear_res = manipulator.commit_write(WritePlan("clear", "A1:B2"))
    mock_clear.assert_called_once_with("A1:B2")
    assert clear_res == {"message": "Range A1:B2 cleared."}

    small_addr = SimpleNamespace(StartColumn=0, EndColumn=1, StartRow=0, EndRow=0, Sheet=0)
    mock_range = MagicMock()
    mock_sheet = MagicMock()
    mock_range.getSpreadsheet.return_value = mock_sheet
    mock_range.getDataArray.return_value = ()
    bridge.resolve_range_or_address.return_value = mock_range
    bridge.doc.getNumberFormats.return_value = MagicMock()
    bridge.doc.getSheets.return_value.getByIndex.return_value = mock_sheet

    with patch("plugin.calc.manipulator._uno_range_address", return_value=small_addr):
        with patch("plugin.calc.manipulator.resolve_document_locale", return_value=MagicMock()):
            res = manipulator.write_formula_range("A1:B1", ["1", "2"])
    assert set(res) == {"message"}
    assert "filled with" in res["message"]


def test_plan_write_shape_comes_from_the_resolved_named_box():
    from plugin.framework.errors import CalcError

    bridge = MagicMock()
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = SimpleNamespace(StartColumn=1, EndColumn=1, StartRow=0, EndRow=2, Sheet=0)
    bridge.resolve_range_or_address.return_value = cell_range
    manipulator = CellManipulator(bridge)

    with pytest.raises(CalcError, match="2 values but range Totals has 3 cells"):
        manipulator.plan_write("Totals", ["x", "y"])


def test_plan_write_single_cell_prose_is_one_value():
    bridge = MagicMock()
    one_cell = MagicMock()
    one_cell.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0, Sheet=0)
    bridge.resolve_range_or_address.return_value = one_cell
    manipulator = CellManipulator(bridge)

    plan_single = manipulator.plan_write("A1", "Hello, world")
    assert plan_single.values == ["Hello, world"]

    two_cells = MagicMock()
    two_cells.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, EndColumn=1, StartRow=0, EndRow=0, Sheet=0)
    bridge.resolve_range_or_address.return_value = two_cells

    plan_pair = manipulator.plan_write("A1:B1", "Hello, world")
    assert plan_pair.values == ["Hello", "world"]


def test_plan_write_expands_2d_from_one_cell():
    bridge = MagicMock()
    one_cell = MagicMock()
    one_cell.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0, Sheet=0)
    sheet = MagicMock()
    one_cell.getSpreadsheet.return_value = sheet
    bridge.resolve_range_or_address.return_value = one_cell
    manipulator = CellManipulator(bridge)

    plan = manipulator.plan_write("A1", [[1, 2], [3, 4]])
    assert plan.range_str == "A1:B2"
    assert plan.expanded is True
    assert plan.values == [1, 2, 3, 4]
    sheet.getCellByPosition.assert_not_called()


def test_write_values_grid_cell_error_rejects_mixed_rows():
    from plugin.calc.manipulator import _write_values_grid_cell_error

    assert _write_values_grid_cell_error([["a"], "b"]) is not None
    assert _write_values_grid_cell_error([{"x": 1}]) is not None
    assert _write_values_grid_cell_error([["a", True]]) is None


@pytest.mark.parametrize(("v_align", "expected"), [("top", 1), ("center", 2), ("bottom", 3)])
def test_set_cell_style_v_align_writes_vert_justify_ints(v_align: str, expected: int):
    bridge = MagicMock()
    cell = MagicMock()
    bridge.resolve_range_or_address.return_value = cell
    manipulator = CellManipulator(bridge)

    manipulator.set_cell_style("A1", v_align=v_align)

    cell.setPropertyValue.assert_any_call("VertJustify", expected)

    cell.reset_mock()
    manipulator.set_cell_style("A1", v_align="middle")
    assert not any(call.args[0] == "VertJustify" for call in cell.setPropertyValue.call_args_list)


def test_plan_write_lets_bridge_error_through():
    bridge = MagicMock()
    bridge.resolve_range_or_address.side_effect = UnoObjectError(
        "No sheet named 'Nope'. Available: Sheet1",
        details={"hint": "h"},
    )
    manipulator = CellManipulator(bridge)

    with pytest.raises(UnoObjectError) as exc_plan:
        manipulator.plan_write("Nope.A1", "1")
    assert exc_plan.value.details == {"hint": "h"}

    with pytest.raises(UnoObjectError) as exc_copy:
        manipulator.copy_formula_range("Nope.A1", "A1")
    assert exc_copy.value.details == {"hint": "h"}


def test_refuse_occupied_cells_reads_once_and_skips_boxes():
    from plugin.calc.manipulator import refuse_occupied_cells

    sheet = MagicMock()
    sheet.getCellRangeByPosition.return_value.getFormulaArray.return_value = (
        ("a", "b", ""),
        ("", "", "c"),
    )

    with pytest.raises(CalcError, match=r"needs A1:C2, but 1 cell\(s\).*\(first: C2\)"):
        refuse_occupied_cells(sheet, 0, 0, 2, 1, skip=[(0, 0, 1, 0)])

    sheet.getCellRangeByPosition.assert_called_once_with(0, 0, 2, 1)
    sheet.getCellByPosition.assert_not_called()

    empty_sheet = MagicMock()
    empty_sheet.getCellRangeByPosition.return_value.getFormulaArray.return_value = (
        ("", ""),
        ("", ""),
    )
    refuse_occupied_cells(empty_sheet, 0, 0, 1, 1)
