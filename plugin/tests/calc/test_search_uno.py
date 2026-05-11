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
