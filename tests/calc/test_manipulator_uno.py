# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import execute_calc_tool as _execute_calc_tool, with_native_doc


@native_test
@with_native_doc("calc")
def test_undo_restores_previously_empty_cells(ctx, doc):
    from com.sun.star.table.CellContentType import EMPTY

    sheet = doc.getCurrentController().getActiveSheet()
    res = _execute_calc_tool(doc, ctx, "write_range", {"range": ["H1:I1"], "values": [["A", "B"]]})
    assert res.get("status") == "ok", f"write failed: {res}"
    assert sheet.getCellByPosition(7, 0).getString() == "A"

    doc.getUndoManager().undo()
    assert sheet.getCellByPosition(7, 0).getType() == EMPTY, "undo left H1 non-empty"
    assert sheet.getCellByPosition(8, 0).getType() == EMPTY, "undo left I1 non-empty"


@native_test
@with_native_doc("calc")
def test_refused_array_write_leaves_no_undo_step(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    sheet.getCellByPosition(1, 1).setString("keep-me")
    undo_mgr = doc.getUndoManager()
    before = list(undo_mgr.getAllUndoActionTitles())
    was_modified = doc.isModified()

    res = _execute_calc_tool(doc, ctx, "write_range", {"range": ["A1"], "values": "=SEQUENCE(3;2)"})
    assert res.get("status") == "error", f"occupied write should fail: {res}"
    assert res.get("code") == "SPILL_BLOCKED", res
    after = list(undo_mgr.getAllUndoActionTitles())
    assert after == before, f"refused array write left an undo step: before={before} after={after} res={res}"
    assert doc.isModified() == was_modified, "refused array write changed the modified flag"


@native_test
@with_native_doc("calc")
def test_clear_part_of_array_is_refused(ctx, doc):
    from plugin.calc.bridge import CalcBridge
    from plugin.calc.manipulator import CellManipulator
    from plugin.framework.errors import CalcError

    sheet = doc.getCurrentController().getActiveSheet()
    res = _execute_calc_tool(doc, ctx, "write_range", {"range": ["A5"], "values": "=SEQUENCE(2;1)"})
    assert res.get("status") == "ok", f"array write failed: {res}"

    manipulator = CellManipulator(CalcBridge(doc))
    try:
        manipulator.clear_range("A6")
        raise AssertionError("clearing part of an array should fail")
    except CalcError as e:
        assert "is part of array" in str(e), str(e)

    manipulator.clear_range("A5:A6")
    assert sheet.getCellByPosition(0, 4).getFormula() == ""


@native_test
@with_native_doc("calc")
def test_manipulator_insert_structure_and_unmerge(ctx, doc):
    from plugin.calc.bridge import CalcBridge
    from plugin.calc.manipulator import CellManipulator

    bridge = CalcBridge(doc)
    manipulator = CellManipulator(bridge)
    sheet = bridge.get_active_sheet()
    sheet.getCellByPosition(0, 0).setString("shifted")
    manipulator.insert_structure("rows", 1, count=1)
    assert sheet.getCellByPosition(0, 0).getString() == ""
    assert sheet.getCellByPosition(0, 1).getString() == "shifted"
    merged_range = sheet.getCellRangeByPosition(0, 0, 1, 0)
    merged_range.merge(True)
    assert merged_range.getIsMerged() is True
    manipulator.merge_cells("A1:B1", center=False, unmerge=True)
    assert merged_range.getIsMerged() is False


@native_test
@with_native_doc("calc")
def test_range_sort_reorders_visible_rows(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    fixture = (
        ("Region", "Revenue"),
        ("North", 1.0),
        ("East", 9.0),
        ("South", 4.0),
        ("Mid", 4.0),
        ("West", 7.0),
    )
    sheet.getCellRangeByName("A1:B6").setDataArray(fixture)

    res = _execute_calc_tool(
        doc,
        ctx,
        "sort_range",
        {"range": ["A1:B6"], "sort_column": 1, "ascending": False, "has_header": True},
    )
    assert res.get("status") == "ok", f"sort failed: {res}"
    assert "no rows moved" not in res.get("message", "")

    col_a = [row[0] for row in sheet.getCellRangeByName("A1:A6").getDataArray()]
    assert col_a == ["Region", "East", "West", "South", "Mid", "North"]

    col_b = [row[0] for row in sheet.getCellRangeByName("B1:B6").getDataArray()]
    assert col_b == ["Revenue", 9.0, 7.0, 4.0, 4.0, 1.0]

    undo_mgr = doc.getUndoManager()
    assert list(undo_mgr.getAllUndoActionTitles())[0] == "WriterAgent: Sort range"

    undo_mgr.undo()
    col_a_restored = [row[0] for row in sheet.getCellRangeByName("A1:A6").getDataArray()]
    assert col_a_restored == ["Region", "North", "East", "South", "Mid", "West"]

    undo_mgr.redo()
    col_a_redone = [row[0] for row in sheet.getCellRangeByName("A1:A6").getDataArray()]
    assert col_a_redone == ["Region", "East", "West", "South", "Mid", "North"]

    res_noop = _execute_calc_tool(
        doc,
        ctx,
        "sort_range",
        {"range": ["A1:B6"], "sort_column": 1, "ascending": False, "has_header": True},
    )
    assert res_noop.get("status") == "ok", f"second sort failed: {res_noop}"
    assert "no rows moved" in res_noop.get("message", "")
    col_a_after = [row[0] for row in sheet.getCellRangeByName("A1:A6").getDataArray()]
    assert col_a_after == ["Region", "East", "West", "South", "Mid", "North"]


@native_test
@with_native_doc("calc")
def test_set_style_v_align_and_merge_center_vertical_justify_uno(ctx, doc):
    from com.sun.star.table.CellHoriJustify import CENTER

    sheet = doc.getCurrentController().getActiveSheet()
    for addr, v_align, expected in (("A1", "top", 1), ("A2", "center", 2), ("A3", "bottom", 3)):
        res = _execute_calc_tool(doc, ctx, "set_style", {"range": [addr], "v_align": v_align})
        assert res.get("status") == "ok", f"set_style failed for {v_align}: {res}"
        row = int(addr[1:]) - 1
        assert sheet.getCellByPosition(0, row).getPropertyValue("VertJustify") == expected

    res_merge = _execute_calc_tool(doc, ctx, "merge_cells", {"range": ["C1:D2"]})
    assert res_merge.get("status") == "ok", f"merge_cells failed: {res_merge}"
    c1 = sheet.getCellByPosition(2, 0)
    assert c1.getPropertyValue("VertJustify") == 2
    assert c1.getPropertyValue("HoriJustify") == CENTER


@native_test
@with_native_doc("calc")
def test_refuse_occupied_cells_uno(ctx, doc):
    from plugin.calc.manipulator import refuse_occupied_cells
    from plugin.framework.errors import CalcError

    sheet = doc.getCurrentController().getActiveSheet()
    sheet.getCellByPosition(0, 0).setValue(0)
    sheet.getCellByPosition(1, 0).setFormula('=""')
    sheet.getCellByPosition(2, 0).setString("text")

    annotations = sheet.getAnnotations()
    annotations.insertNew(sheet.getCellByPosition(3, 0).getCellAddress(), "note")
    sheet.getCellByPosition(4, 0).setPropertyValue("CellBackColor", 0xFFFF00)

    try:
        refuse_occupied_cells(sheet, 0, 0, 4, 0)
        raise AssertionError("occupied range should raise CalcError")
    except CalcError as e:
        msg = str(e)
        assert "3 cell(s)" in msg, msg
        assert "(first: A1)" in msg, msg

    refuse_occupied_cells(sheet, 3, 0, 4, 0)


@native_test
@with_native_doc("calc")
def test_py_inside_its_data_range_is_refused(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    sheet.getCellByPosition(0, 0).setValue(1)
    sheet.getCellByPosition(0, 1).setValue(2)

    res = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["A1"], "values": '=PY("result = np.sum(data)"; A1:A2)'},
    )
    assert res.get("status") == "error", res
    assert res.get("code") == "PY_CIRCULAR", res
    assert res.get("suggested_call", {}).get("arguments", {}).get("range") == ["Sheet1.B1"], res
    assert sheet.getCellByPosition(0, 0).getValue() == 1.0


@native_test
@with_native_doc("calc")
def test_bang_ref_formula_evaluates_uno(ctx, doc):
    sheets = doc.getSheets()
    sheets.insertNewByName("Orders", 1)
    sheets.insertNewByName("My Sheet", 2)
    sheets.getByName("Orders").getCellByPosition(0, 0).setValue(5)
    sheets.getByName("My Sheet").getCellByPosition(0, 0).setValue(11)
    active = doc.getCurrentController().getActiveSheet()

    res = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["D1:D2"], "values": [["=Orders!A1*2"], ["='My Sheet'!A1"]]},
    )
    assert res.get("status") == "ok", res
    assert "error_cells" not in res, res
    assert any(w.get("code") == "SHEET_REF_REWRITTEN" for w in res.get("warnings", [])), res
    c1 = active.getCellByPosition(3, 0)
    c2 = active.getCellByPosition(3, 1)
    assert c1.getError() == 0 and c1.getValue() == 10.0, (c1.getError(), c1.getValue())
    assert c1.getFormula() == "=Orders.A1*2", c1.getFormula()
    assert c2.getError() == 0 and c2.getValue() == 11.0, (c2.getError(), c2.getValue())

    missing_res = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["D3"], "values": "=Missing!A1"},
    )
    assert missing_res.get("error_cells"), missing_res
    formula_warns = [w for w in missing_res.get("warnings", []) if w.get("code") == "FORMULA_ERROR"]
    assert formula_warns and "Sheet.A1" in formula_warns[0]["message"], formula_warns


@native_test
@with_native_doc("calc")
def test_repeated_row_formula_warns_without_rewriting_uno(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    for row_idx, val in enumerate((1, 2, 3), start=1):
        sheet.getCellByPosition(1, row_idx).setValue(val)

    res = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["C2:C4"], "values": [["=B2*2"], ["=B2*2"], ["=B2*2"]]},
    )
    assert res.get("status") == "ok", res
    warns = [w for w in res.get("warnings", []) if w.get("code") == "REPEATED_FORMULA"]
    assert warns and warns[0].get("suggested_call", {}).get("arguments", {}).get("values") == "=B2*2", res
    assert [sheet.getCellByPosition(2, r).getValue() for r in (1, 2, 3)] == [2.0, 2.0, 2.0]


@native_test
@with_native_doc("calc")
def test_py_literal_newlines_are_repaired_on_write(ctx, doc):
    from plugin.calc.python.function import clear_python_addin_cache
    from plugin.calc.python.workbook_state import WorkbookState

    clear_python_addin_cache()
    try:
        sheet = doc.getCurrentController().getActiveSheet()

        res_a = _execute_calc_tool(
            doc,
            ctx,
            "write_range",
            {"range": ["A1"], "values": '=PY("x = 2\\nresult = x * 21")'},
        )
        res_b = _execute_calc_tool(
            doc,
            ctx,
            "write_range",
            {"range": ["B1"], "values": '=PY("s = ""a\\nb""\\nresult = len(s)")'},
        )
        res_c = _execute_calc_tool(
            doc,
            ctx,
            "write_range",
            {"range": ["C1"], "values": '=PY("# total\\nresult = 6 * 7")'},
        )
        doc.calculateAll()

        for res in (res_a, res_b, res_c):
            assert res.get("status") == "ok", res
            assert "Err:" not in res.get("message", ""), res

        a1 = sheet.getCellByPosition(0, 0)
        assert a1.getError() == 0
        assert a1.getValue() == 42.0
        assert "\n" in a1.getFormula()

        b1 = sheet.getCellByPosition(1, 0)
        assert b1.getError() == 0
        assert b1.getValue() == 3.0

        c1 = sheet.getCellByPosition(2, 0)
        assert c1.getError() == 0
        assert c1.getValue() == 42.0
    finally:
        st = WorkbookState.known(doc)
        if st is not None:
            WorkbookState.forget_key(st.key)
