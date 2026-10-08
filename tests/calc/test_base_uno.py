# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import execute_calc_tool as _execute_calc_tool, with_native_doc


@native_test
@with_native_doc("calc")
def test_unknown_tool(ctx, doc):
    res = _execute_calc_tool(doc, ctx, "bad_tool", {})
    assert res.get("status") == "error", f"unknown tool handling failed: {res}"


@native_test
@with_native_doc("calc")
def test_calc_integration_tests(ctx, doc):
    pass


@native_test
@with_native_doc("calc")
def test_tool_argument_normalization(ctx, doc):
    active_sheet = doc.getCurrentController().getActiveSheet()

    # Test with string param
    res1 = _execute_calc_tool(doc, ctx, "write_formula_range", {"range": "A10", "values": "Norm"})
    assert res1.get("status") == "ok", f"String param failed: {res1}"

    # Test with list[str] param
    res2 = _execute_calc_tool(doc, ctx, "write_formula_range", {"range": ["A11"], "values": "Norm"})
    assert res2.get("status") == "ok", f"List param failed: {res2}"

    assert active_sheet.getCellByPosition(0, 9).getString() == "Norm", "Value mismatch for string param"
    assert active_sheet.getCellByPosition(0, 10).getString() == "Norm", "Value mismatch for list param"


@native_test
@with_native_doc("calc")
def test_consistent_error_payloads(ctx, doc):
    # 2. Invalid color string (standardized tool error: status/code/message/details)
    res_color = _execute_calc_tool(doc, ctx, "set_style", {"range": "A1", "bg_color": "not_a_real_color"})
    assert res_color.get("status") == "error", f"Expected error for invalid color, got {res_color.get('status')}"
    assert "message" in res_color, f"Expected 'message' key in payload: {res_color}"
    assert isinstance(res_color["message"], str), "Error message should be a string"
    assert len(res_color["message"]) > 0, "Error message should not be empty"
    assert "code" in res_color, f"Expected 'code' key in payload: {res_color}"
