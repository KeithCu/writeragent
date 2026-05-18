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

    # 3. Clear formats (using new unified tool)
    res_clear = _execute_calc_tool("remove_conditional_formats", {"range_name": "C20:C22"})
    assert res_clear.get("status") == "ok", f"remove_conditional_formats (clear) failed: {res_clear}"

    # 4. Verify cleared
    res_list_after = _execute_calc_tool("list_conditional_formats", {"range_name": "C20:C22"})
    rules_after = res_list_after.get("rules", [])
    assert len(rules_after) == 0, f"Expected rules to be cleared, but found {len(rules_after)}"

    # 5. Add two rules to test removing by index
    _execute_calc_tool("add_conditional_format", {
        "range_name": "C20:C22", "operator": "GREATER", "formula1": "10", "style_name": "Result"
    })
    _execute_calc_tool("add_conditional_format", {
        "range_name": "C20:C22", "operator": "LESS", "formula1": "5", "style_name": "Result"
    })

    # 6. Remove the first rule by index
    res_remove_idx = _execute_calc_tool("remove_conditional_formats", {"range_name": "C20:C22", "rule_index": 0})
    assert res_remove_idx.get("status") == "ok", f"remove_conditional_formats (index) failed: {res_remove_idx}"

    # 7. Verify only 1 rule remains
    res_list_final = _execute_calc_tool("list_conditional_formats", {"range_name": "C20:C22"})
    rules_final = res_list_final.get("rules", [])
    assert len(rules_final) == 1, f"Expected 1 rule remaining after removing by index, found {len(rules_final)}"

    # 8. BETWEEN + list round-trip (extended operator uses operator_code in list when available)
    _execute_calc_tool("remove_conditional_formats", {"range_name": "C20:C22"})
    _execute_calc_tool("write_formula_range", {"range_name": "C20:C22", "formula_or_values": [[5], [10], [15]]})
    res_between = _execute_calc_tool("add_conditional_format", {
        "range_name": "C20:C22",
        "operator": "BETWEEN",
        "formula1": "6",
        "formula2": "12",
        "style_name": "Result",
    })
    assert res_between.get("status") == "ok", f"BETWEEN add failed: {res_between}"
    res_lb = _execute_calc_tool("list_conditional_formats", {"range_name": "C20:C22"})
    br = res_lb.get("rules", [])
    assert len(br) == 1 and br[0].get("operator") == "BETWEEN", br
    assert br[0].get("formula1") == "6" and br[0].get("formula2") == "12", br[0]

    # 9. DUPLICATE (LibreOffice ConditionOperator2) — empty formula1
    _execute_calc_tool("remove_conditional_formats", {"range_name": "E20:E22"})
    _execute_calc_tool("write_formula_range", {"range_name": "E20:E22", "formula_or_values": [[1], [1], [2]]})
    res_dup = _execute_calc_tool("add_conditional_format", {
        "range_name": "E20:E22",
        "operator": "DUPLICATE",
        "style_name": "Result",
    })
    assert res_dup.get("status") == "ok", f"DUPLICATE add failed: {res_dup}"
    res_ld = _execute_calc_tool("list_conditional_formats", {"range_name": "E20:E22"})
    dr = res_ld.get("rules", [])
    assert len(dr) == 1, dr
    assert dr[0].get("operator") == "DUPLICATE", dr[0]
    assert dr[0].get("operator_code") == 10, dr[0]
