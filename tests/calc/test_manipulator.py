# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc cell manipulator helpers (no soffice)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.array_formula import CellRect
from plugin.framework.errors import CalcError, UnoObjectError
from plugin.calc.manipulator import (
    CellManipulator,
    WritePlan,
    _delimited_text_message,
    _parse_json_array_string,
    _ref_named_ranges,
    _write_report,
    looks_numeric_text,
    parse_then_by,
)
from plugin.tests.testing_utils import DisposedException


def _used(end_row: int, end_col: int, sheet: int = 0) -> SimpleNamespace:
    return SimpleNamespace(Sheet=sheet, EndRow=end_row, EndColumn=end_col)


@pytest.mark.parametrize(
    ("used_rect", "target", "refs", "expected"),
    [
        # Two columns right of used area
        ((20, 3), CellRect(0, 0, 1, 0), [], (5, 0)),
        # Clears the target column
        ((20, 3), CellRect(0, 0, 10, 0), [], (12, 0)),
        # Used area already at XFD: shift below used rows instead of overwriting rows 0-1
        ((10, 16383), CellRect(0, 0, 5, 0), [], (16383, 11)),
        # Cursor does not report start row, so rows 0..EndRow count as used
        ((5, 16383), CellRect(0, 0, 0, 0), [], (16383, 6)),
        # Clamp is still outside used columns: stay on row 0
        ((10, 16381), CellRect(0, 0, 0, 0), [], (16383, 0)),
        # Whole-row ref (1:3) with target H20 shifts below target
        ((2, 7), CellRect(7, 19, 7, 19), [CellRect(0, 0, 16383, 2)], (9, 20)),
        # Generous ref past used area (F1:Z1000) with target D1 shifts right of ref
        ((0, 0), CellRect(3, 0, 3, 0), [CellRect(5, 0, 25, 999)], (26, 0)),
        # Whole-sheet ref falls back to ignoring refs
        ((0, 0), CellRect(0, 0, 0, 0), [CellRect(0, 0, 16383, 1_048_575)], (2, 0)),
    ],
)
def test_array_probe_origin(used_rect, target, refs, expected):
    col, row = CellManipulator._array_probe_origin(_used(*used_rect), target, refs)
    assert (col, row) == expected


def test_array_probe_refuses_when_xfd_has_no_rows_left():
    with pytest.raises(CalcError, match="column XFD"):
        CellManipulator._array_probe_origin(_used(1_048_575, 16383), CellRect(0, 0, 0, 0), [])


def test_measure_array_does_not_write_the_used_xfd_cells():
    # The size probe must land under the used area, then clear those same cells.
    written: list[tuple[int, int, str]] = []
    cleared: list[tuple[int, int]] = []

    class _Cursor:
        def gotoEndOfUsedArea(self, _expand):
            return None

        def getRangeAddress(self):
            return _used(4, 16383)

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

    rows, cols = CellManipulator(MagicMock())._measure_array(_Sheet(), "=A1:A3", CellRect(0, 0, 1, 0))

    assert (rows, cols) == (3, 3)
    # Measure writes, then the finally block clears the same two cells.
    assert [pos[:2] for pos in written] == [(16383, 5), (16383, 6), (16383, 5), (16383, 6)]
    assert written[0][2].startswith("=ROWS(")
    assert written[1][2].startswith("=COLUMNS(")
    assert written[2][2] == "" and written[3][2] == ""
    assert cleared == [(16383, 5), (16383, 6)]
    assert all(row not in (0, 1) for _col, row, _formula in written)


def _make_measure_probe_sheet(own_error: int, own_text: str = "5"):
    cleared: list[tuple[int, int]] = []
    written: dict[tuple[int, int], str] = {}

    class _Cursor:
        def gotoEndOfUsedArea(self, _expand):
            return None

        def getRangeAddress(self):
            return _used(0, 0)

    class _Probe:
        def __init__(self, col: int, row: int) -> None:
            self.col = col
            self.row = row

        def setArrayFormula(self, formula: str) -> None:
            written[(self.col, self.row)] = formula

        def clearContents(self, _flags: int) -> None:
            cleared.append((self.col, self.row))

    class _Cell:
        def __init__(self, col: int, row: int) -> None:
            self.col = col
            self.row = row

        @property
        def Error(self) -> int:
            formula = written.get((self.col, self.row), "")
            if formula.startswith(("=ROWS(", "=COLUMNS(")):
                return 504
            if formula.startswith("=("):
                return own_error
            return 0

        def getString(self) -> str:
            err = self.Error
            if err == 504:
                return "Err:504"
            return f"Err:{err}" if err else own_text

    class _Sheet:
        def createCursor(self) -> _Cursor:
            return _Cursor()

        def getCellRangeByPosition(self, col: int, row: int, _c2: int, _r2: int) -> _Probe:
            return _Probe(col, row)

        def getCellByPosition(self, col: int, row: int) -> _Cell:
            return _Cell(col, row)

    return _Sheet(), cleared


def test_measure_array_single_value_is_1x1():
    sheet, cleared = _make_measure_probe_sheet(0)
    rows, cols = CellManipulator(MagicMock())._measure_array(sheet, "=SUM(1;2)", CellRect(0, 0, 0, 0))

    assert (rows, cols) == (1, 1)
    assert cleared == [(2, 0), (2, 1)]


@pytest.mark.parametrize(
    ("err", "expected_sub", "absent_sub"),
    [
        (532, "code 532", "FILTER"),
        (533, "FILTER", None),
        (525, "unknown function", "FILTER"),
    ],
)
def test_measure_array_reports_the_formulas_own_error(err: int, expected_sub: str, absent_sub: str | None):
    sheet, cleared = _make_measure_probe_sheet(err)
    with pytest.raises(CalcError) as exc_info:
        CellManipulator(MagicMock())._measure_array(sheet, "=1/0", CellRect(0, 0, 0, 0))
    msg = str(exc_info.value)
    assert expected_sub in msg
    if absent_sub is not None:
        assert absent_sub not in msg
    assert cleared == [(2, 0), (2, 1)]


def test_measure_array_refuses_py_failure_text_and_allows_native_text():
    failure = "Error: Python venv not found. Open Settings → Python, set the venv path, then Test."
    sheet_py, cleared_py = _make_measure_probe_sheet(0, own_text=failure)
    with pytest.raises(CalcError, match="Python venv not found"):
        CellManipulator(MagicMock())._measure_array(sheet_py, '=PY("result = 1")', CellRect(0, 0, 0, 0))
    assert cleared_py == [(2, 0), (2, 1)]

    sheet_native, cleared_native = _make_measure_probe_sheet(0, own_text=failure)
    assert CellManipulator(MagicMock())._measure_array(sheet_native, "=A1", CellRect(0, 0, 0, 0)) == (1, 1)
    assert cleared_native == [(2, 0), (2, 1)]


def test_write_array_formula_reads_error_before_clearing():
    class _Anchor:
        def __init__(self) -> None:
            self.Error = 522
            self._text = "Err:522"

        def getString(self) -> str:
            return self._text

    anchor = _Anchor()

    class _Range:
        def setArrayFormula(self, formula: str) -> None:
            if not formula:
                anchor.Error = 0
                anchor._text = ""

        def clearContents(self, _flags: int) -> None:
            anchor.Error = 0
            anchor._text = ""

        def getArrayFormula(self) -> str:
            return ""

    class _Sheet:
        def getCellRangeByPosition(self, *_args):
            return _Range()

        def getCellByPosition(self, *_args):
            return anchor

        def createCursorByRange(self, _rng):
            cur = MagicMock()
            cur.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, StartRow=0, EndColumn=0, EndRow=0)
            return cur

    manipulator = CellManipulator(MagicMock())
    with patch("plugin.calc.manipulator.refuse_occupied_cells"):
        with pytest.raises(CalcError, match="code 522"):
            manipulator._write_array_formula(_Sheet(), "=SUM(A1:A3)", (0, 0), (0, 0), premeasured_array_size=(1, 1))


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
    with patch("plugin.calc.manipulator._uno_range_address", return_value=addr):
        with pytest.raises(CalcError) as exc_info:
            manipulator.write_formula_range("A1:B2", [[1, 2, 3], [4, 5, 6]])
    msg = str(exc_info.value)
    assert "A1:B2" in msg
    assert "2×3" in msg
    assert "2×2" in msg
    assert "starting at A1" in msg


def test_write_formula_range_ragged_grid_names_row():
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
    with patch("plugin.calc.manipulator._uno_range_address", return_value=addr):
        with pytest.raises(CalcError) as exc_info:
            manipulator.write_formula_range("A1:B2", [[1, 2], [3]])
    msg = str(exc_info.value)
    assert "row 2" in msg
    assert "A1:B2" in msg


def test_write_formula_range_ragged_grid_single_cell_names_row():
    bridge = MagicMock()
    rng = MagicMock()
    addr = MagicMock()
    addr.StartColumn = 0
    addr.EndColumn = 0
    addr.StartRow = 0
    addr.EndRow = 0
    rng.getRangeAddress.return_value = addr
    bridge.resolve_range_or_address.return_value = rng
    manipulator = CellManipulator(bridge)
    with patch("plugin.calc.manipulator._uno_range_address", return_value=addr):
        with pytest.raises(CalcError) as exc_info:
            manipulator.write_formula_range("A1", [[1, 2], [3]])
    assert "row 2" in str(exc_info.value)


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


def test_classify_write_cell_repairs_py_literal_newlines():
    manipulator = CellManipulator(MagicMock())
    repaired = manipulator._classify_write_cell('=PY("x = 2\\nresult = x")', None, None)
    assert repaired.kind == "formula"
    assert repaired.formula == '=PY("x = 2\nresult = x")'

    as_data = manipulator._classify_write_cell('=PY("x = 2\\nresult = x")', None, None, data_text=True)
    assert as_data.kind == "forced_text"
    assert as_data.data == '=PY("x = 2\\nresult = x")'

    as_literal = manipulator._classify_write_cell('=PY("x = 2\\nresult = x")', None, None, literal_text=True)
    assert as_literal.kind == "forced_text"
    assert as_literal.data == '=PY("x = 2\\nresult = x")'


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
        with pytest.raises(CalcError, match=r"0 to 1") as exc_info:
            manipulator.plan_sort("A1:B10", [(2, True)])
        assert exc_info.value.code == "INVALID_PARAM"
        assert "A1:B10" in str(exc_info.value)
        with pytest.raises(CalcError, match=r"0 to 1") as exc_neg:
            manipulator.plan_sort("A1:B10", [(-1, True)])
        assert exc_neg.value.code == "INVALID_PARAM"
        assert "A1:B10" in str(exc_neg.value)
        mock_range.createSortDescriptor.assert_not_called()

        p_sort_fields = SimpleNamespace(Name="SortFields", Value=())
        p_contains_header = SimpleNamespace(Name="ContainsHeader", Value=False)
        mock_range.createSortDescriptor.return_value = [p_sort_fields, p_contains_header]

        with patch("uno.Any") as any_mock:
            mock_key_range = mock_range.getCellRangeByPosition.return_value
            mock_key_range.getDataArray.return_value = (("a",), ("b",))
            res = manipulator.range_sort(manipulator.plan_sort("A1:B10", [(0, True)]), has_header=True)
            mock_range.sort.assert_called_once()
            any_mock.assert_called_once_with("[]com.sun.star.table.TableSortField", (tsf_cls.return_value,))
            assert p_sort_fields.Value == any_mock.return_value
            assert p_contains_header.Value is True
            mock_range.getCellRangeByPosition.assert_called_with(0, 1, 0, 10)
            assert "no rows moved" in res

            mock_key_range.getDataArray.side_effect = [(("b",), ("a",)), (("a",), ("b",))]
            res_moved = manipulator.range_sort(manipulator.plan_sort("A1:B10", [(0, True)]), has_header=True)
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
        res = manipulator.range_sort(manipulator.plan_sort("A1:B2", [(0, True)]), has_header=True)
        mock_range.getCellRangeByPosition.assert_not_called()
        mock_range.sort.assert_called_once()
        assert "no rows moved" in res


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([], []),
        ([{"column": 0, "ascending": True}], [(0, True)]),
        ([{"column": 1.0, "ascending": False}], [(1, False)]),
        ({"column": 0, "ascending": True}, None),
        ([{"column": 0}], None),
        ([{"column": 0, "ascending": True, "sort_column": 1}], None),
        ([{"column": True, "ascending": True}], None),
        ([{"column": -1, "ascending": True}], None),
        ([{"column": 1.5, "ascending": True}], None),
        ([{"column": "A", "ascending": True}], None),
        ([{"column": 0, "ascending": "false"}], None),
    ],
)
def test_parse_then_by(raw, expected):
    assert parse_then_by(raw) == expected


def test_range_sort_then_by_out_of_width_refused():
    bridge = MagicMock()
    mock_range = MagicMock()
    mock_addr = SimpleNamespace(StartColumn=0, EndColumn=1, StartRow=0, EndRow=10)
    bridge.resolve_range_or_address.return_value = mock_range

    manipulator = CellManipulator(bridge)
    with patch("plugin.calc.manipulator._uno_range_address", return_value=mock_addr):
        with pytest.raises(CalcError, match=r"then_by column") as exc_info:
            manipulator.plan_sort("A1:B10", [(0, True), (5, False)])
        assert exc_info.value.code == "INVALID_PARAM"
        assert "A1:B10" in str(exc_info.value)
    mock_range.sort.assert_not_called()


def test_range_sort_tie_reorder_is_not_no_rows_moved():
    bridge = MagicMock()
    mock_range = MagicMock()
    mock_addr = SimpleNamespace(StartColumn=0, EndColumn=1, StartRow=0, EndRow=2)
    bridge.resolve_range_or_address.return_value = mock_range
    mock_range.createSortDescriptor.return_value = []

    col1_range = MagicMock()
    col1_range.getDataArray.side_effect = [(("10",), ("10",)), (("10",), ("10",))]
    col0_range = MagicMock()
    col0_range.getDataArray.side_effect = [(("b",), ("a",)), (("a",), ("b",))]
    mock_range.getCellRangeByPosition.side_effect = [col1_range, col0_range, col1_range, col0_range]

    manipulator = CellManipulator(bridge)
    with (
        patch("plugin.calc.manipulator._uno_range_address", return_value=mock_addr),
        patch("plugin.calc.manipulator.TableSortField", create=True),
        patch("uno.Any"),
    ):
        res = manipulator.range_sort(manipulator.plan_sort("A1:B3", [(1, False), (0, True)]), has_header=True)
    assert "no rows moved" not in res
    assert "column 1 descending, then column 0 ascending" in res


def test_quiet_probe_cleanup():
    from plugin.calc.manipulator import quiet_probe_cleanup

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


def test_plan_write_array_py_repairs_literal_newlines():
    bridge = MagicMock()
    cell_range = MagicMock()
    cell_range.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0, Sheet=0)
    bridge.resolve_range_or_address.return_value = cell_range
    manipulator = CellManipulator(bridge)

    plan = manipulator.plan_write(
        "A1",
        '=PY("x = 2\\nresult = x")',
        array=True,
        premeasured_array_size=(1, 1),
    )
    assert plan.kind == "array"
    assert plan.values == '=PY("x = 2\nresult = x")'


def test_plan_write_refuses_py_array_before_268():
    bridge = MagicMock()
    cell_range = MagicMock()
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    cell_range.getSpreadsheet.return_value = sheet
    cell_range.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=2, Sheet=0)
    bridge.resolve_range_or_address.return_value = cell_range
    manipulator = CellManipulator(bridge)

    with (
        patch("plugin.calc.manipulator.accepts_returned_any", return_value=False),
        patch.object(manipulator, "_probe_array_formula") as mock_probe,
    ):
        with pytest.raises(CalcError) as exc_info:
            manipulator.plan_write("A1:A3", '=PY("result = [1, 2]")', array=True)
        assert exc_info.value.code == "INVALID_PARAM"
        assert exc_info.value.details["suggested_call"]["arguments"]["range"] == ["Sheet1.A1"]
        mock_probe.assert_not_called()

    with patch("plugin.calc.manipulator.accepts_returned_any", return_value=True):
        plan_ok = manipulator.plan_write(
            "A1:A3",
            '=PY("result = [1, 2]")',
            array=True,
            premeasured_array_size=(2, 1),
        )
        assert plan_ok.kind == "array"


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

    with pytest.raises(CalcError) as exc:
        manipulator.plan_write("A1:B1", "Hello, world")
    assert exc.value.code == "INVALID_PARAM"


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

    with pytest.raises(CalcError, match=r"needs A1:C2, but 1 cell\(s\).*\(first: C2\)") as exc:
        refuse_occupied_cells(sheet, 0, 0, 2, 1, skip=[(0, 0, 1, 0)])
    assert exc.value.code == "SPILL_BLOCKED"
    assert exc.value.details == {
        "needed_range": "A1:C2",
        "blocking_cells": ["C2"],
        "blocking_count": 1,
    }

    sheet.getCellRangeByPosition.assert_called_once_with(0, 0, 2, 1)
    sheet.getCellByPosition.assert_not_called()

    empty_sheet = MagicMock()
    empty_sheet.getCellRangeByPosition.return_value.getFormulaArray.return_value = (
        ("", ""),
        ("", ""),
    )
    refuse_occupied_cells(empty_sheet, 0, 0, 1, 1)


class TestFormulaParsingLogic:
    def test_parse_json_array(self):
        s = '["A"; "B"; "C"]'
        assert _parse_json_array_string(s) == ["A", "B", "C"]

    def test_complex_formula_in_json_array(self):
        s = '["Highest Consumer"; "=INDEX(A2:A11;MATCH(MAX(B2:B11);B2:B11;0))"]'
        # Ensure the semicolon inside the formula is NOT replaced by a comma
        result = _parse_json_array_string(s)
        assert result == ["Highest Consumer", "=INDEX(A2:A11;MATCH(MAX(B2:B11);B2:B11;0))"]

    def test_formula_not_split(self):
        # Should return None so it's treated as a single formula string
        assert _parse_json_array_string("=SUM(A1;A2)") is None

    def test_single_value_not_split(self):
        assert _parse_json_array_string("Plain text") is None
        assert _parse_json_array_string("Name;Age;Salary") is None

    def test_nested_json_arrays(self):
        s = '[["r1c1"; "r1c2"]; ["r2c1"; "r2c2"]]'
        assert _parse_json_array_string(s) == [["r1c1", "r1c2"], ["r2c1", "r2c2"]]

    def test_unicode_and_emoji(self):
        s = '["Česká"; "Republika"; "📈"]'
        assert _parse_json_array_string(s) == ["Česká", "Republika", "📈"]

    def test_quoted_semicolon_in_formula_json(self):
        s = '["=IF(A1=\\";\\"; 1; 0)"]'
        assert _parse_json_array_string(s) == ['=IF(A1=";"; 1; 0)']

    def test_trailing_spaces_in_json(self):
        s = '  [  "A"  ;  "B"  ]  '
        assert _parse_json_array_string(s) == ["A", "B"]


@pytest.mark.parametrize(
    ("s", "single_cell", "refused"),
    [
        ("Name,Age\nAnn,30", True, True),
        ("Alice\nBob", False, True),
        ("Name;Age;Salary", False, True),
        ("Hello, world", False, True),
        ("Hello, world", True, False),
        ('=PY("a = 1\nresult = a"; A1:B3)', True, False),
        ("=SUM(A1;B1)", False, False),
        (" =B2+1", False, False),
        ("Batch", False, False),
        ("a\tb", False, False),
        ("", False, False),
    ],
)
def test_delimited_text_message_refuses_ambiguous_text(s: str, single_cell: bool, refused: bool):
    msg = _delimited_text_message("A1", s, single_cell=single_cell)
    assert (msg is not None) is refused


@pytest.mark.parametrize(
    ("range_str", "values"),
    [
        ("A1:C1", "Name;Age;Salary"),
        ("E1", "Name,Age\nAlice,30"),
        ("A1:A3", "Alice\nBob\nCarol"),
    ],
)
def test_plan_write_refuses_delimited_text(range_str: str, values: str):
    bridge = MagicMock()
    cell_range = MagicMock()
    sheet = MagicMock()
    end_col = 2 if range_str == "A1:C1" else 0
    end_row = 2 if range_str == "A1:A3" else 0
    cell_range.getRangeAddress.return_value = SimpleNamespace(
        StartColumn=0, EndColumn=end_col, StartRow=0, EndRow=end_row, Sheet=0
    )
    cell_range.getSpreadsheet.return_value = sheet
    bridge.resolve_range_or_address.return_value = cell_range
    manipulator = CellManipulator(bridge)

    with pytest.raises(CalcError) as exc:
        manipulator.plan_write(range_str, values)
    assert exc.value.code == "INVALID_PARAM"
    sheet.getCellByPosition.assert_not_called()
    cell_range.setDataArray.assert_not_called()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,5", True),
        ("1.234,56", True),
        ("$1,200", True),
        ("12%", True),
        ("08/05/2026", True),
        ("5.8.2026", True),
        ("2026/08/05", True),
        ("2026-02-30", True),
        ("2026-08-08T10:00Z", True),
        ("8:30 PM", True),
        ("1.2.3", False),
        ("10.0.0.1", False),
        ("1,2,3", False),
        ("Q1", False),
        ("90s", False),
        ("TRUE", False),
        ("555-1234", False),
        ("v1.2", False),
        ("8/5", False),
    ],
)
def test_looks_numeric_text(text: str, expected: bool) -> None:
    assert looks_numeric_text(text) is expected


def test_write_report_flags_numeric_text_and_needless_apostrophe() -> None:
    vals = ["1,5", "'tis", "'2026-08-08", "''x", "08/05/2026", "hello", "=A1", "'"]
    metas = CellManipulator(MagicMock()).plan_writes(vals, None, 0)
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    found = MagicMock()
    found.getRangeAddresses.return_value = []
    sheet.getCellRangeByPosition.return_value.queryFormulaCells.return_value = found

    report = _write_report(sheet, (0, 0), (7, 0), vals, metas, scan_text=True)
    assert report["text_that_looks_numeric"] == [
        {"address": "Sheet1.A1", "value": "1,5"},
        {"address": "Sheet1.E1", "value": "08/05/2026"},
    ]
    assert report["apostrophe_removed"] == [
        {"address": "Sheet1.B1", "value": "tis"},
        {"address": "Sheet1.H1", "value": ""},
    ]
    assert {w["code"] for w in report["warnings"]} == {"TEXT_LOOKS_NUMERIC", "APOSTROPHE_REMOVED"}

    egress_report = _write_report(sheet, (0, 0), (7, 0), vals, metas, scan_text=False)
    assert "text_that_looks_numeric" not in egress_report
    assert "apostrophe_removed" not in egress_report


def test_write_report_error_cells_with_address_and_count() -> None:
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"

    err_cell = MagicMock()
    err_cell.getError.return_value = 509
    err_cell.getFormula.return_value = "=py header"
    err_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0)

    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = err_cell

    found = MagicMock()
    found.getRangeAddresses.return_value = [SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=11)]
    found.getCells.return_value.createEnumeration.return_value = enum
    sheet.getCellRangeByPosition.return_value.queryFormulaCells.return_value = found

    metas = CellManipulator(MagicMock()).plan_writes(["=PY header"] * 12, None, 0)
    res = _write_report(sheet, (0, 0), (0, 11), ["=PY header"] * 12, metas, scan_text=True)
    assert res["error_cells"] == [{"address": "Sheet1.A1", "error": "Err:509", "formula": "=py header"}]
    assert res["warnings"][0]["code"] == "FORMULA_ERROR"
    assert "12 formula cell(s)" in res["warnings"][0]["message"]


@pytest.mark.parametrize(
    ("formula", "expect_hint"),
    [
        ("=SUM(1,2)", True),
        ("=missing!A1", True),
        ("=SUMM(G1)", False),
    ],
)
def test_write_report_dialect_hint_only_when_formula_has_comma_or_bang(formula: str, expect_hint: bool) -> None:
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"

    err_cell = MagicMock()
    err_cell.getError.return_value = 525
    err_cell.getFormula.return_value = formula
    err_cell.getCellAddress.return_value = SimpleNamespace(Column=0, Row=0)

    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = err_cell

    found = MagicMock()
    found.getRangeAddresses.return_value = [SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0)]
    found.getCells.return_value.createEnumeration.return_value = enum
    sheet.getCellRangeByPosition.return_value.queryFormulaCells.return_value = found

    metas = CellManipulator(MagicMock()).plan_writes([formula], None, 0)
    res = _write_report(sheet, (0, 0), (0, 0), [formula], metas, scan_text=True)
    msg = res["warnings"][0]["message"]
    assert ("Formulas use Calc syntax:" in msg) is expect_hint


def test_commit_write_forwards_write_report() -> None:
    small_addr = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0, Sheet=0)
    mock_range = MagicMock()
    mock_sheet = MagicMock()
    mock_range.getSpreadsheet.return_value = mock_sheet
    mock_range.getDataArray.return_value = ()
    bridge = MagicMock()
    bridge.resolve_range_or_address.return_value = mock_range
    bridge.doc.getNumberFormats.return_value = MagicMock()
    bridge.doc.getSheets.return_value.getByIndex.return_value = mock_sheet

    fake_report = {
        "error_cells": [{"address": "Sheet1.A1", "error": "Err:509", "formula": "=py"}],
        "warnings": [{"code": "FORMULA_ERROR", "message": "1 formula cell(s)"}],
    }
    with (
        patch("plugin.calc.manipulator._uno_range_address", return_value=small_addr),
        patch("plugin.calc.manipulator.resolve_document_locale", return_value=MagicMock()),
        patch("plugin.calc.manipulator._write_report", return_value=fake_report) as mock_report,
    ):
        res = CellManipulator(bridge).write_formula_range("A1", "=py")

    mock_report.assert_called_once()
    assert res["error_cells"] == fake_report["error_cells"]
    assert res["warnings"] == fake_report["warnings"]
    assert "formula gives" not in res["message"]


def test_plan_write_refuses_py_inside_its_data_range():
    bridge = MagicMock()
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    sheet.getCellRangeByPosition.return_value.getFormulaArray.return_value = (("",), ("",))

    def _resolve(range_str: str):
        cell_range = MagicMock()
        cell_range.getSpreadsheet.return_value = sheet
        if range_str == "A1:A2":
            cell_range.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=1, Sheet=0)
        else:
            cell_range.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0, Sheet=0)
        return cell_range

    bridge.resolve_range_or_address.side_effect = _resolve
    manipulator = CellManipulator(bridge)

    formula = '=PY("result = np.sum(data)"; A1:A2)'
    with pytest.raises(CalcError) as exc:
        manipulator.plan_write("A1", formula)
    assert exc.value.code == "PY_CIRCULAR"
    assert exc.value.details == {
        "data_range": "A1:A2",
        "suggested_call": {
            "tool": "write_range",
            "arguments": {"range": ["Sheet1.B1"], "values": formula},
        },
    }

    # Different sheet data arg is not circular on Sheet1.
    plan_other = manipulator.plan_write("A1", '=PY("result = np.sum(data)"; Orders.A1:A2)')
    assert plan_other.kind == "cells"

    # Multi-cell write omits suggested_call.
    with pytest.raises(CalcError) as exc_multi:
        manipulator.plan_write("A1:A2", [formula, "=1"])
    assert exc_multi.value.code == "PY_CIRCULAR"
    assert "suggested_call" not in (exc_multi.value.details or {})

    # Whole-column and whole-row arguments also trigger PY_CIRCULAR.
    for open_arg in ("A:A", "1:3"):
        with pytest.raises(CalcError) as exc_open:
            manipulator.plan_write("A1", f'=PY("result = np.sum(data)"; {open_arg})')
        assert exc_open.value.code == "PY_CIRCULAR"
        assert exc_open.value.details == {"data_range": open_arg}

    # Long plain-text values containing "py" must not reach PyFormula.parse's 8192-char @deal contract.
    long_text = "happy " * 1500
    assert len(long_text) > 8192
    assert manipulator.plan_write("A1", long_text).kind == "cells"

    # Non-ASCII sheet names must still trigger PY_CIRCULAR.
    sheet.getName.return_value = "Données"
    unicode_formula = '=PY("result = np.sum(data)"; \'Données\'.A1:A2)'
    with pytest.raises(CalcError) as exc_uni:
        manipulator.plan_write("A1", unicode_formula)
    assert exc_uni.value.code == "PY_CIRCULAR"
    assert (exc_uni.value.details or {})["data_range"] == "'Données'.A1:A2"


def test_plan_write_refuses_py_array_inside_its_data_range():
    bridge = MagicMock()
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    sheet.getCellRangeByPosition.return_value.getFormulaArray.return_value = (("",), ("",))

    ranges = {
        "A1": SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0, Sheet=0),
        "F1:G2": SimpleNamespace(StartColumn=5, EndColumn=6, StartRow=0, EndRow=1, Sheet=0),
        "C1": SimpleNamespace(StartColumn=2, EndColumn=2, StartRow=0, EndRow=0, Sheet=0),
    }

    def _resolve(range_str: str):
        cell_range = MagicMock()
        cell_range.getSpreadsheet.return_value = sheet
        cell_range.getRangeAddress.return_value = ranges[range_str]
        return cell_range

    bridge.resolve_range_or_address.side_effect = _resolve
    manipulator = CellManipulator(bridge)

    f_a1 = '=PY("result = data"; A1:A2)'
    with patch("plugin.calc.manipulator.accepts_returned_any", return_value=True):
        with patch.object(manipulator, "_probe_array_formula") as probe:
            with pytest.raises(CalcError) as exc_a1:
                manipulator.plan_write("A1", f_a1, array=True)
            probe.assert_not_called()
    assert exc_a1.value.code == "PY_CIRCULAR"
    assert exc_a1.value.details == {
        "data_range": "A1:A2",
        "suggested_call": {
            "tool": "write_range",
            "arguments": {"range": ["Sheet1.B1"], "values": f_a1, "array": True},
        },
    }

    f_block = '=PY("result = data"; G2:G9)'
    with patch("plugin.calc.manipulator.accepts_returned_any", return_value=True):
        with patch.object(manipulator, "_probe_array_formula") as probe:
            with pytest.raises(CalcError) as exc_block:
                manipulator.plan_write("F1:G2", f_block, array=True)
            probe.assert_not_called()
    assert exc_block.value.code == "PY_CIRCULAR"
    assert "F1:G2" in str(exc_block.value)
    assert exc_block.value.details == {
        "data_range": "G2:G9",
        "suggested_call": {
            "tool": "write_range",
            "arguments": {"range": ["Sheet1.H2:I3"], "values": f_block, "array": True},
        },
    }

    f_grow = '=PY("result = data"; D1:D5)'
    with patch("plugin.calc.manipulator.accepts_returned_any", return_value=True):
        with patch.object(manipulator, "_probe_array_formula", return_value=(2, 2)):
            with pytest.raises(CalcError) as exc_grow:
                manipulator.plan_write("C1", f_grow, array=True, premeasured_array_size=(2, 2))
    assert exc_grow.value.code == "PY_CIRCULAR"
    assert exc_grow.value.details == {
        "data_range": "D1:D5",
        "suggested_call": {
            "tool": "write_range",
            "arguments": {"range": ["Sheet1.E1:F2"], "values": f_grow, "array": True},
        },
    }

    with patch("plugin.calc.manipulator.accepts_returned_any", return_value=False):
        with pytest.raises(CalcError) as exc_old:
            manipulator.plan_write("A1", f_a1, array=True)
    assert exc_old.value.code == "PY_CIRCULAR"
    assert "array" not in (exc_old.value.details or {})["suggested_call"]["arguments"]


def test_plan_write_py_suggestion_skips_column_occupied_below_top_row():
    bridge = MagicMock()
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"

    def _range(left: int, top: int, right: int, bottom: int):
        rng = MagicMock()
        rows = bottom - top + 1
        cols = right - left + 1
        if left <= 1 <= right and top <= 1 <= bottom:
            grid = tuple(
                tuple("x" if (top + r == 1 and left + c == 1) else "" for c in range(cols))
                for r in range(rows)
            )
        else:
            grid = tuple(("",) * cols for _ in range(rows))
        rng.getFormulaArray.return_value = grid
        return rng

    sheet.getCellRangeByPosition.side_effect = _range
    cell_range = MagicMock()
    cell_range.getSpreadsheet.return_value = sheet
    cell_range.getRangeAddress.return_value = SimpleNamespace(StartColumn=0, EndColumn=0, StartRow=0, EndRow=0, Sheet=0)
    bridge.resolve_range_or_address.return_value = cell_range

    manipulator = CellManipulator(bridge)
    formula = '=PY("result = data * 2"; A1:A5)'

    with pytest.raises(CalcError) as exc_ordinary:
        manipulator.plan_write("A1", formula)
    assert exc_ordinary.value.code == "PY_CIRCULAR"
    assert (exc_ordinary.value.details or {})["suggested_call"]["arguments"]["range"] == ["Sheet1.C1"]

    with patch("plugin.calc.manipulator.accepts_returned_any", return_value=True):
        with pytest.raises(CalcError) as exc_array:
            manipulator.plan_write("A1", formula, array=True)
    assert exc_array.value.code == "PY_CIRCULAR"
    assert (exc_array.value.details or {})["suggested_call"]["arguments"] == {
        "range": ["Sheet1.C1"],
        "values": formula,
        "array": True,
    }


def test_commit_quotes_the_py_run_error():
    from plugin.calc.python.diagnostics import get_diagnostics_store

    store = get_diagnostics_store()
    code = "result = sum(data)"
    formula = f'=PY("{code}"; A1:A2)'

    bridge = MagicMock()
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    cell = MagicMock()
    cell.Error = 519
    cell.getString.return_value = ""

    def _set_formula_err(_f):
        store.record(workbook_key="wb", code=code, status="error", message="TypeError: unsupported operand type")

    cell.setFormula.side_effect = _set_formula_err
    sheet.getCellByPosition.return_value = cell

    cell_range = MagicMock()
    cell_range.getSpreadsheet.return_value = sheet
    cell_range.getRangeAddress.return_value = SimpleNamespace(StartColumn=1, EndColumn=1, StartRow=0, EndRow=0, Sheet=0)
    bridge.resolve_range_or_address.return_value = cell_range

    manipulator = CellManipulator(bridge)
    plan = manipulator.plan_write("B1", formula)

    with patch("plugin.calc.python.diagnostics.existing_calc_session_id", return_value="wb"):
        with patch("plugin.calc.manipulator.resolve_document_locale", return_value=MagicMock()):
            res = manipulator.commit_write(plan)
    assert "Err:519" in res["message"]
    assert "TypeError: unsupported operand type" in res["message"]

    # An entry recorded before commit is not quoted (even for the same code),
    # whereas a new entry recorded during setFormula is quoted.
    stale_code = "result = 1 / 0"
    stale_formula = f'=PY("{stale_code}"; A1:A2)'
    store.record(workbook_key="wb", code=stale_code, status="error", message="ZeroDivisionError: old")
    cell.setFormula.side_effect = None
    plan_stale = manipulator.plan_write("B1", stale_formula)
    with patch("plugin.calc.python.diagnostics.existing_calc_session_id", return_value="wb"):
        with patch("plugin.calc.manipulator.resolve_document_locale", return_value=MagicMock()):
            res_stale = manipulator.commit_write(plan_stale)
    assert "ZeroDivisionError: old" not in res_stale["message"]

    def _set_formula_new_for_same_code(_f):
        store.record(workbook_key="wb", code=stale_code, status="error", message="ZeroDivisionError: fresh")

    cell.setFormula.side_effect = _set_formula_new_for_same_code
    with patch("plugin.calc.python.diagnostics.existing_calc_session_id", return_value="wb"):
        with patch("plugin.calc.manipulator.resolve_document_locale", return_value=MagicMock()):
            res_fresh = manipulator.commit_write(plan_stale)
    assert "ZeroDivisionError: fresh" in res_fresh["message"]

    # Error=0 with #SPILL! quotes the spill range from diagnostics.
    spill_code = "result = [[1, 2]]"
    spill_formula = f'=PY("{spill_code}"; A1:A2)'
    cell.Error = 0
    cell.getString.return_value = "#SPILL!"

    def _set_formula_spill(_f):
        store.record(
            workbook_key="wb",
            code=spill_code,
            status="error",
            message="#SPILL!: the 1 x 2 result needs Sheet1.B1:C1, but C1 is not empty.",
        )

    cell.setFormula.side_effect = _set_formula_spill
    plan_spill = manipulator.plan_write("B1", spill_formula)
    with patch("plugin.calc.python.diagnostics.existing_calc_session_id", return_value="wb"):
        with patch("plugin.calc.manipulator.resolve_document_locale", return_value=MagicMock()):
            res_spill = manipulator.commit_write(plan_spill)
    assert "B1 shows #SPILL!" in res_spill["message"]
    assert "Sheet1.B1:C1" in res_spill["message"]

    # Error 0 with setup failure text emits a warning quoting the cell text.
    cell.Error = 0
    cell.setFormula.side_effect = None
    cell.getString.return_value = "Error: Python venv not found. Open Settings → Python, set the venv path, then Test."
    plan_text = manipulator.plan_write("B1", '=PY("result = 1")')
    with patch("plugin.calc.manipulator.resolve_document_locale", return_value=MagicMock()):
        res_text = manipulator.commit_write(plan_text)
    assert "shows an error message instead of a result" in res_text["message"]
    assert "Python venv not found" in res_text["message"]


def test_number_format_key_malformed_code_is_invalid_param():
    from com.sun.star.util import MalformedNumberFormatException

    doc = MagicMock()
    formats = doc.getNumberFormats.return_value
    formats.addNewConverted.side_effect = MalformedNumberFormatException("bad format", None, 0)
    manipulator = CellManipulator(SimpleNamespace(doc=doc))
    with patch("plugin.calc.manipulator.resolve_document_locale", return_value=MagicMock()):
        with pytest.raises(CalcError) as exc_info:
            manipulator.number_format_key("bogus[[[")
    assert exc_info.value.code == "INVALID_PARAM"


def test_number_format_key_general_preset_skips_conversion():
    doc = MagicMock()
    formats = doc.getNumberFormats.return_value
    formats.getStandardFormat.return_value = 0
    locale = MagicMock()
    manipulator = CellManipulator(SimpleNamespace(doc=doc))
    with patch("plugin.calc.manipulator.resolve_document_locale", return_value=locale):
        key = manipulator.number_format_key("General")
    assert key == 0
    formats.getStandardFormat.assert_called_once_with(16, locale)
    formats.addNewConverted.assert_not_called()


def test_surviving_ref_cells_rows_and_columns():
    from plugin.calc.manipulator import _surviving_ref_cells

    cells = {
        ("Sheet1", 0, 1),   # above deleted rows 3..4 (0-based 2..3)
        ("Sheet1", 1, 2),   # inside deleted rows
        ("Sheet1", 2, 3),   # inside deleted rows
        ("Sheet1", 0, 5),   # below deleted rows -> row 3
        ("Other", 1, 2),    # other sheet: untouched
    }
    assert _surviving_ref_cells(cells, "Sheet1", True, 2, 2) == {
        ("Sheet1", 0, 1),
        ("Sheet1", 0, 3),
        ("Other", 1, 2),
    }

    col_cells = {
        ("Sheet1", 0, 0),   # left of deleted cols B..C (0-based 1..2)
        ("Sheet1", 1, 4),   # inside deleted cols
        ("Sheet1", 2, 5),   # inside deleted cols
        ("Sheet1", 4, 2),   # right of deleted cols -> col 2
        ("Other", 1, 4),    # other sheet: untouched
    }
    assert _surviving_ref_cells(col_cells, "Sheet1", False, 1, 2) == {
        ("Sheet1", 0, 0),
        ("Sheet1", 2, 2),
        ("Other", 1, 4),
    }


def test_delete_structure_confirm_without_undo_does_not_promise_ctrl_z():
    doc = MagicMock()
    doc.getUndoManager.return_value = None
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    sheets = doc.getSheets.return_value
    sheets.getCount.return_value = 1
    sheets.getByIndex.return_value = sheet

    empty_ranges = MagicMock()
    empty_ranges.getRangeAddresses.return_value = []
    ra = SimpleNamespace(StartColumn=0, StartRow=0, EndColumn=0, EndRow=0)
    after_ranges = MagicMock()
    after_ranges.getRangeAddresses.return_value = [ra]
    sheet.queryContentCells.side_effect = [empty_ranges, after_ranges]
    sheet.getCellRangeByPosition.return_value.getFormulaArray.return_value = (("=#REF!",),)

    bridge = SimpleNamespace(doc=doc, get_active_sheet=lambda: sheet)
    manipulator = CellManipulator(bridge)
    res = manipulator.delete_structure("rows", 5, confirm=True)

    assert res["refs_broken"] == 1
    assert len(res["warnings"]) == 1
    msg = res["warnings"][0]["message"]
    assert "Ctrl+Z" not in msg
    assert "Undo was unavailable" in msg


def test_ref_named_ranges_scans_global_and_visible_sheet_scopes():
    class _FakeNamedRanges:
        def __init__(self, entries: dict[str, str]) -> None:
            self._entries = entries

        def getElementNames(self) -> tuple[str, ...]:
            return tuple(self._entries.keys())

        def getByName(self, name: str) -> SimpleNamespace:
            content = self._entries[name]
            return SimpleNamespace(getContent=lambda: content)

    def _sheet(name: str, entries: dict[str, str]) -> SimpleNamespace:
        return SimpleNamespace(getName=lambda: name, NamedRanges=_FakeNamedRanges(entries))

    sheets_list = [
        _sheet("Sheet1", {"L_BAD": "$Sheet1.$A$#REF!*2"}),
        _sheet("_Hidden", {"H_BAD": "#REF!"}),
    ]
    sheets = SimpleNamespace(
        getCount=lambda: len(sheets_list),
        getByIndex=lambda idx: sheets_list[idx],
    )
    doc = SimpleNamespace(
        NamedRanges=_FakeNamedRanges({"G_OK": "$Sheet1.$A$1", "G_BAD": "$Sheet1.$A$#REF!"}),
        getSheets=lambda: sheets,
    )

    assert _ref_named_ranges(doc) == {("global", "G_BAD"), ("Sheet1", "L_BAD")}


class _FakeDialects:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    def to_api(self, text: str, _pos=None) -> str:
        self.calls.append(text)
        return self._mapping.get(text, text)


def test_plan_write_hands_formulas_to_calc_grammar() -> None:
    sheet = MagicMock()
    cell_range = MagicMock()
    cell_range.getSpreadsheet.return_value = sheet
    cell_range.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=0, EndRow=0)

    bridge = MagicMock(doc=MagicMock())
    bridge.resolve_range_or_address.return_value = cell_range
    fake = _FakeDialects({"=Orders!A1*2": "=$Orders.A1*2", "=SUM(A1,B1)": "=SUM(A1;B1)"})

    with patch("plugin.calc.manipulator.FormulaDialects", return_value=fake):
        manipulator = CellManipulator(bridge)
        plan = manipulator.plan_write("A1", "=Orders!A1*2")
        assert plan.values == ["=$Orders.A1*2"]
        assert plan.notes == ()

        cell_range.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=1, EndRow=1)
        list_plan = manipulator.plan_write("A1:B2", [["=SUM(A1,B1)", "plain, text"], ["=Orders!A1*2", 42]])
        assert list_plan.values == ["=SUM(A1;B1)", "plain, text", "=$Orders.A1*2", 42]
        assert list_plan.notes == ()

        cell_range.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=0, EndRow=0)
        data_txt = manipulator.plan_write("A1", "=Orders!A1*2", data_text=True)
        assert data_txt.values == ["=Orders!A1*2"]
        assert data_txt.notes == ()

        lit_txt = manipulator.plan_write("A1", "=SUM(A1,B1)", literal_text=True)
        assert lit_txt.values == ["=SUM(A1,B1)"]
        assert lit_txt.notes == ()

    assert fake.calls == ["=Orders!A1*2", "=SUM(A1,B1)", "=Orders!A1*2"]


def test_plan_write_warns_on_repeated_row_formula_without_rewriting() -> None:
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    cell_range = MagicMock()
    cell_range.getSpreadsheet.return_value = sheet
    cell_range.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=2, StartRow=1, EndColumn=2, EndRow=3)

    bridge = MagicMock(doc=MagicMock())
    bridge.resolve_range_or_address.return_value = cell_range
    manipulator = CellManipulator(bridge)

    plan = manipulator.plan_write("C2:C4", [["=B2*Rates.B2"]] * 3)
    assert plan.values == ["=B2*Rates.B2", "=B2*Rates.B2", "=B2*Rates.B2"]
    assert len(plan.notes) == 1
    note = plan.notes[0]
    assert note["code"] == "REPEATED_FORMULA"
    assert "Column C" in note["message"]
    assert note["suggested_call"] == {
        "tool": "write_range",
        "arguments": {"range": ["Sheet1.C2:C4"], "values": "=B2*Rates.B2"},
    }

    pinned = manipulator.plan_write("C2:C4", [["=B1"]] * 3)
    assert pinned.values == ["=B1", "=B1", "=B1"]
    assert pinned.notes == ()

    cell_range.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=2, StartRow=1, EndColumn=2, EndRow=1)
    expanded = manipulator.plan_write("C2", [["=B2*2"]] * 3)
    assert expanded.range_str == "C2:C4"
    assert expanded.values == ["=B2*2", "=B2*2", "=B2*2"]
    assert expanded.notes[0]["code"] == "REPEATED_FORMULA"
    assert expanded.notes[0]["suggested_call"] == {
        "tool": "write_range",
        "arguments": {"range": ["Sheet1.C2:C4"], "values": "=B2*2"},
    }


def test_array_error_quotes_fresh_py_diagnostic() -> None:
    from plugin.calc.python.diagnostics import get_diagnostics_store

    store = get_diagnostics_store()
    code = "result = 1 / 0  # array_diag_test"
    formula = f'=PY("{code}"; A1:A2)'

    # 1) _measure_array: stale diagnostic before write is ignored; fresh one during write is quoted.
    store.record(workbook_key="wb_arr", code=code, status="error", message="ZeroDivisionError: old")

    def _make_sheet(record_fresh: bool):
        written: dict[tuple[int, int], str] = {}

        class _Cursor:
            def gotoEndOfUsedArea(self, _expand):
                return None

            def getRangeAddress(self):
                return _used(0, 0)

        class _Probe:
            def __init__(self, col: int, row: int) -> None:
                self.col = col
                self.row = row

            def setArrayFormula(self, f: str) -> None:
                written[(self.col, self.row)] = f
                if record_fresh and f.startswith("=("):
                    store.record(workbook_key="wb_arr", code=code, status="error", message="ZeroDivisionError: fresh")

            def clearContents(self, _flags: int) -> None:
                return None

        class _Cell:
            Error = 519

            def getString(self) -> str:
                return "Err:519"

        class _Sheet:
            def createCursor(self) -> _Cursor:
                return _Cursor()

            def getCellRangeByPosition(self, col: int, row: int, _c2: int, _r2: int) -> _Probe:
                return _Probe(col, row)

            def getCellByPosition(self, _col: int, _row: int) -> _Cell:
                return _Cell()

        return _Sheet()

    manipulator = CellManipulator(MagicMock())
    with patch("plugin.calc.python.diagnostics.existing_calc_session_id", return_value="wb_arr"):
        with pytest.raises(CalcError) as exc_stale:
            manipulator._measure_array(_make_sheet(False), formula, CellRect(0, 0, 0, 0))
        assert "ZeroDivisionError: old" not in str(exc_stale.value)

        with pytest.raises(CalcError) as exc_fresh:
            manipulator._measure_array(_make_sheet(True), formula, CellRect(0, 0, 0, 0))
        assert "ZeroDivisionError: fresh" in str(exc_fresh.value)

    # 2) _write_array_formula: fresh diagnostic recorded during setArrayFormula is quoted.
    anchor = MagicMock(Error=519)
    anchor.getString.return_value = "Err:519"
    rng = MagicMock()
    rng.getArrayFormula.return_value = ""
    rng.setArrayFormula.side_effect = lambda f: (
        store.record(workbook_key="wb_arr", code=code, status="error", message="ValueError: fresh write")
        if f
        else None
    )
    sheet_w = MagicMock()
    sheet_w.getCellRangeByPosition.return_value = rng
    sheet_w.getCellByPosition.return_value = anchor
    with patch("plugin.calc.manipulator.refuse_occupied_cells"):
        with patch("plugin.calc.python.diagnostics.existing_calc_session_id", return_value="wb_arr"):
            with pytest.raises(CalcError) as exc_w:
                manipulator._write_array_formula(sheet_w, formula, (0, 0), (0, 0), premeasured_array_size=(1, 1))
    assert "ValueError: fresh write" in str(exc_w.value)


def test_array_525_drops_unknown_function_hint_for_whole_py() -> None:
    sheet, _cleared = _make_measure_probe_sheet(525)
    with pytest.raises(CalcError) as exc_info:
        CellManipulator(MagicMock())._measure_array(sheet, '=PY("result = \'#NAME?\'")', CellRect(0, 0, 0, 0))
    msg = str(exc_info.value)
    assert "code 525" in msg
    assert "unknown function" not in msg


@pytest.mark.parametrize("keeps_block", [True, False])
@pytest.mark.parametrize(
    ("formula", "array"),
    [
        ('=TRANSPOSE(PY("result = data"; B1:B3))', None),
        ('=IFERROR(PY("result = 1");7)', True),
        ('=PY("a";PY("b"))', True),
    ],
)
def test_nested_py_array_refused_during_planning(keeps_block: bool, formula: str, array: bool | None) -> None:
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    cell_range = MagicMock()
    cell_range.getSpreadsheet.return_value = sheet
    cell_range.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=0, EndRow=0)

    bridge = MagicMock(doc=MagicMock())
    bridge.resolve_range_or_address.return_value = cell_range
    manipulator = CellManipulator(bridge)

    with (
        patch("plugin.calc.manipulator.accepts_returned_any", return_value=keeps_block),
        patch.object(manipulator, "_probe_array_formula") as mock_probe,
    ):
        with pytest.raises(CalcError, match=r"exactly one =PY") as exc:
            manipulator.plan_write("A1", formula, array=array)
        assert exc.value.code == "INVALID_PARAM"
        mock_probe.assert_not_called()


def test_ordinary_nested_py_still_plans_as_cells() -> None:
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    cell_range = MagicMock()
    cell_range.getSpreadsheet.return_value = sheet
    cell_range.getRangeAddress.return_value = SimpleNamespace(Sheet=0, StartColumn=0, StartRow=0, EndColumn=0, EndRow=0)

    bridge = MagicMock(doc=MagicMock())
    bridge.resolve_range_or_address.return_value = cell_range
    manipulator = CellManipulator(bridge)

    assert manipulator.plan_write("A1", '=SUM(PY("result = data"; A1:A3))', array=False).kind == "cells"
    assert manipulator.plan_write("A1", '=TRANSPOSE(PY("result = data"; B1:B3))', array=False).kind == "cells"


def test_free_block_right_of_skips_wide_tall_data_and_keeps_one_column() -> None:
    from plugin.calc.manipulator import _refuse_circular_py
    from plugin.calc.python.cell_discovery import py_formula_or_none

    sheet = MagicMock()
    sheet.getCellRangeByPosition.return_value.getFormulaArray.return_value = (("",),)
    formula = '=PY("result = data"; A1:A200000)'
    parts = py_formula_or_none(formula)
    assert parts is not None

    # 2-column target over tall data: no occupancy read and no suggested_call
    with pytest.raises(CalcError) as exc_wide:
        _refuse_circular_py(sheet, "Sheet1", formula, parts, (0, 0), (1, 0), suggest=True, array=True)
    assert exc_wide.value.code == "PY_CIRCULAR"
    assert "suggested_call" not in (exc_wide.value.details or {})
    sheet.getCellRangeByPosition.assert_not_called()

    # 1-column target over tall data: still suggests B1
    with pytest.raises(CalcError) as exc_narrow:
        _refuse_circular_py(sheet, "Sheet1", formula, parts, (0, 0), (0, 0), suggest=True, array=True)
    assert exc_narrow.value.code == "PY_CIRCULAR"
    assert (exc_narrow.value.details or {})["suggested_call"]["arguments"]["range"] == ["Sheet1.B1"]
