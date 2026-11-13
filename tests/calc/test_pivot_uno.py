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

    res = _execute_calc_tool(doc, ctx, "pivot_table_create", {
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
    assert res.get("status") == "ok", f"pivot_table_create failed: {res}"

    res_list = _execute_calc_tool(doc, ctx, "pivot_table_list", {"sheet": sn})
    assert res_list.get("status") == "ok", f"pivot_table_list failed: {res_list}"
    names = [p.get("name") for p in res_list.get("pivot_tables", [])]
    assert "WA_PivotTest" in names, f"Expected WA_PivotTest in {names}"

    res_ref = _execute_calc_tool(doc, ctx, "pivot_table_refresh", {
        "name": "WA_PivotTest",
        "sheet": sn,
    })
    assert res_ref.get("status") == "ok", f"pivot_table_refresh failed: {res_ref}"

    # Insert second sheet and attempt creating duplicate pivot name WA_PivotTest on Sheet2
    if not doc.getSheets().hasByName("Sheet2"):
        doc.getSheets().insertNewByName("Sheet2", 1)

    res_dup = _execute_calc_tool(doc, ctx, "pivot_table_create", {
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

    res_sheet2 = _execute_calc_tool(doc, ctx, "pivot_table_list", {"sheet": "Sheet2"})
    assert res_sheet2.get("status") == "ok", f"pivot_table_list failed: {res_sheet2}"
    sheet2_pivots = res_sheet2.get("pivot_tables", [])
    assert sheet2_pivots == [], f"Expected no pivot on Sheet2 after rejected duplicate, got {sheet2_pivots}"
    assert "WA_PivotTest" not in [p.get("name") for p in sheet2_pivots]

    res_nosheet = _execute_calc_tool(doc, ctx, "pivot_table_refresh", {
        "name": "WA_PivotTest",
        "sheet": "NoSuchSheet",
    })
    assert res_nosheet.get("status") == "error"
    assert res_nosheet.get("code") == "UNO_OBJECT_ERROR"
    assert "Available:" in res_nosheet.get("message", "")
    assert "hint" in res_nosheet.get("details", {})

    res_miss = _execute_calc_tool(doc, ctx, "pivot_table_refresh", {
        "name": "WA_PivotTestX",
    })
    assert res_miss.get("status") == "error"
    assert res_miss.get("code") == "PIVOT_TABLE_NOT_FOUND"
    assert "WA_PivotTest" in res_miss.get("details", {}).get("similar", [])

    res_create_nosheet = _execute_calc_tool(doc, ctx, "pivot_table_create", {
        "name": "WA_PivotMissingSrc",
        "source_range": "A1:B6",
        "source_sheet": "NoSuchSheet",
        "destination_sheet": sn,
        "destination_cell": "D15",
        "data_fields": ["Sales"],
    })
    assert res_create_nosheet.get("status") == "error"
    assert res_create_nosheet.get("code") == "UNO_OBJECT_ERROR"

    if not doc.getSheets().hasByName("_wa_hidden"):
        doc.getSheets().insertNewByName("_wa_hidden", 2)

    res_hidden = _execute_calc_tool(doc, ctx, "pivot_table_create", {
        "name": "WA_Hidden",
        "source_range": "A1:B6",
        "source_sheet": sn,
        "destination_sheet": "_wa_hidden",
        "destination_cell": "A1",
        "data_fields": ["Sales"],
    })
    assert res_hidden.get("status") == "ok", f"create on _wa_hidden failed: {res_hidden}"

    res_list_all = _execute_calc_tool(doc, ctx, "pivot_table_list", {})
    assert res_list_all.get("status") == "ok"
    all_names = [p.get("name") for p in res_list_all.get("pivot_tables", [])]
    assert "WA_Hidden" not in all_names, f"Internal sheet pivot leaked into list: {all_names}"

    res_bare_hidden = _execute_calc_tool(doc, ctx, "pivot_table_refresh", {"name": "WA_Hidden"})
    assert res_bare_hidden.get("status") == "error"
    assert res_bare_hidden.get("code") == "PIVOT_TABLE_NOT_FOUND"

    res_explicit_hidden = _execute_calc_tool(doc, ctx, "pivot_table_refresh", {
        "name": "WA_Hidden",
        "sheet": "_wa_hidden",
    })
    assert res_explicit_hidden.get("status") == "ok", f"explicit hidden refresh failed: {res_explicit_hidden}"

    res_range_dest = _execute_calc_tool(doc, ctx, "pivot_table_create", {
        "name": "WA_PivotRangeDest",
        "source_range": "A1:B6",
        "source_sheet": sn,
        "destination_sheet": sn,
        "destination_cell": "H20:I21",
        "row_fields": ["Month"],
        "data_fields": ["Sales"],
    })
    assert res_range_dest.get("status") == "ok", f"create with range dest failed: {res_range_dest}"

    res_list_sn = _execute_calc_tool(doc, ctx, "pivot_table_list", {"sheet": sn})
    assert res_list_sn.get("status") == "ok"
    by_name = {p["name"]: p for p in res_list_sn.get("pivot_tables", [])}
    assert by_name["WA_PivotRangeDest"]["output_range"].startswith("H20:")


@native_test
@with_native_doc("calc")
def test_pivot_create_on_apostrophe_sheet_uno(ctx, doc):
    sheets = doc.getSheets()
    sheets.insertNewByName("O'Brien", sheets.getCount())

    res_write = _execute_calc_tool(doc, ctx, "write_formula_range", {
        "range": "'O''Brien'.A1:B3",
        "values": [["Month", "Sales"], ["Jan", "100"], ["Feb", "150"]],
    })
    assert res_write.get("status") == "ok", f"write_formula_range failed: {res_write}"

    res = _execute_calc_tool(doc, ctx, "pivot_table_create", {
        "name": "ApostrophePivot",
        "source_range": "A1:B3",
        "source_sheet": "O'Brien",
        "destination_sheet": "O'Brien",
        "destination_cell": "D1",
        "row_fields": ["Month"],
        "data_fields": ["Sales"],
    })
    assert res.get("status") == "ok", f"pivot_table_create failed: {res}"

    sheet = sheets.getByName("O'Brien")
    rows = sheet.getCellRangeByName("D1:E6").getDataArray()
    assert any(row[0] == "Jan" for row in rows), f"Expected 'Jan' row in {rows}"
    assert any(250 in row or 250.0 in row for row in rows), f"Expected total 250 in {rows}"


@native_test
@with_native_doc("calc")
def test_pivot_create_is_one_undo_step_uno(ctx, doc):
    from plugin.doc.compound_undo import undo_titles

    sheet = doc.getSheets().getByIndex(0)
    sn = sheet.getName()
    res_write = _execute_calc_tool(
        doc,
        ctx,
        "write_formula_range",
        {
            "range": "A1:B4",
            "values": [["Month", "Sales"], ["Jan", "100"], ["Feb", "150"], ["Mar", "200"]],
        },
    )
    assert res_write.get("status") == "ok"

    before = undo_titles(doc)
    assert before is not None

    res = _execute_calc_tool(
        doc,
        ctx,
        "pivot_table_create",
        {
            "name": "WA_PivotUndo",
            "source_range": "A1:B4",
            "source_sheet": sn,
            "destination_sheet": sn,
            "destination_cell": "E1",
            "row_fields": ["Month"],
            "data_fields": ["Sales"],
        },
    )
    assert res.get("status") == "ok"

    after = undo_titles(doc)
    assert after is not None
    delta = after[: len(after) - len(before)]
    assert delta == ("WriterAgent: Create pivot table",)

    doc.getUndoManager().undo()
    res_list = _execute_calc_tool(doc, ctx, "pivot_table_list", {"sheet": sn})
    names = [p["name"] for p in res_list.get("pivot_tables", [])]
    assert "WA_PivotUndo" not in names
    assert sheet.getCellByPosition(4, 0).getString() == ""
