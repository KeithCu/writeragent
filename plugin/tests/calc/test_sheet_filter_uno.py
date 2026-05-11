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
def test_calc_sheet_filter_apply_get_clear():
    active_sheet = _test_doc.getCurrentController().getActiveSheet()
    sheet_rows = active_sheet.getRows()

    def _visible_row_count(i0: int, i1: int) -> int:
        """Count 0-based row indices in [i0, i1] with ``TableRow.IsVisible`` true."""
        n = 0
        for ri in range(i0, i1 + 1):
            if sheet_rows.getByIndex(ri).IsVisible:
                n += 1
        return n

    # G40:I43 → header row index 39; data rows Alice/Bob/Carol at 40–42.
    data_i0, data_i1 = 40, 42

    res_write = _execute_calc_tool("write_formula_range", {
        "range_name": "G40:I43",
        "formula_or_values": [
            ["Name", "Region", "Score"],
            ["Alice", "East", "10"],
            ["Bob", "West", "20"],
            ["Carol", "East", "30"],
        ],
    })
    assert res_write.get("status") == "ok", f"write_formula_range failed: {res_write}"

    res_apply = _execute_calc_tool("apply_sheet_filter", {
        "range_name": "G40:I43",
        "contains_header": True,
        "criteria": [
            {"field": 1, "operator": "CONTAINS", "value": "East"},
        ],
    })
    assert res_apply.get("status") == "ok", f"apply_sheet_filter failed: {res_apply}"

    res_get = _execute_calc_tool("get_sheet_filter", {"range_name": "G40:I43"})
    assert res_get.get("status") == "ok", f"get_sheet_filter failed: {res_get}"
    crit = res_get.get("criteria", [])
    assert len(crit) >= 1, crit
    assert crit[0].get("operator") == "CONTAINS", crit[0]
    assert crit[0].get("field") == 1, crit[0]

    res_apply_or = _execute_calc_tool("apply_sheet_filter", {
        "range_name": "G40:I43",
        "contains_header": True,
        "criteria": [
            {"field": 1, "operator": "CONTAINS", "value": "East"},
            {
                "field": 2,
                "operator": "GREATER",
                "value": "15",
                "is_numeric": True,
                "connection": "OR",
            },
        ],
    })
    assert res_apply_or.get("status") == "ok", f"apply_sheet_filter OR chain failed: {res_apply_or}"

    assert _visible_row_count(data_i0, data_i1) == 3, "OR: East or Score>15 should show all three data rows"

    res_get_or = _execute_calc_tool("get_sheet_filter", {"range_name": "G40:I43"})
    assert res_get_or.get("status") == "ok", f"get_sheet_filter after OR apply failed: {res_get_or}"
    crit_or = res_get_or.get("criteria", [])
    assert len(crit_or) == 2, crit_or
    assert crit_or[1].get("operator") == "GREATER", crit_or[1]
    # Do not assert crit_or[1]["connection"] == "OR": LibreOffice may report AND on
    # getFilterFields2 readback even when the active filter is OR (validated above).

    res_apply_and = _execute_calc_tool("apply_sheet_filter", {
        "range_name": "G40:I43",
        "contains_header": True,
        "criteria": [
            {"field": 1, "operator": "CONTAINS", "value": "East"},
            {
                "field": 2,
                "operator": "GREATER",
                "value": "15",
                "is_numeric": True,
            },
        ],
    })
    assert res_apply_and.get("status") == "ok", f"apply_sheet_filter AND chain failed: {res_apply_and}"
    assert _visible_row_count(data_i0, data_i1) == 1, "AND: only Carol matches East and Score>15"

    res_clear = _execute_calc_tool("clear_sheet_filter", {"range_name": "G40:I43", "contains_header": True})
    assert res_clear.get("status") == "ok", f"clear_sheet_filter failed: {res_clear}"

    res_get2 = _execute_calc_tool("get_sheet_filter", {"range_name": "G40:I43"})
    assert res_get2.get("status") == "ok", f"get_sheet_filter after clear failed: {res_get2}"
    assert res_get2.get("count", -1) == 0, res_get2
