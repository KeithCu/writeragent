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
def test_calc_conditional_formatting(ctx, doc):
    # Write some numbers
    _execute_calc_tool(doc, ctx, "write_formula_range", {"range": ["C20:C22"], "values": [
        [10],
        [20],
        [30]
    ]})

    # 1. Add conditional formatting rule: highlight cells greater than 15
    res_add = _execute_calc_tool(doc, ctx, "conditional_formatting_add", {
        "range": ["C20:C22"],
        "operator": "GREATER",
        "formula1": "15",
        "style": "Result"
    })
    assert res_add.get("status") == "ok", f"conditional_formatting_add failed: {res_add}"

    # 2. List formats to verify
    res_list = _execute_calc_tool(doc, ctx, "conditional_formatting_list", {"range": ["C20:C22"]})
    assert res_list.get("status") == "ok", f"conditional_formatting_list failed: {res_list}"
    rules = res_list.get("rules", [])
    assert len(rules) == 1, f"Expected 1 conditional formatting rule, found {len(rules)}"

    rule = rules[0]
    assert rule.get("operator") == "GREATER", f"Expected GREATER, got {rule.get('operator')}"
    assert rule.get("formula1") == "15", f"Expected 15, got {rule.get('formula1')}"
    assert rule.get("style") == "Result", f"Expected Result, got {rule.get('style')}"

    # 3. Clear formats (using new unified tool)
    res_clear = _execute_calc_tool(doc, ctx, "conditional_formatting_remove", {"range": ["C20:C22"]})
    assert res_clear.get("status") == "ok", f"conditional_formatting_remove (clear) failed: {res_clear}"

    # 4. Verify cleared
    res_list_after = _execute_calc_tool(doc, ctx, "conditional_formatting_list", {"range": ["C20:C22"]})
    rules_after = res_list_after.get("rules", [])
    assert len(rules_after) == 0, f"Expected rules to be cleared, but found {len(rules_after)}"

    # 5. Add two rules to test removing by index
    _execute_calc_tool(doc, ctx, "conditional_formatting_add", {
        "range": ["C20:C22"], "operator": "GREATER", "formula1": "10", "style": "Result"
    })
    _execute_calc_tool(doc, ctx, "conditional_formatting_add", {
        "range": ["C20:C22"], "operator": "LESS", "formula1": "5", "style": "Result"
    })

    # 6. Remove the first rule by index
    res_remove_idx = _execute_calc_tool(doc, ctx, "conditional_formatting_remove", {"range": ["C20:C22"], "rule_index": 0})
    assert res_remove_idx.get("status") == "ok", f"conditional_formatting_remove (index) failed: {res_remove_idx}"

    # 7. Verify only 1 rule remains
    res_list_final = _execute_calc_tool(doc, ctx, "conditional_formatting_list", {"range": ["C20:C22"]})
    rules_final = res_list_final.get("rules", [])
    assert len(rules_final) == 1, f"Expected 1 rule remaining after removing by index, found {len(rules_final)}"

    # 8. BETWEEN + list round-trip (extended operator uses operator_code in list when available)
    _execute_calc_tool(doc, ctx, "conditional_formatting_remove", {"range": ["C20:C22"]})
    _execute_calc_tool(doc, ctx, "write_formula_range", {"range": ["C20:C22"], "values": [[5], [10], [15]]})
    res_between = _execute_calc_tool(doc, ctx, "conditional_formatting_add", {
        "range": ["C20:C22"],
        "operator": "BETWEEN",
        "formula1": "6",
        "formula2": "12",
        "style": "Result",
    })
    assert res_between.get("status") == "ok", f"BETWEEN add failed: {res_between}"
    res_lb = _execute_calc_tool(doc, ctx, "conditional_formatting_list", {"range": ["C20:C22"]})
    br = res_lb.get("rules", [])
    assert len(br) == 1 and br[0].get("operator") == "BETWEEN", br
    assert br[0].get("formula1") == "6" and br[0].get("formula2") == "12", br[0]

    # 9. DUPLICATE (LibreOffice ConditionOperator2) — empty formula1
    _execute_calc_tool(doc, ctx, "conditional_formatting_remove", {"range": ["E20:E22"]})
    _execute_calc_tool(doc, ctx, "write_formula_range", {"range": ["E20:E22"], "values": [[1], [1], [2]]})
    res_dup = _execute_calc_tool(doc, ctx, "conditional_formatting_add", {
        "range": ["E20:E22"],
        "operator": "DUPLICATE",
        "style": "Result",
    })
    assert res_dup.get("status") == "ok", f"DUPLICATE add failed: {res_dup}"
    res_ld = _execute_calc_tool(doc, ctx, "conditional_formatting_list", {"range": ["E20:E22"]})
    dr = res_ld.get("rules", [])
    assert len(dr) == 1, dr
    assert dr[0].get("operator") == "DUPLICATE", dr[0]
    assert dr[0].get("operator_code") == 10, dr[0]


@native_test
@with_native_doc("calc")
def test_calc_conditional_formula_source_position(ctx, doc):
    _execute_calc_tool(doc, ctx, "write_formula_range", {
        "range": ["C5:C9"],
        "values": [[50], [150], [80], [200], [90]],
    })
    res_add = _execute_calc_tool(doc, ctx, "conditional_formatting_add", {
        "range": ["C5:C9"],
        "operator": "FORMULA",
        "formula1": "C5>100",
        "style": "Result",
    })
    assert res_add.get("status") == "ok"
    res_list = _execute_calc_tool(doc, ctx, "conditional_formatting_list", {"range": ["C5:C9"]})
    rules = res_list.get("rules", [])
    assert len(rules) == 1
    assert rules[0]["operator"] == "FORMULA"
    assert rules[0]["formula1"] == "C5>100"


@native_test
@with_native_doc("calc")
def test_conditional_formatting_inline_style(ctx, doc):
    from com.sun.star.awt import FontWeight

    _execute_calc_tool(doc, ctx, "write_formula_range", {
        "range": ["C1:C3"],
        "values": [[10], [200], [50]],
    })
    res1 = _execute_calc_tool(doc, ctx, "conditional_formatting_add", {
        "range": ["C1:C3"],
        "operator": "GREATER",
        "formula1": "100",
        "bg_color": "red",
        "bold": True,
    })
    assert res1.get("status") == "ok", res1
    assert res1.get("style") == "WA CF bg #FF0000 bold"
    assert res1.get("style_created") is True

    cell_styles = doc.getStyleFamilies().getByName("CellStyles")
    assert cell_styles.hasByName("WA CF bg #FF0000 bold")
    st = cell_styles.getByName("WA CF bg #FF0000 bold")
    assert st.getPropertyValue("Hidden") is True
    assert int(st.getPropertyValue("CellBackColor")) == 0xFF0000
    assert st.getPropertyValue("CharWeight") == FontWeight.BOLD

    res_list = _execute_calc_tool(doc, ctx, "conditional_formatting_list", {"range": ["C1:C3"]})
    rules = res_list.get("rules", [])
    assert len(rules) == 1
    assert rules[0]["style"] == "WA CF bg #FF0000 bold"

    res2 = _execute_calc_tool(doc, ctx, "conditional_formatting_add", {
        "range": ["C1:C3"],
        "operator": "LESS",
        "formula1": "20",
        "bg_color": "red",
        "bold": True,
    })
    assert res2.get("status") == "ok", res2
    assert res2.get("style") == "WA CF bg #FF0000 bold"
    assert res2.get("style_created") is False
    wa_cf_names = [n for n in cell_styles.getElementNames() if n.startswith("WA CF")]
    assert wa_cf_names == ["WA CF bg #FF0000 bold"]
