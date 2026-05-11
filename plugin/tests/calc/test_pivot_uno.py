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
def test_calc_pivot_table():
    sn = _test_doc.getSheets().getByIndex(0).getName()
    res_write = _execute_calc_tool("write_formula_range", {
        "range_name": "A1:B6",
        "formula_or_values": [
            ["Month", "Sales"],
            ["Jan", "100"],
            ["Feb", "150"],
            ["Mar", "200"],
            ["Apr", "250"],
            ["May", "300"],
        ],
    })
    assert res_write.get("status") == "ok", f"write_formula_range failed: {res_write}"

    res = _execute_calc_tool("create_pivot_table", {
        "pivot_table_name": "WA_PivotTest",
        "source_range": "A1:B6",
        "source_sheet_name": sn,
        "destination_sheet_name": sn,
        "destination_cell": "D1",
        "row_fields": ["Month"],
        "column_fields": [],
        "data_fields": ["Sales"],
        "page_fields": [],
    })
    assert res.get("status") == "ok", f"create_pivot_table failed: {res}"

    res_list = _execute_calc_tool("list_pivot_tables", {"sheet_name": sn})
    assert res_list.get("status") == "ok", f"list_pivot_tables failed: {res_list}"
    names = [p.get("name") for p in res_list.get("pivot_tables", [])]
    assert "WA_PivotTest" in names, f"Expected WA_PivotTest in {names}"

    res_ref = _execute_calc_tool("refresh_pivot_table", {
        "pivot_table_name": "WA_PivotTest",
        "sheet_name": sn,
    })
    assert res_ref.get("status") == "ok", f"refresh_pivot_table failed: {res_ref}"
