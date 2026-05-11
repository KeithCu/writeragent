# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test

_test_doc = None
_test_ctx = None

@setup
def setup_calc_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    desktop = get_desktop(ctx)
    import uno
    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    _test_doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, (hidden_prop,))

@teardown
def teardown_calc_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None

def _execute_calc_tool(name, args):
    from plugin.main import get_tools, get_services
    from plugin.framework.tool import ToolContext
    # Pass suite bootstrap ctx (same as setup_calc_tests); None makes
    # get_desktop() use uno.getComponentContext() and can segfault under
    # python -m plugin.testing_runner.
    tctx = ToolContext(_test_doc, _test_ctx, "calc", get_services(), "test")
    try:
        res = get_tools().execute(name, tctx, **args)
    except (KeyError, ValueError) as e:
        res = {"status": "error", "error": str(e)}
    return res


@native_test
def test_set_cell_style_and_details():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    _execute_calc_tool("set_style", {"range_name": "A1", "bold": True, "bg_color": "yellow"})
    cell = active_sheet.getCellByPosition(0, 0)
    from com.sun.star.awt.FontWeight import BOLD
    assert cell.getPropertyValue("CharWeight") == BOLD, "Bold not set"
    assert cell.getPropertyValue("CellBackColor") == 0xFFFF00, "Background color not set"

    from plugin.calc.bridge import CalcBridge
    from plugin.calc.inspector import CellInspector
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
    _execute_calc_tool("write_formula_range", {"range_name": ["G1", "H1"], "formula_or_values": ""})
    assert active_sheet.getCellByPosition(6, 0).getString() == "", "G1 not cleared"
    assert active_sheet.getCellByPosition(7, 0).getString() == "", "H1 not cleared"


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
    res_clear = _execute_calc_tool("write_formula_range", {"range_name": "Z1:Z2", "formula_or_values": ""})
    assert res_clear.get("status") == "ok", f"write_formula_range clear failed: {res_clear}"
    res_search = _execute_calc_tool("search_in_spreadsheet", {"pattern": "Apple"})
    assert res_search.get("status") == "ok", f"search_in_spreadsheet failed: {res_search}"
    # Filter matches to only check Z column to avoid false positives from other tests
    z_matches = [m for m in res_search.get("matches", []) if m.get("cell", "").startswith("Z")]
    assert len(z_matches) == 0, f"Expected 0 matches for Apple in Z column, found {len(z_matches)}"
