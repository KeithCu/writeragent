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
def test_formulas_error_detector():
    from plugin.calc.formulas import DetectErrors
    from plugin.framework.tool import ToolContext

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
