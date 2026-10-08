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
