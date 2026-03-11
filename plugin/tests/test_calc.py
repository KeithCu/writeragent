# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from plugin.framework.uno_helpers import get_desktop
from plugin.testing_runner import setup, teardown, test


_test_doc = None
_test_ctx = None


@setup
def setup_calc_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx

    desktop = get_desktop(ctx)
    from com.sun.star.beans import PropertyValue
    hidden_prop = PropertyValue()
    hidden_prop.Name = "Hidden"
    hidden_prop.Value = True

    _test_doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, (hidden_prop,))
    assert _test_doc is not None, "Could not create hidden test calc document"


@teardown
def teardown_calc_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


@test
def test_address_utils():
    from plugin.modules.calc.address_utils import (
        column_to_index, index_to_column, parse_address,
        parse_range_string, format_address
    )

    assert column_to_index("A") == 0
    assert column_to_index("AA") == 26
    assert index_to_column(0) == "A"
    assert index_to_column(26) == "AA"
    assert parse_address("A1") == (0, 0)
    assert parse_address("B10") == (1, 9)
    assert format_address(0, 0) == "A1"

    # Round-trip
    for addr in ("A1", "B10", "Z1", "AA100"):
        col, row = parse_address(addr)
        assert format_address(col, row) == addr

    try:
        parse_address("Invalid")
        assert False, "Expected ValueError for 'Invalid'"
    except ValueError:
        pass

    assert parse_range_string("A1:B2") == ((0, 0), (1, 1))
    assert parse_range_string("C3") == ((2, 2), (2, 2))

    try:
        parse_range_string("A1:Z")
        assert False, "Expected ValueError for 'A1:Z'"
    except ValueError:
        pass


@test
def test_error_detector_data():
    from plugin.modules.calc.error_detector import ERROR_TYPES, ERROR_PATTERNS
    assert 502 in ERROR_TYPES
    assert len(ERROR_PATTERNS) > 0
    for code, info in ERROR_TYPES.items():
        assert "name" in info
        assert "description" in info


@test
def test_cells_parse_color():
    from plugin.modules.calc.cells import _parse_color
    assert _parse_color("red") == 0xFF0000
    assert _parse_color("RED") == 0xFF0000
    assert _parse_color("#00FF00") == 0x00FF00
    assert _parse_color("#000") == 0x000000
    assert _parse_color("invalid") is None


def _execute_calc_tool(name, args):
    from plugin.main import get_tools
    from plugin.framework.tool_context import ToolContext
    tctx = ToolContext(_test_doc, None, "calc", {}, "test")
    try:
        res = get_tools().execute(name, tctx, **args)
    except (KeyError, ValueError) as e:
        res = {"status": "error", "error": str(e)}
    return res


@test
def test_write_formula_range():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    res = _execute_calc_tool("write_formula_range", {"range_name": "A1", "formula_or_values": "Hello"})
    assert res.get("status") == "ok", f"write_formula_range failed: {res}"
    assert active_sheet.getCellByPosition(0, 0).getString() == "Hello", "Value mismatch"

    # Batch write
    _execute_calc_tool("write_formula_range", {"range_name": ["B1", "B2"], "formula_or_values": "Batch"})
    assert active_sheet.getCellByPosition(1, 0).getString() == "Batch", "Batch write cell 1 failed"
    assert active_sheet.getCellByPosition(1, 1).getString() == "Batch", "Batch write cell 2 failed"


@test
def test_set_cell_style_and_details():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    _execute_calc_tool("set_cell_style", {"range_name": "A1", "bold": True, "bg_color": "yellow"})
    cell = active_sheet.getCellByPosition(0, 0)
    from com.sun.star.awt.FontWeight import BOLD
    assert cell.getPropertyValue("CharWeight") == BOLD, "Bold not set"
    assert cell.getPropertyValue("CellBackColor") == 0xFFFF00, "Background color not set"

    from plugin.modules.calc.bridge import CalcBridge
    from plugin.modules.calc.inspector import CellInspector
    b = CalcBridge(_test_doc)
    insp = CellInspector(b)
    details = insp.get_cell_details("A1")

    assert details.get("background_color") == 0xFFFF00, f"Details readback bg color failed: {details}"
    assert details.get("bold") == BOLD, f"Details readback bold failed: {details}"


@test
def test_merge_cells():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    _execute_calc_tool("merge_cells", {"range_name": ["C1:D1", "E1:F1"]})
    rng1 = active_sheet.getCellRangeByPosition(2, 0, 3, 0)
    rng2 = active_sheet.getCellRangeByPosition(4, 0, 5, 0)
    assert rng1.getIsMerged(), "C1:D1 not merged"
    assert rng2.getIsMerged(), "E1:F1 not merged"


@test
def test_clear_range():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    active_sheet.getCellByPosition(6, 0).setString("ClearMe")
    active_sheet.getCellByPosition(7, 0).setString("ClearMe")
    _execute_calc_tool("clear_range", {"range_name": ["G1", "H1"]})
    assert active_sheet.getCellByPosition(6, 0).getString() == "", "G1 not cleared"
    assert active_sheet.getCellByPosition(7, 0).getString() == "", "H1 not cleared"


@test
def test_unknown_tool():
    res = _execute_calc_tool("bad_tool", {})
    assert res.get("status") == "error", f"unknown tool handling failed: {res}"


@test
def test_formulas_error_detector():
    from plugin.modules.calc.formulas import DetectErrors
    from plugin.framework.tool_context import ToolContext

    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    active_sheet.getCellByPosition(8, 0).setFormula("=1/0")
    tctx = ToolContext(_test_doc, None, "calc", {}, "test")
    res = DetectErrors().execute(tctx, range_name="I1")

    assert res.get("status") == "ok", f"detect_and_explain_errors failed: {res}"
    assert res.get("result", {}).get("error_count", 0) > 0, "No errors detected"
    errors = res.get("result", {}).get("errors", [])
    err0 = errors[0].get("error", {}) if errors else {}
    assert err0.get("code") == "#DIV/0!", f"Expected #DIV/0!, got: {errors}"

    active_sheet.getCellByPosition(9, 0).setFormula("=UNKNOWN_NAME()")
    res2 = DetectErrors().execute(tctx, range_name="J1")
    assert res2.get("status") == "ok", f"detect_and_explain_errors #NAME? failed: {res2}"
    assert res2.get("result", {}).get("error_count", 0) > 0, "No errors detected"
    errors = res2.get("result", {}).get("errors", [])
    err0 = errors[0].get("error", {}) if errors else {}
    assert err0.get("code") == "#NAME?", f"Expected #NAME?, got: {errors}"


@test
def test_analyzer_get_sheet_summary():
    from plugin.modules.calc.bridge import CalcBridge
    from plugin.modules.calc.analyzer import SheetAnalyzer

    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    bridge = CalcBridge(_test_doc)
    analyzer = SheetAnalyzer(bridge)
    active_sheet.getCellByPosition(0, 5).setString("TestEnd")
    summary = analyzer.get_sheet_summary()

    assert summary.get("sheet_name") == active_sheet.getName(), "Sheet name mismatch"
    assert summary.get("row_count") >= 6, f"Row count mismatch: {summary}"


@test
def test_create_sheet():
    res = _execute_calc_tool("create_sheet", {"sheet_name": "NewSheet"})
    assert res.get("status") == "ok", f"create_sheet failed: {res}"
    assert _test_doc.getSheets().hasByName("NewSheet"), "Sheet not created"


@test
def test_add_row_and_column():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    _execute_calc_tool("add_row", {"sheet_name": active_sheet.getName(), "row_index": 1, "count": 1})
    _execute_calc_tool("add_column", {"sheet_name": active_sheet.getName(), "col_index": 1, "count": 1})
    # we just test it didn't crash for now


@test
def test_calc_integration_tests():
    pass
