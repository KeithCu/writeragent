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
def test_calc_pivot_table(ctx, doc):
    sn = doc.getSheets().getByIndex(0).getName()
    res_write = _execute_calc_tool(doc, ctx, "write_formula_range", {
        "range": "A1:B6",
        "values": [
            ["Month", "Sales"],
            ["Jan", "100"],
            ["Feb", "150"],
            ["Mar", "200"],
            ["Apr", "250"],
            ["May", "300"],
        ],
    })
    assert res_write.get("status") == "ok", f"write_formula_range failed: {res_write}"

    res = _execute_calc_tool(doc, ctx, "create_pivot_table", {
        "name": "WA_PivotTest",
        "source_range": "A1:B6",
        "source_sheet": sn,
        "destination_sheet": sn,
        "destination_cell": "D1",
        "row_fields": ["Month"],
        "column_fields": [],
        "data_fields": ["Sales"],
        "page_fields": [],
    })
    assert res.get("status") == "ok", f"create_pivot_table failed: {res}"

    res_list = _execute_calc_tool(doc, ctx, "list_pivot_tables", {"sheet": sn})
    assert res_list.get("status") == "ok", f"list_pivot_tables failed: {res_list}"
    names = [p.get("name") for p in res_list.get("pivot_tables", [])]
    assert "WA_PivotTest" in names, f"Expected WA_PivotTest in {names}"

    res_ref = _execute_calc_tool(doc, ctx, "refresh_pivot_table", {
        "name": "WA_PivotTest",
        "sheet": sn,
    })
    assert res_ref.get("status") == "ok", f"refresh_pivot_table failed: {res_ref}"

    # Insert second sheet and attempt creating duplicate pivot name WA_PivotTest on Sheet2
    if not doc.getSheets().hasByName("Sheet2"):
        doc.getSheets().insertNewByName("Sheet2", 1)

    res_dup = _execute_calc_tool(doc, ctx, "create_pivot_table", {
        "name": "WA_PivotTest",
        "source_range": "A1:B6",
        "source_sheet": sn,
        "destination_sheet": "Sheet2",
        "destination_cell": "A1",
        "data_fields": ["Sales"],
    })
    assert res_dup.get("status") == "error"
    msg = res_dup.get("message", "")
    assert "already exists" in msg, f"Unexpected duplicate error: {msg}"

    res_sheet2 = _execute_calc_tool(doc, ctx, "list_pivot_tables", {"sheet": "Sheet2"})
    assert res_sheet2.get("status") == "ok", f"list_pivot_tables failed: {res_sheet2}"
    sheet2_pivots = res_sheet2.get("pivot_tables", [])
    assert sheet2_pivots == [], f"Expected no pivot on Sheet2 after rejected duplicate, got {sheet2_pivots}"
    assert "WA_PivotTest" not in [p.get("name") for p in sheet2_pivots]

