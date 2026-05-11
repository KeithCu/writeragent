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
def test_import_csv_from_string():
    doc = _test_doc
    active_sheet = doc.getCurrentController().getActiveSheet()

    # Test case 1: Standard comma-separated
    csv_1 = "Name,Age\nAlice,30\nBob,25"
    res1 = _execute_calc_tool("write_formula_range", {
        "formula_or_values": csv_1,
        "range_name": "E1"
    })
    assert res1.get("status") == "ok", f"CSV import failed: {res1}"
    assert active_sheet.getCellByPosition(4, 0).getString() == "Name" # E1
    assert active_sheet.getCellByPosition(5, 0).getString() == "Age"  # F1
    assert active_sheet.getCellByPosition(4, 1).getString() == "Alice" # E2
    assert active_sheet.getCellByPosition(5, 1).getValue() == 30.0    # F2
    assert active_sheet.getCellByPosition(5, 2).getValue() == 25.0    # F3

    # Test case 2: Semicolon-separated
    csv_2 = "Item;Price\nApple;1.5\nBanana;0.75"
    res2 = _execute_calc_tool("write_formula_range", {
        "formula_or_values": csv_2,
        "range_name": "G1"
    })
    assert res2.get("status") == "ok", f"Semicolon CSV import failed: {res2}"
    assert active_sheet.getCellByPosition(6, 0).getString() == "Item" # G1
    assert active_sheet.getCellByPosition(7, 0).getString() == "Price"# H1
    assert active_sheet.getCellByPosition(6, 1).getString() == "Apple"# G2
    assert active_sheet.getCellByPosition(7, 1).getValue() == 1.5     # H2

    # Test case 3: CSV with quoted commas
    csv_3 = "Person,Description\nCarol,\"Smart, Funny, Tall\"\nDave,Cool"
    res3 = _execute_calc_tool("write_formula_range", {
        "formula_or_values": csv_3,
        "range_name": "E5"
    })
    assert res3.get("status") == "ok", f"Quoted CSV import failed: {res3}"
    assert active_sheet.getCellByPosition(4, 5).getString() == "Carol" # E6
    # The cell at F6 (5, 5) should contain the comma text
    desc = active_sheet.getCellByPosition(5, 5).getString()
    assert desc == "Smart, Funny, Tall", f"Quoted comma parsed incorrectly: {desc}"