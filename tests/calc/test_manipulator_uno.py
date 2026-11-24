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
    c1 = active.getCellByPosition(3, 0)
    c2 = active.getCellByPosition(3, 1)
    assert c1.getError() == 0 and c1.getValue() == 10.0, (c1.getError(), c1.getValue())
    assert c1.getFormula() == "=$Orders.A1*2", c1.getFormula()
    assert c2.getError() == 0 and c2.getValue() == 11.0, (c2.getError(), c2.getValue())

    missing_res = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["D3"], "values": "=Missing!A1"},
    )
    assert missing_res.get("error_cells"), missing_res
    formula_warns = [w for w in missing_res.get("warnings", []) if w.get("code") == "FORMULA_ERROR"]
    assert formula_warns and "Formulas use Calc syntax:" in formula_warns[0]["message"], formula_warns


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


@native_test
@with_native_doc("calc")
def test_anchor_expanded_write_counts_overwritten_cells(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    sheet.getCellByPosition(0, 0).setString("old")
    sheet.getCellByPosition(1, 1).setValue(7)

    res = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["A1"], "values": [["x", "y"], ["z", 1]]},
    )
    assert res.get("status") == "ok", res
    assert "overwrote 2 non-empty cell(s)" in res.get("message", ""), res
    assert sheet.getCellByPosition(0, 0).getString() == "x"
    assert sheet.getCellByPosition(1, 1).getValue() == 1.0


def _format_key(doc, format_str: str) -> int:
    formats = doc.getNumberFormats()
    locale = doc.getPropertyValue("CharLocale")
    key = formats.queryKey(format_str, locale, False)
    if key == -1:
        key = formats.addNew(format_str, locale)
    return int(key)


def _cell_key(sheet, col: int, row: int) -> int:
    return int(sheet.getCellByPosition(col, row).getPropertyValue("NumberFormat"))


@native_test
@with_native_doc("calc")
def test_s29_restore_keeps_date_column_after_near_miss_text_uno(ctx, doc):
    """A near-miss text (08/05/2026) into a YYYY-MM-DD cell keeps the date key; an apostrophe literal keeps @."""
    from com.sun.star.table.CellContentType import VALUE

    sheet = doc.getCurrentController().getActiveSheet()
    date_key = _format_key(doc, "YYYY-MM-DD")
    text_key = _format_key(doc, "@")
    sheet.getCellRangeByPosition(0, 0, 3, 0).setPropertyValue("NumberFormat", date_key)

    # Control: a raw string commit of the same near-miss text rewrites the cell to @.
    sheet.getCellRangeByPosition(3, 0, 3, 0).setDataArray((("08/05/2026",),))
    assert _cell_key(sheet, 3, 0) == text_key, "raw setDataArray should force @ on a near-miss text"

    res = _execute_calc_tool(
        doc, ctx, "write_range", {"range": ["A1:C1"], "values": [["2026-08-08", "08/05/2026", "'2026-08-08"]]}
    )
    assert res.get("status") == "ok", res
    a1 = sheet.getCellByPosition(0, 0)
    assert a1.getType() == VALUE and a1.getString() == "2026-08-08", (a1.getType(), a1.getString())
    b1 = sheet.getCellByPosition(1, 0)
    assert b1.getString() == "08/05/2026"
    assert _cell_key(sheet, 1, 0) == date_key, f"S29 restore missing: B1 key {_cell_key(sheet, 1, 0)} != {date_key}"
    assert _cell_key(sheet, 2, 0) == text_key, "apostrophe literal must keep @ (S18)"
    # The restored key is the real cell format: a serial typed into B1 renders as an ISO date.
    b1.setValue(46242)
    assert b1.getString() == "2026-08-08", b1.getString()


@native_test
@with_native_doc("calc")
def test_s29_restore_groups_by_prior_key_uno(ctx, doc):
    """Checkerboard of two keys over A1:B4: every text cell gets its own prior key back."""
    sheet = doc.getCurrentController().getActiveSheet()
    date_key = _format_key(doc, "YYYY-MM-DD")
    num_key = _format_key(doc, "0.00")
    expected = {}
    for row in range(4):
        for col in range(2):
            key = date_key if (row + col) % 2 == 0 else num_key
            sheet.getCellByPosition(col, row).setPropertyValue("NumberFormat", key)
            expected[(col, row)] = key

    # Every value must miss the ISO gate (no 2026-08-08 or 12:30 shapes) so all eight commit as
    # text and go through the S29 restore rather than the temporal format pass.
    values = [["08/05/2026", "1,234"], ["50%", "$5"], ["$12", "08/06/2026"], ["2,345", "75%"]]
    res = _execute_calc_tool(doc, ctx, "write_range", {"range": ["A1:B4"], "values": values})
    assert res.get("status") == "ok", res
    after = {(col, row): _cell_key(sheet, col, row) for (col, row) in expected}
    assert after == expected, f"restored keys differ: {after} != {expected}"
    assert sheet.getCellByPosition(1, 0).getString() == "1,234"


@native_test
@with_native_doc("calc")
def test_s29_batched_restore_large_column_uno(ctx, doc):
    """3000 text cells into a date column keep their date key; the elapsed time is printed, not asserted."""
    import time

    sheet = doc.getCurrentController().getActiveSheet()
    date_key = _format_key(doc, "YYYY-MM-DD")
    sheet.getCellRangeByPosition(0, 0, 0, 2999).setPropertyValue("NumberFormat", date_key)
    values = [["08/05/2026" if i % 10 == 0 else f"note {i}"] for i in range(3000)]

    t0 = time.perf_counter()
    res = _execute_calc_tool(doc, ctx, "write_range", {"range": ["A1:A3000"], "values": values})
    elapsed = time.perf_counter() - t0
    print(f"write_range 3000 text cells into a date column: {elapsed:.2f}s")
    assert res.get("status") == "ok", res
    # Only the near-miss rows (every tenth) are rewritten to @ by Calc, so they are the ones that
    # prove the restore reached the whole column; a note row is the no-op control.
    lost = [row for row in range(0, 3000, 10) if _cell_key(sheet, 0, row) != date_key]
    assert not lost, f"near-miss rows lost their date key: {lost[:5]} ({len(lost)} of 300)"
    assert _cell_key(sheet, 0, 2999) == date_key, "A3000 (note row) lost its date key"
    assert sheet.getCellByPosition(0, 2990).getString() == "08/05/2026"


@native_test
@with_native_doc("calc")
def test_range_sort_then_by_breaks_ties(ctx, doc):
    """Multi-key sort_range breaks ties, records one undo step, and leaves no leftover keys."""
    sheet = doc.getCurrentController().getActiveSheet()
    rows = [
        ["Product", "Revenue"],
        ["Widget", 1200],
        ["Gadget", 850],
        ["Tool", 2100],
        ["Device", 1200],
        ["Bolt", 1200],
    ]
    w_res = _execute_calc_tool(doc, ctx, "write_range", {"range": ["A1:B6"], "values": rows})
    assert w_res.get("status") == "ok", w_res

    def col_a() -> list[str]:
        return [sheet.getCellByPosition(0, r).getString() for r in range(6)]

    res = _execute_calc_tool(
        doc,
        ctx,
        "sort_range",
        {
            "range": ["A1:B6"],
            "sort_column": 1,
            "ascending": False,
            "then_by": [{"column": 0, "ascending": True}],
            "has_header": True,
        },
    )
    assert res.get("status") == "ok", res
    assert col_a() == ["Product", "Tool", "Bolt", "Device", "Widget", "Gadget"]

    undo_mgr = doc.getUndoManager()
    assert undo_mgr.getCurrentUndoActionTitle() == "WriterAgent: Sort range"
    undo_mgr.undo()
    assert col_a() == ["Product", "Widget", "Gadget", "Tool", "Device", "Bolt"]

    undo_mgr.redo()
    assert col_a() == ["Product", "Tool", "Bolt", "Device", "Widget", "Gadget"]

    res_again = _execute_calc_tool(
        doc,
        ctx,
        "sort_range",
        {
            "range": ["A1:B6"],
            "sort_column": 1,
            "ascending": False,
            "then_by": [{"column": 0, "ascending": True}],
            "has_header": True,
        },
    )
    assert res_again.get("status") == "ok", res_again
    assert "no rows moved" in res_again.get("message", "")

    res_one = _execute_calc_tool(
        doc,
        ctx,
        "sort_range",
        {"range": ["A1:B6"], "sort_column": 0, "ascending": True, "has_header": True},
    )
    assert res_one.get("status") == "ok", res_one
    assert col_a() == ["Product", "Bolt", "Device", "Gadget", "Tool", "Widget"]


@native_test
@with_native_doc("calc")
def test_range_sort_then_by_reports_tie_only_move(ctx, doc):
    """Reordering only tied rows must not report 'no rows moved'."""
    sheet = doc.getCurrentController().getActiveSheet()
    rows = [
        ["Product", "Revenue"],
        ["Tool", 2100],
        ["Widget", 1200],
        ["Device", 1200],
        ["Gadget", 850],
    ]
    w_res = _execute_calc_tool(doc, ctx, "write_range", {"range": ["A1:B5"], "values": rows})
    assert w_res.get("status") == "ok", w_res

    res = _execute_calc_tool(
        doc,
        ctx,
        "sort_range",
        {
            "range": ["A1:B5"],
            "sort_column": 1,
            "ascending": False,
            "then_by": [{"column": 0, "ascending": True}],
            "has_header": True,
        },
    )
    assert res.get("status") == "ok", res
    assert "no rows moved" not in res.get("message", "")
    col_a = [sheet.getCellByPosition(0, r).getString() for r in range(5)]
    assert col_a == ["Product", "Tool", "Device", "Widget", "Gadget"]


@native_test
@with_native_doc("calc")
def test_sort_range_bad_key_refused_before_any_sort(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    res_w1 = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["A1:B3"], "values": [["Name", "Val"], ["Z", 2], ["A", 1]]},
    )
    assert res_w1.get("status") == "ok", res_w1
    res_w2 = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["D1:E3"], "values": [["K", "V"], ["b", 2], ["a", 1]]},
    )
    assert res_w2.get("status") == "ok", res_w2

    undo_mgr = doc.getUndoManager()
    undo_mgr.undo()
    undo_titles_before = undo_mgr.getAllUndoActionTitles()
    redo_titles_before = undo_mgr.getAllRedoActionTitles()

    res = _execute_calc_tool(
        doc,
        ctx,
        "sort_range",
        {"range": ["A1:B3", "D1:D3"], "sort_column": 1, "ascending": True, "has_header": True},
    )
    assert res.get("status") == "error", res
    assert res.get("code") == "INVALID_PARAM", res
    assert "D1:D3" in res.get("message", ""), res
    assert [sheet.getCellByPosition(0, r).getString() for r in range(3)] == ["Name", "Z", "A"]
    assert undo_mgr.getAllUndoActionTitles() == undo_titles_before
    assert undo_mgr.getAllRedoActionTitles() == redo_titles_before


@native_test
@with_native_doc("calc")
def test_sort_range_coerces_nested_then_by_strings_uno(ctx, doc):
    from plugin.calc.cells import SortRange
    from plugin.framework.tool import ToolContext, prepare_tool_call

    sheet = doc.getCurrentController().getActiveSheet()
    res_w = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["A1:B4"], "values": [["Name", "Group"], ["A", 1], ["C", 1], ["B", 1]]},
    )
    assert res_w.get("status") == "ok", res_w

    tctx = ToolContext(doc=doc, ctx=ctx, doc_type="calc", services=None, caller="test")
    tool = SortRange()
    prep = prepare_tool_call(
        tool,
        tctx,
        "sort_range",
        {
            "range": ["A1:B4"],
            "sort_column": 1,
            "ascending": True,
            "then_by": [{"column": "0", "ascending": "false"}],
            "has_header": True,
        },
    )
    assert prep.error is None, prep.error
    res = tool.execute(tctx, **prep.kwargs)
    assert res.get("status") == "ok", res
    assert [sheet.getCellByPosition(0, r).getString() for r in range(4)] == ["Name", "C", "B", "A"]
