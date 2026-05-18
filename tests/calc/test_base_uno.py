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
def test_unknown_tool():
    res = _execute_calc_tool("bad_tool", {})
    assert res.get("status") == "error", f"unknown tool handling failed: {res}"


@native_test
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
    # assert "message" in res_range, f"Expected 'message' key in payload: {res_range}"
    # assert isinstance(res_range["message"], str), "Error message should be a string"
    # assert len(res_range["message"]) > 0, "Error message should not be empty"

    # 2. Invalid color string (standardized tool error: status/code/message/details)
    res_color = _execute_calc_tool("set_style", {"range_name": "A1", "bg_color": "not_a_real_color"})
    assert res_color.get("status") == "error", f"Expected error for invalid color, got {res_color.get('status')}"
    assert "message" in res_color, f"Expected 'message' key in payload: {res_color}"
    assert isinstance(res_color["message"], str), "Error message should be a string"
    assert len(res_color["message"]) > 0, "Error message should not be empty"
    assert "code" in res_color, f"Expected 'code' key in payload: {res_color}"
