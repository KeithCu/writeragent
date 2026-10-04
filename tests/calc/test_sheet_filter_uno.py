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
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _execute_calc_tool(doc, ctx, name, args):
    return TestingFactory.execute_tool(doc, ctx, name, args, doc_type="calc")


@native_test
@with_native_doc("calc")
def test_calc_sheet_filter_apply_get_clear(ctx, doc):
    active_sheet = doc.getCurrentController().getActiveSheet()
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

    res_write = _execute_calc_tool(doc, ctx, "write_formula_range", {
        "range": ["G40:I43"],
        "values": [
            ["Name", "Region", "Score"],
            ["Alice", "East", "10"],
            ["Bob", "West", "20"],
            ["Carol", "East", "30"],
        ],
    })
    assert res_write.get("status") == "ok", f"write_formula_range failed: {res_write}"

    res_apply = _execute_calc_tool(doc, ctx, "apply_sheet_filter", {
        "range": ["G40:I43"],
        "has_header": True,
        "criteria": [
            {"field": 1, "operator": "CONTAINS", "value": "East"},
        ],
    })
    assert res_apply.get("status") == "ok", f"apply_sheet_filter failed: {res_apply}"

    res_get = _execute_calc_tool(doc, ctx, "get_sheet_filter", {"range": ["G40:I43"]})
    assert res_get.get("status") == "ok", f"get_sheet_filter failed: {res_get}"
    crit = res_get.get("criteria", [])
    assert len(crit) >= 1, crit
    assert crit[0].get("operator") == "CONTAINS", crit[0]
    assert crit[0].get("field") == 1, crit[0]

    res_apply_or = _execute_calc_tool(doc, ctx, "apply_sheet_filter", {
        "range": ["G40:I43"],
        "has_header": True,
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

    res_get_or = _execute_calc_tool(doc, ctx, "get_sheet_filter", {"range": ["G40:I43"]})
    assert res_get_or.get("status") == "ok", f"get_sheet_filter after OR apply failed: {res_get_or}"
    crit_or = res_get_or.get("criteria", [])
    assert len(crit_or) == 2, crit_or
    assert crit_or[1].get("operator") == "GREATER", crit_or[1]
    # Do not assert crit_or[1]["connection"] == "OR": LibreOffice may report AND on
    # getFilterFields2 readback even when the active filter is OR (validated above).

    res_apply_and = _execute_calc_tool(doc, ctx, "apply_sheet_filter", {
        "range": ["G40:I43"],
        "has_header": True,
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

    res_clear = _execute_calc_tool(doc, ctx, "clear_sheet_filter", {"range": ["G40:I43"], "has_header": True})
    assert res_clear.get("status") == "ok", f"clear_sheet_filter failed: {res_clear}"

    res_get2 = _execute_calc_tool(doc, ctx, "get_sheet_filter", {"range": ["G40:I43"]})
    assert res_get2.get("status") == "ok", f"get_sheet_filter after clear failed: {res_get2}"
    assert res_get2.get("count", -1) == 0, res_get2

@native_test
@with_native_doc("calc")
def test_calc_sheet_filter_invalid_range(ctx, doc):
    res_apply = _execute_calc_tool(doc, ctx, "apply_sheet_filter", {
        "range": [],
        "has_header": True,
        "criteria": [
            {"field": 1, "operator": "CONTAINS", "value": "East"},
        ],
    })
    assert res_apply.get("status") == "error", f"Expected error for empty range in apply_sheet_filter, got {res_apply}"
    assert "range parameter must be a non-empty list" in res_apply.get("message", "")

    res_get = _execute_calc_tool(doc, ctx, "get_sheet_filter", {"range": []})
    assert res_get.get("status") == "error", f"Expected error for empty range in get_sheet_filter, got {res_get}"
    assert "range parameter must be a non-empty list" in res_get.get("message", "")

    res_clear = _execute_calc_tool(doc, ctx, "clear_sheet_filter", {"range": [], "has_header": True})
    assert res_clear.get("status") == "error", f"Expected error for empty range in clear_sheet_filter, got {res_clear}"
    assert "range parameter must be a non-empty list" in res_clear.get("message", "")
