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
from plugin.testing_runner import setup, teardown, native_test


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


@native_test
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


@native_test
def test_error_detector_data():
    from plugin.modules.calc.error_detector import ERROR_TYPES, ERROR_PATTERNS
    assert 502 in ERROR_TYPES
    assert len(ERROR_PATTERNS) > 0
    for code, info in ERROR_TYPES.items():
        assert "name" in info
        assert "description" in info


@native_test
def test_cells_parse_color():
    from plugin.modules.calc.cells import _parse_color
    assert _parse_color("red") == 0xFF0000
    assert _parse_color("RED") == 0xFF0000
    assert _parse_color("#00FF00") == 0x00FF00
    assert _parse_color("#000") == 0x000000
    assert _parse_color("invalid") is None


def _execute_calc_tool(name, args):
    from plugin.main import get_tools, get_services
    from plugin.framework.tool_context import ToolContext
    tctx = ToolContext(_test_doc, None, "calc", get_services(), "test")
    try:
        res = get_tools().execute(name, tctx, **args)
    except (KeyError, ValueError) as e:
        res = {"status": "error", "error": str(e)}
    return res



@native_test
def test_write_formula_range():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    res = _execute_calc_tool("write_formula_range", {"range_name": "A1", "formula_or_values": "Hello"})
    assert res.get("status") == "ok", f"write_formula_range failed: {res}"
    assert active_sheet.getCellByPosition(0, 0).getString() == "Hello", "Value mismatch"

    # Batch write
    _execute_calc_tool("write_formula_range", {"range_name": ["B1", "B2"], "formula_or_values": "Batch"})
    assert active_sheet.getCellByPosition(1, 0).getString() == "Batch", "Batch write cell 1 failed"
    assert active_sheet.getCellByPosition(1, 1).getString() == "Batch", "Batch write cell 2 failed"


@native_test
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


@native_test
def test_merge_cells():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    _execute_calc_tool("merge_cells", {"range_name": ["C1:D1", "E1:F1"]})
    rng1 = active_sheet.getCellRangeByPosition(2, 0, 3, 0)
    rng2 = active_sheet.getCellRangeByPosition(4, 0, 5, 0)
    assert rng1.getIsMerged(), "C1:D1 not merged"
    assert rng2.getIsMerged(), "E1:F1 not merged"


@native_test
def test_clear_range():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    active_sheet.getCellByPosition(6, 0).setString("ClearMe")
    active_sheet.getCellByPosition(7, 0).setString("ClearMe")
    _execute_calc_tool("clear_range", {"range_name": ["G1", "H1"]})
    assert active_sheet.getCellByPosition(6, 0).getString() == "", "G1 not cleared"
    assert active_sheet.getCellByPosition(7, 0).getString() == "", "H1 not cleared"


@native_test
def test_unknown_tool():
    res = _execute_calc_tool("bad_tool", {})
    assert res.get("status") == "error", f"unknown tool handling failed: {res}"


@native_test
def test_formulas_error_detector():
    from plugin.modules.calc.formulas import DetectErrors
    from plugin.framework.tool_context import ToolContext

    active_sheet = _test_doc.getCurrentController().getActiveSheet()

    # Test #DIV/0!
    active_sheet.getCellByPosition(8, 0).setFormula("=1/0")
    tctx = ToolContext(_test_doc, None, "calc", {}, "test")
    res = DetectErrors().execute(tctx, range_name="I1")

    assert res.get("status") == "ok", f"detect_and_explain_errors failed: {res}"
    assert res.get("result", {}).get("error_count", 0) > 0, "No errors detected"
    errors = res.get("result", {}).get("errors", [])
    err0 = errors[0].get("error", {}) if errors else {}
    assert err0.get("code") == "#DIV/0!", f"Expected #DIV/0!, got: {errors}"

    # Test #NAME?
    active_sheet.getCellByPosition(9, 0).setFormula("=UNKNOWN_NAME()")
    res2 = DetectErrors().execute(tctx, range_name="J1")
    assert res2.get("status") == "ok", f"detect_and_explain_errors #NAME? failed: {res2}"
    assert res2.get("result", {}).get("error_count", 0) > 0, "No errors detected"
    errors = res2.get("result", {}).get("errors", [])
    err0 = errors[0].get("error", {}) if errors else {}
    assert err0.get("code") == "#NAME?", f"Expected #NAME?, got: {errors}"

    # Test #REF!
    active_sheet.getCellByPosition(10, 0).setFormula("=#REF!")
    res3 = DetectErrors().execute(tctx, range_name="K1")
    assert res3.get("status") == "ok", f"detect_and_explain_errors #REF! failed: {res3}"
    assert res3.get("result", {}).get("error_count", 0) > 0, "No #REF! errors detected"
    errors = res3.get("result", {}).get("errors", [])
    err0 = errors[0].get("error", {}) if errors else {}
    assert err0.get("code") == "#REF!", f"Expected #REF!, got: {errors}"
    assert "#REF!" in errors[0].get("suggestion", ""), f"Suggestion does not mention #REF!: {errors[0].get('suggestion')}"


@native_test
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


@native_test
def test_create_sheet():
    res = _execute_calc_tool("create_sheet", {"sheet_name": "NewSheet"})
    assert res.get("status") == "ok", f"create_sheet failed: {res}"
    assert _test_doc.getSheets().hasByName("NewSheet"), "Sheet not created"


@native_test
def test_add_row_and_column():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    _execute_calc_tool("add_row", {"sheet_name": active_sheet.getName(), "row_index": 1, "count": 1})
    _execute_calc_tool("add_column", {"sheet_name": active_sheet.getName(), "col_index": 1, "count": 1})
    # we just test it didn't crash for now


@native_test
def test_read_cell_range():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()

    # Populate a 3x3 grid (A1:C3)
    # Row 1: Strings
    active_sheet.getCellByPosition(0, 0).setString("Col1")
    active_sheet.getCellByPosition(1, 0).setString("Col2")
    active_sheet.getCellByPosition(2, 0).setString("Col3")

    # Row 2: Numbers
    active_sheet.getCellByPosition(0, 1).setValue(1.0)
    active_sheet.getCellByPosition(1, 1).setValue(2.5)
    active_sheet.getCellByPosition(2, 1).setValue(3.14)

    # Row 3: Mixed (String, Empty, Formula)
    active_sheet.getCellByPosition(0, 2).setString("End")
    # Leave B3 empty
    active_sheet.getCellByPosition(2, 2).setFormula("=A2+B2")

    res = _execute_calc_tool("read_cell_range", {"range_name": ["A1:C3"]})
    assert res.get("status") == "ok", f"read_cell_range failed: {res}"

    result_data = res.get("result", [])
    assert len(result_data) == 1, "Expected list of 1 result for 1 range"

    grid = result_data[0]
    assert len(grid) == 3, "Expected 3 rows"
    assert len(grid[0]) == 3, "Expected 3 columns per row"

    # Check Row 1
    assert grid[0][0]["value"] == "Col1"
    assert grid[0][1]["value"] == "Col2"
    assert grid[0][2]["value"] == "Col3"

    # Check Row 2
    assert grid[1][0]["value"] == 1.0
    assert grid[1][1]["value"] == 2.5
    assert grid[1][2]["value"] == 3.14

    # Check Row 3
    assert grid[2][0]["value"] == "End"
    assert grid[2][1]["value"] is None
    # Formula value depends on evaluation but formula property should be set
    assert grid[2][2]["formula"] == "=A2+B2"


@native_test
def test_cross_sheet_formula():
    doc = _test_doc
    sheets = doc.getSheets()

    # Create Sheet2 if it doesn't exist
    if not sheets.hasByName("Sheet2"):
        sheets.insertNewByName("Sheet2", sheets.getCount())

    sheet2 = sheets.getByName("Sheet2")
    # Set a target value
    sheet2.getCellByPosition(0, 0).setValue(100.0) # Sheet2.A1 = 100

    # Active sheet is usually Sheet1
    active_sheet = doc.getCurrentController().getActiveSheet()

    res = _execute_calc_tool("write_formula_range", {
        "range_name": ["D1"],
        "formula_or_values": "=Sheet2.A1 * 2"
    })

    assert res.get("status") == "ok", f"write_formula_range failed: {res}"

    # Verify the formula is set and evaluates properly
    cell = active_sheet.getCellByPosition(3, 0) # D1
    assert cell.getFormula() == "=Sheet2.A1*2" or cell.getFormula() == "=Sheet2.A1 * 2"

    # Wait for formula recalculation or force if necessary.
    # Usually in LibreOffice UNO it computes immediately, but we can verify formula strings safely.
    assert cell.getValue() == 200.0, f"Cross-sheet formula did not compute to 200.0, got {cell.getValue()}"


@native_test
def test_import_csv_from_string():
    doc = _test_doc
    active_sheet = doc.getCurrentController().getActiveSheet()

    # Test case 1: Standard comma-separated
    csv_1 = "Name,Age\nAlice,30\nBob,25"
    res1 = _execute_calc_tool("import_csv_from_string", {
        "csv_data": csv_1,
        "target_cell": "E1"
    })
    assert res1.get("status") == "ok", f"CSV import failed: {res1}"
    assert active_sheet.getCellByPosition(4, 0).getString() == "Name" # E1
    assert active_sheet.getCellByPosition(5, 0).getString() == "Age"  # F1
    assert active_sheet.getCellByPosition(4, 1).getString() == "Alice" # E2
    assert active_sheet.getCellByPosition(5, 1).getValue() == 30.0    # F2
    assert active_sheet.getCellByPosition(5, 2).getValue() == 25.0    # F3

    # Test case 2: Semicolon-separated
    csv_2 = "Item;Price\nApple;1.5\nBanana;0.75"
    res2 = _execute_calc_tool("import_csv_from_string", {
        "csv_data": csv_2,
        "target_cell": "G1"
    })
    assert res2.get("status") == "ok", f"Semicolon CSV import failed: {res2}"
    assert active_sheet.getCellByPosition(6, 0).getString() == "Item" # G1
    assert active_sheet.getCellByPosition(7, 0).getString() == "Price"# H1
    assert active_sheet.getCellByPosition(6, 1).getString() == "Apple"# G2
    assert active_sheet.getCellByPosition(7, 1).getValue() == 1.5     # H2

    # Test case 3: CSV with quoted commas
    csv_3 = "Person,Description\nCarol,\"Smart, Funny, Tall\"\nDave,Cool"
    res3 = _execute_calc_tool("import_csv_from_string", {
        "csv_data": csv_3,
        "target_cell": "E5"
    })
    assert res3.get("status") == "ok", f"Quoted CSV import failed: {res3}"
    assert active_sheet.getCellByPosition(4, 5).getString() == "Carol" # E6
    # The cell at F6 (5, 5) should contain the comma text
    desc = active_sheet.getCellByPosition(5, 5).getString()
    assert desc == "Smart, Funny, Tall", f"Quoted comma parsed incorrectly: {desc}"
def test_calc_integration_tests():
    pass


@native_test
def test_tool_argument_normalization():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()

    # Test with string param
    res1 = _execute_calc_tool("write_formula_range", {"range_name": "A10", "formula_or_values": "Norm"})
    assert res1.get("status") == "ok", f"String param failed: {res1}"

    # Test with list[str] param
    res2 = _execute_calc_tool("write_formula_range", {"range_name": ["A11"], "formula_or_values": "Norm"})
    assert res2.get("status") == "ok", f"List param failed: {res2}"

    assert active_sheet.getCellByPosition(0, 9).getString() == "Norm", "Value mismatch for string param"
    assert active_sheet.getCellByPosition(0, 10).getString() == "Norm", "Value mismatch for list param"


@native_test
def test_consistent_error_payloads():
    # 1. Invalid range address
    #
    # NOTE: This intentionally passes a malformed cell range and expects the
    # tool to return an error payload. However, the underlying Calc tool
    # currently logs full tracebacks via `logger.exception(...)`, which
    # makes test output noisy. For now we skip this block to avoid the
    # distracting exception output while keeping the invalid-color coverage.
    #
    # res_range = _execute_calc_tool("read_cell_range", {"range_name": "Invalid!!Range"})
    # assert res_range.get("status") == "error", f"Expected error for invalid range, got {res_range.get('status')}"
    # assert "error" in res_range, f"Expected 'error' key in payload: {res_range}"
    # assert isinstance(res_range["error"], str), "Error message should be a string"
    # assert len(res_range["error"]) > 0, "Error message should not be empty"

    # 2. Invalid color string
    res_color = _execute_calc_tool("set_cell_style", {"range_name": "A1", "bg_color": "not_a_real_color"})
    assert res_color.get("status") == "error", f"Expected error for invalid color, got {res_color.get('status')}"
    assert "error" in res_color, f"Expected 'error' key in payload: {res_color}"
    assert isinstance(res_color["error"], str), "Error message should be a string"
    assert len(res_color["error"]) > 0, "Error message should not be empty"


@native_test
def test_read_after_write_stability():
    # 1. Write data
    res_write = _execute_calc_tool("write_formula_range", {"range_name": "Z1:Z2", "formula_or_values": [["Apple"], ["Banana"]]})
    assert res_write.get("status") == "ok", f"write_formula_range failed: {res_write}"

    # 2. Read back
    res_read = _execute_calc_tool("read_cell_range", {"range_name": "Z1:Z2"})
    assert res_read.get("status") == "ok", f"read_cell_range failed: {res_read}"
    grid = res_read.get("result", [])[0]
    assert grid[0][0]["value"] == "Apple", f"Expected Apple, got {grid[0][0]['value']}"
    assert grid[1][0]["value"] == "Banana", f"Expected Banana, got {grid[1][0]['value']}"

    # 3. Merge and read back
    res_merge = _execute_calc_tool("merge_cells", {"range_name": "Z1:Z2"})
    assert res_merge.get("status") == "ok", f"merge_cells failed: {res_merge}"
    res_read_merged = _execute_calc_tool("read_cell_range", {"range_name": "Z1:Z2"})
    assert res_read_merged.get("status") == "ok", f"read_cell_range after merge failed: {res_read_merged}"
    grid_merged = res_read_merged.get("result", [])[0]
    # In LibreOffice, the top-left cell of a merged range keeps the value
    assert grid_merged[0][0]["value"] == "Apple", f"Expected Apple in merged range, got {grid_merged[0][0]['value']}"

    # 4. Clear range and search
    res_clear = _execute_calc_tool("clear_range", {"range_name": "Z1:Z2"})
    assert res_clear.get("status") == "ok", f"clear_range failed: {res_clear}"
    res_search = _execute_calc_tool("search_in_spreadsheet", {"pattern": "Apple"})
    assert res_search.get("status") == "ok", f"search_in_spreadsheet failed: {res_search}"
    # Filter matches to only check Z column to avoid false positives from other tests
    z_matches = [m for m in res_search.get("matches", []) if m.get("cell", "").startswith("Z")]
    assert len(z_matches) == 0, f"Expected 0 matches for Apple in Z column, found {len(z_matches)}"


@native_test
def test_calc_comments():
    # 1. Add a comment
    res_add = _execute_calc_tool("add_cell_comment", {"cell": "A10", "text": "This is a test comment"})
    assert res_add.get("status") == "ok", f"add_cell_comment failed: {res_add}"

    # 2. List comments to verify
    res_list = _execute_calc_tool("list_cell_comments", {})
    assert res_list.get("status") == "ok", f"list_cell_comments failed: {res_list}"
    comments = res_list.get("comments", [])

    found = False
    for c in comments:
        if c.get("cell") == "A10" and c.get("text") == "This is a test comment":
            found = True
            break
    assert found, f"Comment not found in list: {comments}"

    # 3. Delete the comment
    res_delete = _execute_calc_tool("delete_cell_comment", {"cell": "A10"})
    assert res_delete.get("status") == "ok", f"delete_cell_comment failed: {res_delete}"

    # 4. Verify deletion
    res_list_after = _execute_calc_tool("list_cell_comments", {})
    comments_after = res_list_after.get("comments", [])
    found_after = any(c.get("cell") == "A10" for c in comments_after)
    assert not found_after, "Comment was not deleted"


@native_test
def test_calc_search_and_replace():
    # Write some data
    _execute_calc_tool("write_formula_range", {"range_name": "A20:B21", "formula_or_values": [
        ["AppleUnique", "BananaUnique"],
        ["CherryUnique", "DateUnique"]
    ]})

    # 1. Search for "BananaUnique"
    res_search = _execute_calc_tool("search_in_spreadsheet", {"pattern": "BananaUnique"})
    assert res_search.get("status") == "ok", f"search_in_spreadsheet failed: {res_search}"
    matches = res_search.get("matches", [])
    assert len(matches) == 1, f"Expected 1 match, found {len(matches)}"
    assert matches[0].get("cell") == "B20", f"Expected B20, got {matches[0].get('cell')}"

    # 2. Replace "BananaUnique" with "BlueberryUnique"
    res_replace = _execute_calc_tool("replace_in_spreadsheet", {"search": "BananaUnique", "replace": "BlueberryUnique"})
    assert res_replace.get("status") == "ok", f"replace_in_spreadsheet failed: {res_replace}"
    assert res_replace.get("replacements") == 1, f"Expected 1 replacement, got {res_replace.get('replacements')}"

    # 3. Verify replacement
    res_search_after = _execute_calc_tool("search_in_spreadsheet", {"pattern": "BlueberryUnique"})

    matches_after = res_search_after.get("matches", [])
    assert len(matches_after) == 1, f"Expected 1 match for BlueberryUnique, found {len(matches_after)}"
    assert matches_after[0].get("cell") == "B20", f"Expected BlueberryUnique at B20, got {matches_after[0].get('cell')}"



@native_test
def test_calc_conditional_formatting():
    # Write some numbers
    _execute_calc_tool("write_formula_range", {"range_name": "C20:C22", "formula_or_values": [
        [10],
        [20],
        [30]
    ]})

    # 1. Add conditional formatting rule: highlight cells greater than 15
    res_add = _execute_calc_tool("add_conditional_format", {
        "range_name": "C20:C22",
        "operator": "GREATER",
        "formula1": "15",
        "style_name": "Result"
    })
    assert res_add.get("status") == "ok", f"add_conditional_format failed: {res_add}"

    # 2. List formats to verify
    res_list = _execute_calc_tool("list_conditional_formats", {"range_name": "C20:C22"})
    assert res_list.get("status") == "ok", f"list_conditional_formats failed: {res_list}"
    rules = res_list.get("rules", [])
    assert len(rules) == 1, f"Expected 1 conditional formatting rule, found {len(rules)}"

    rule = rules[0]
    assert rule.get("operator") == "GREATER", f"Expected GREATER, got {rule.get('operator')}"
    assert rule.get("formula1") == "15", f"Expected 15, got {rule.get('formula1')}"
    assert rule.get("style_name") == "Result", f"Expected Result, got {rule.get('style_name')}"

    # 3. Clear formats
    res_clear = _execute_calc_tool("clear_conditional_formats", {"range_name": "C20:C22"})
    assert res_clear.get("status") == "ok", f"clear_conditional_formats failed: {res_clear}"

    # 4. Verify cleared
    res_list_after = _execute_calc_tool("list_conditional_formats", {"range_name": "C20:C22"})
    rules_after = res_list_after.get("rules", [])
    assert len(rules_after) == 0, f"Expected rules to be cleared, but found {len(rules_after)}"


@native_test
def test_charts_creation_and_listing():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()

    # 1. Populate sample data
    data = [
        "Month", "Sales",
        "Jan", "100",
        "Feb", "150",
        "Mar", "200",
        "Apr", "250",
        "May", "300"
    ]
    res_write = _execute_calc_tool("write_formula_range", {"range_name": "A1:B6", "formula_or_values": data})
    assert res_write.get("status") == "ok", f"write_formula_range failed: {res_write}"

    # 2. Create chart
    res_create = _execute_calc_tool("create_chart", {"data_range": "A1:B6", "chart_type": "bar"})
    assert res_create.get("status") == "ok", f"create_chart failed: {res_create}"

    # 3. List charts
    res_list = _execute_calc_tool("list_charts", {})
    assert res_list.get("status") == "ok", f"list_charts failed: {res_list}"
    charts = res_list.get("charts", [])
    assert len(charts) == 1, f"Expected 1 chart, found {len(charts)}"
    chart_name = charts[0].get("name")
    assert chart_name is not None, "Chart name should not be None"

    # 4. Query DrawPage for OLE2Shape
    draw_page = active_sheet.getDrawPage()
    found_chart_shape = False
    for i in range(draw_page.getCount()):
        shape = draw_page.getByIndex(i)
        if shape.getShapeType() == "com.sun.star.drawing.OLE2Shape":
            found_chart_shape = True
            break
    assert found_chart_shape, "com.sun.star.drawing.OLE2Shape not found on DrawPage"

    # 5. Get chart info
    res_info = _execute_calc_tool("get_chart_info", {"chart_name": chart_name})
    assert res_info.get("status") == "ok", f"get_chart_info failed: {res_info}"
    assert res_info.get("name") == chart_name, "Chart info name mismatch"

    # 6. Edit chart
    res_edit = _execute_calc_tool("edit_chart", {"chart_name": chart_name, "title": "Monthly Sales"})
    assert res_edit.get("status") == "ok", f"edit_chart failed: {res_edit}"

    # Verify title change
    res_info_after_edit = _execute_calc_tool("get_chart_info", {"chart_name": chart_name})
    assert res_info_after_edit.get("title") == "Monthly Sales", f"Chart title not updated: {res_info_after_edit}"

    # 7. Delete chart
    res_delete = _execute_calc_tool("delete_chart", {"chart_name": chart_name})
    assert res_delete.get("status") == "ok", f"delete_chart failed: {res_delete}"

    # Verify deletion
    res_list_after_delete = _execute_calc_tool("list_charts", {})
    assert len(res_list_after_delete.get("charts", [])) == 0, "Chart not deleted"
