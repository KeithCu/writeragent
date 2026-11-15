# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native LibreOffice tests for Excel-dialect formula acceptance across Calc tools."""

from __future__ import annotations

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import execute_calc_tool as _execute_calc_tool, with_native_doc


@native_test
@with_native_doc("calc")
def test_excel_commas_nested_and_strings_uno(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    sheet.getCellByPosition(0, 0).setValue(2)
    sheet.getCellByPosition(1, 0).setValue(10)

    res = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {
            "range": ["C1:C3"],
            "values": [
                ["=IF(SUM(A1,B1)>5,MAX(A1,B1),MIN(A1,B1))"],
                ['=IF(A1>0,"a,b;c","no")'],
                ['=TEXTJOIN(";",TRUE,A1:B1)'],
            ],
        },
    )
    assert res.get("status") == "ok", res
    assert "error_cells" not in res, res

    c1 = sheet.getCellByPosition(2, 0)
    assert c1.getError() == 0 and c1.getValue() == 10.0, (c1.getError(), c1.getValue())
    assert c1.getFormula() == "=IF(SUM(A1;B1)>5;MAX(A1;B1);MIN(A1;B1))"

    c2 = sheet.getCellByPosition(2, 1)
    assert c2.getError() == 0 and c2.getString() == "a,b;c"

    c3 = sheet.getCellByPosition(2, 2)
    assert c3.getError() == 0 and c3.getString() == "2;10"


@native_test
@with_native_doc("calc")
def test_excel_inline_arrays_and_calc_only_functions_uno(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    sheet.getCellByPosition(0, 0).setValue(1)
    sheet.getCellByPosition(0, 1).setValue(2)

    arr_res = _execute_calc_tool(doc, ctx, "write_range", {"range": ["D1"], "values": "=TRANSPOSE({1,2;3,4})"})
    assert arr_res.get("status") == "ok", arr_res
    assert arr_res.get("array_range") == "D1:E2", arr_res
    assert sheet.getCellRangeByPosition(3, 0, 4, 1).getDataArray() == ((1.0, 3.0), (2.0, 4.0))

    res = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {
            "range": ["C1:C3"],
            "values": [
                ["=SUM(A1:A2,{1,2;3,4})"],
                ['=REGEX("abc","b","x")'],
                ["=ISLEAPYEAR(DATE(2024,1,1))"],
            ],
        },
    )
    assert res.get("status") == "ok", res
    assert "error_cells" not in res, res
    assert sheet.getCellByPosition(2, 0).getValue() == 13.0
    assert sheet.getCellByPosition(2, 1).getString() == "axc"
    assert sheet.getCellByPosition(2, 2).getValue() == 1.0


@native_test
@with_native_doc("calc")
def test_excel_cross_sheet_and_mixed_refs_uno(ctx, doc):
    sheets = doc.getSheets()
    sheets.insertNewByName("Orders", 1)
    sheets.insertNewByName("My Sheet", 2)
    orders = sheets.getByName("Orders")
    orders.getCellByPosition(0, 0).setValue(5)
    orders.getCellByPosition(0, 1).setValue(7)
    sheets.getByName("My Sheet").getCellByPosition(0, 0).setValue(11)

    active = doc.getCurrentController().getActiveSheet()
    active.getCellByPosition(1, 0).setValue(3)

    res = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {
            "range": ["D1:D6"],
            "values": [
                ["=Orders!A1*2"],
                ["='My Sheet'!A1"],
                ["=SUM(Orders!A1:A2,B1)"],
                ["=SUM(Orders!A1:A2;B1)"],
                ["=SUM(Orders.A1:A2,B1)"],
                ["=IF($Orders.A1>0,1,0)"],
            ],
        },
    )
    assert res.get("status") == "ok", res
    assert "error_cells" not in res, res
    assert active.getCellByPosition(3, 0).getValue() == 10.0
    assert active.getCellByPosition(3, 0).getFormula() == "=$Orders.A1*2"
    assert active.getCellByPosition(3, 1).getValue() == 11.0
    assert active.getCellByPosition(3, 1).getFormula() == "=$'My Sheet'.A1"
    assert active.getCellByPosition(3, 2).getValue() == 15.0
    assert active.getCellByPosition(3, 3).getValue() == 15.0
    assert active.getCellByPosition(3, 3).getFormula() == "=SUM($Orders.A1:A2;B1)"
    assert active.getCellByPosition(3, 4).getValue() == 15.0
    assert active.getCellByPosition(3, 4).getFormula() == "=SUM(Orders.A1:A2;B1)"
    assert active.getCellByPosition(3, 5).getValue() == 1.0
    assert active.getCellByPosition(3, 5).getFormula() == "=IF($Orders.A1>0;1;0)"


@native_test
@with_native_doc("calc")
def test_invalid_mixed_and_calc_syntax_unchanged_uno(ctx, doc):
    sheets = doc.getSheets()
    sheets.insertNewByName("Orders", 1)
    active = doc.getCurrentController().getActiveSheet()
    active.getCellByPosition(0, 0).setValue(2)
    active.getCellByPosition(0, 1).setValue(3)
    active.getCellByPosition(1, 0).setValue(4)
    active.getCellByPosition(1, 1).setValue(5)

    bad_res = _execute_calc_tool(doc, ctx, "write_range", {"range": ["D1"], "values": "=SUM(A1;B1,C1)"})
    assert bad_res.get("error_cells"), bad_res
    assert bad_res["error_cells"][0]["error"] == "Err:508"
    assert active.getCellByPosition(3, 0).getFormula() == "=SUM(A1;B1,C1)"
    warns = [w for w in bad_res.get("warnings", []) if w.get("code") == "FORMULA_ERROR"]
    assert warns and "Formulas use Calc syntax:" in warns[0]["message"], warns

    name_res = _execute_calc_tool(doc, ctx, "write_range", {"range": ["D2"], "values": "=FOO(1,2)"})
    assert name_res.get("error_cells") and name_res["error_cells"][0]["error"] == "#NAME?", name_res

    # Calc syntax stays verbatim, including union (~), intersection (!),
    # string with comma, and inline array ({1;2|3;4}).
    calc_res = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {
            "range": ["E1:E4"],
            "values": [
                ["=SUM(A1;B1)"],
                ["=SUM(A1:A2~B1:B2)"],
                ["=SUM(A1:B2!B1:C3)"],
                ['=IF(A1>0;"a,b";0)'],
            ],
        },
    )
    assert calc_res.get("status") == "ok", calc_res
    assert "error_cells" not in calc_res, calc_res
    assert active.getCellByPosition(4, 0).getFormula() == "=SUM(A1;B1)"
    assert active.getCellByPosition(4, 0).getValue() == 6.0
    assert active.getCellByPosition(4, 1).getFormula() == "=SUM(A1:A2~B1:B2)"
    assert active.getCellByPosition(4, 1).getValue() == 14.0
    assert active.getCellByPosition(4, 2).getFormula() == "=SUM(A1:B2!B1:C3)"
    assert active.getCellByPosition(4, 2).getValue() == 9.0
    assert active.getCellByPosition(4, 3).getFormula() == '=IF(A1>0;"a,b";0)'
    assert active.getCellByPosition(4, 3).getString() == "a,b"

    calc_arr = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["F1"], "values": "={1;2|3;4}", "array": True},
    )
    assert calc_arr.get("status") == "ok", calc_arr
    assert calc_arr.get("array_range") == "F1:G2", calc_arr
    assert active.getCellRangeByPosition(5, 0, 6, 1).getDataArray() == ((1.0, 2.0), (3.0, 4.0))

    # Structured reference (Calc-clean, raw) stays working.
    active.getCellByPosition(0, 4).setString("Col")
    active.getCellByPosition(0, 5).setValue(10)
    active.getCellByPosition(0, 6).setValue(20)
    db_range = active.getCellRangeByPosition(0, 4, 0, 6).getRangeAddress()
    doc.DatabaseRanges.addNewByName("Table1", db_range)
    struct_res = _execute_calc_tool(doc, ctx, "write_range", {"range": ["E3"], "values": "=SUM(Table1[Col])"})
    assert struct_res.get("status") == "ok", struct_res
    assert "error_cells" not in struct_res, struct_res
    assert active.getCellByPosition(4, 2).getValue() == 30.0
    assert active.getCellByPosition(4, 2).getFormula() == "=SUM(Table1[Col])"


@native_test
@with_native_doc("calc")
def test_excel_fill_down_bulk_dynamic_array_and_roundtrip_uno(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    for row_idx in range(5):
        sheet.getCellByPosition(0, row_idx).setValue(row_idx + 1)
        sheet.getCellByPosition(1, row_idx).setValue((row_idx + 1) * 10)

    fill_res = _execute_calc_tool(doc, ctx, "write_range", {"range": ["C1:C3"], "values": "=SUM(A1,B1)"})
    assert fill_res.get("status") == "ok", fill_res
    assert "error_cells" not in fill_res, fill_res
    assert sheet.getCellByPosition(2, 2).getFormula() == "=SUM(A3;B3)"
    assert sheet.getCellByPosition(2, 2).getValue() == 33.0

    bulk_res = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["D1:E2"], "values": [["=SUM(A1,B1)", "=A1*2"], ["=SUM(A2;B2)", "x"]]},
    )
    assert bulk_res.get("status") == "ok", bulk_res
    assert "error_cells" not in bulk_res, bulk_res
    assert sheet.getCellByPosition(3, 0).getValue() == 11.0
    assert sheet.getCellByPosition(4, 0).getValue() == 2.0
    assert sheet.getCellByPosition(3, 1).getValue() == 22.0
    assert sheet.getCellByPosition(4, 1).getString() == "x"

    dyn_res = _execute_calc_tool(doc, ctx, "write_range", {"range": ["F1"], "values": "=FILTER(A1:A5,A1:A5>2)"})
    assert dyn_res.get("status") == "ok", dyn_res
    assert dyn_res.get("array_range") == "F1:F3", dyn_res
    assert sheet.getCellRangeByPosition(5, 0, 5, 2).getDataArray() == ((3.0,), (4.0,), (5.0,))

    xlook_res = _execute_calc_tool(
        doc, ctx, "write_range", {"range": ["G1"], "values": "=_xlfn.XLOOKUP(2,A1:A5,B1:B5)"}
    )
    assert xlook_res.get("status") == "ok", xlook_res
    assert sheet.getCellByPosition(6, 0).getValue() == 20.0

    # Read-back round trip
    _execute_calc_tool(doc, ctx, "write_range", {"range": ["H1"], "values": '=IF(A1>0,"Yes, sir","No")'})
    read_res = _execute_calc_tool(doc, ctx, "read_range", {"range": ["H1"], "format": "cells"})
    read_formula = read_res["result"][0]["result"][0][0][0]["formula"]
    assert read_formula == '=IF(A1>0;"Yes, sir";"No")', read_res
    _execute_calc_tool(doc, ctx, "write_range", {"range": ["H2"], "values": read_formula})
    assert sheet.getCellByPosition(7, 1).getFormula() == read_formula
    assert sheet.getCellByPosition(7, 1).getString() == "Yes, sir"


@native_test
@with_native_doc("calc")
def test_excel_evaluate_conditional_and_named_range_uno(ctx, doc):
    sheets = doc.getSheets()
    sheets.insertNewByName("Orders", 1)
    orders = sheets.getByName("Orders")
    for r in range(5):
        orders.getCellByPosition(0, r).setValue(r + 1)

    active = doc.getCurrentController().getActiveSheet()
    active.getCellByPosition(0, 0).setValue(2)
    active.getCellByPosition(1, 0).setValue(10)

    eval_res = _execute_calc_tool(doc, ctx, "evaluate_formula", {"formula": "=SUM(A1,B1)", "cell": "C1"})
    assert eval_res.get("status") == "ok", eval_res
    assert eval_res.get("result") == 12.0
    assert eval_res.get("formula") == "=SUM(A1;B1)"

    cf_res = _execute_calc_tool(
        doc,
        ctx,
        "conditional_formatting_add",
        {"range": ["A1:A5"], "operator": "FORMULA", "formula1": "AND($A1>1,$A1<5)", "bold": True},
    )
    assert cf_res.get("status") == "ok", cf_res
    cf_list = _execute_calc_tool(doc, ctx, "conditional_formatting_list", {"range": ["A1:A5"]})
    assert cf_list["rules"][0]["formula1"] == "AND($A1>1;$A1<5)", cf_list

    nr_res = _execute_calc_tool(
        doc,
        ctx,
        "named_range_add",
        {"name": "OrderNums", "content": "Orders!$A$1:$A$5"},
    )
    assert nr_res.get("status") == "ok", nr_res
    nr = doc.NamedRanges.getByName("OrderNums")
    ra = nr.getReferredCells().getRangeAddress()
    assert (ra.Sheet, ra.StartRow, ra.EndRow) == (1, 0, 4)

    sum_res = _execute_calc_tool(doc, ctx, "write_range", {"range": ["D1"], "values": "=SUM(OrderNums)"})
    assert sum_res.get("status") == "ok", sum_res
    assert active.getCellByPosition(3, 0).getValue() == 15.0

    # Sheet-local named range resolved on target sheet during Excel-dialect translation.
    local_nr = _execute_calc_tool(
        doc,
        ctx,
        "named_range_add",
        {"name": "Rate", "content": "0.15", "scope": "Orders"},
    )
    assert local_nr.get("status") == "ok", local_nr
    local_formula = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["Orders.B1"], "values": "=ROUND(Rate*A1,2)"},
    )
    assert local_formula.get("status") == "ok", local_formula
    assert "error_cells" not in local_formula, local_formula
    assert orders.getCellByPosition(1, 0).getFormula() == "=ROUND(Rate*A1;2)"
    assert orders.getCellByPosition(1, 0).getValue() == 0.15


@native_test
@with_native_doc("calc")
def test_excel_py_formula_string_roundtrip_uno(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    py_code = "result = sum(x for x in [1, 2])"
    res = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["B1"], "values": f'=PY("{py_code}", A1:A3)'},
    )
    assert res.get("status") == "ok", res
    formula = sheet.getCellByPosition(1, 0).getFormula()
    assert f'"{py_code}";' in formula and formula.endswith("A1:A3)"), formula
    assert formula.upper().startswith("=PY(") or "PYTHONFUNCTION.PY(" in formula.upper(), formula
