# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
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
    tctx = ToolContext(_test_doc, _test_ctx, "calc", get_services(), "test")
    try:
        res = get_tools().execute(name, tctx, **args)
    except Exception as e:
        res = {"status": "error", "error": str(e)}
    return res


@native_test
def test_add_and_list_named_ranges():
    # Make sure we clean up any old named ranges if existing
    named_ranges = _test_doc.NamedRanges
    if named_ranges.hasByName("MyTestRange"):
        named_ranges.removeByName("MyTestRange")
    if named_ranges.hasByName("OtherRange"):
        named_ranges.removeByName("OtherRange")

    # 1. Add Named Range
    res = _execute_calc_tool("add_named_range", {"name": "MyTestRange", "content": "$Sheet1.$A$1:$B$2"})
    assert res.get("status") == "ok", f"add_named_range failed: {res}"
    assert named_ranges.hasByName("MyTestRange"), "Named range was not created"

    # Add another
    res2 = _execute_calc_tool("add_named_range", {"name": "OtherRange", "content": "$Sheet1.$C$1"})
    assert res2.get("status") == "ok", f"add_named_range failed: {res2}"

    # 2. List Named Ranges
    res_list = _execute_calc_tool("list_named_ranges", {})
    assert res_list.get("status") == "ok"
    ranges = res_list.get("result", [])
    names = [r["name"] for r in ranges]
    assert "MyTestRange" in names
    assert "OtherRange" in names

    my_range_content = next(r["content"] for r in ranges if r["name"] == "MyTestRange")
    assert "$Sheet1.$A$1:$B$2" in my_range_content


@native_test
def test_delete_named_range():
    named_ranges = _test_doc.NamedRanges
    if not named_ranges.hasByName("MyTestRange"):
        from com.sun.star.table import CellAddress
        named_ranges.addNewByName("MyTestRange", "$Sheet1.$A$1:$B$2", CellAddress(Sheet=0, Column=0, Row=0), 0)

    res = _execute_calc_tool("delete_named_range", {"name": "MyTestRange"})
    assert res.get("status") == "ok", f"delete_named_range failed: {res}"
    assert not named_ranges.hasByName("MyTestRange"), "Named range was not deleted"


@native_test
def test_transparent_resolution_read_write():
    named_ranges = _test_doc.NamedRanges
    if named_ranges.hasByName("TransparentRange"):
        named_ranges.removeByName("TransparentRange")

    # Create a named range pointing to a 1x2 area (A10:B10)
    from com.sun.star.table import CellAddress
    named_ranges.addNewByName("TransparentRange", "$Sheet1.$A$10:$B$10", CellAddress(Sheet=0, Column=0, Row=0), 0)

    sheet = _test_doc.getSheets().getByIndex(0)
    sheet.getCellByPosition(0, 9).setString("Apple")
    sheet.getCellByPosition(1, 9).setString("Banana")

    # 1. Read using the named range
    res_read = _execute_calc_tool("read_cell_range", {"range_name": ["TransparentRange"]})
    assert res_read.get("status") == "ok", f"read_cell_range failed: {res_read}"
    
    # Structure from read_cell_range with multiple ranges is {"status": "ok", "result": [[[{"value": "Apple", ...}, ...]]]}
    result_data = res_read["result"][0]
    assert result_data[0][0]["value"] == "Apple"
    assert result_data[0][1]["value"] == "Banana"

    # 2. Write using the named range
    res_write = _execute_calc_tool("write_formula_range", {
        "range_name": ["TransparentRange"],
        "formula_or_values": '["Cherry", "Date"]'
    })
    assert res_write.get("status") == "ok", f"write_formula_range failed: {res_write}"
    assert sheet.getCellByPosition(0, 9).getString() == "Cherry"
    assert sheet.getCellByPosition(1, 9).getString() == "Date"

    # Clean up
    named_ranges.removeByName("TransparentRange")
