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
    res = _execute_calc_tool(doc, ctx, "write_formula_range", {"range": ["H1:I1"], "values": [["A", "B"]]})
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

    res = _execute_calc_tool(doc, ctx, "write_formula_range", {"range": ["A1"], "values": "=SEQUENCE(3;2)"})
    assert res.get("status") == "error", f"occupied write should fail: {res}"
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
    res = _execute_calc_tool(doc, ctx, "write_formula_range", {"range": ["A5"], "values": "=SEQUENCE(2;1)"})
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
    manipulator.insert_structure("rows", 1, count=1)
    sheet = bridge.get_active_sheet()
    sheet.getCellRangeByPosition(0, 0, 1, 0).merge(True)
    manipulator.merge_cells("A1:B1", center=False, unmerge=True)


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
        "range_sort",
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
        "range_sort",
        {"range": ["A1:B6"], "sort_column": 1, "ascending": False, "has_header": True},
    )
    assert res_noop.get("status") == "ok", f"second sort failed: {res_noop}"
    assert "no rows moved" in res_noop.get("message", "")
    col_a_after = [row[0] for row in sheet.getCellRangeByName("A1:A6").getDataArray()]
    assert col_a_after == ["Region", "East", "West", "South", "Mid", "North"]


@native_test
@with_native_doc("calc")
def test_set_style_v_align_and_merge_center_vertical_justify_uno(ctx, doc):
    from com.sun.star.table import CellHoriJustify

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
    assert c1.getPropertyValue("HoriJustify") == CellHoriJustify.CENTER
