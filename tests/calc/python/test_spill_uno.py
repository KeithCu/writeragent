# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for Calc auto-spill behavior."""

from __future__ import annotations

import json

from com.sun.star.sheet.CellDeleteMode import UP
from com.sun.star.table.CellContentType import EMPTY

from plugin.calc.python.formula_locator_cache import FormulaOrigin, is_matching_py_formula
from plugin.calc.python.sheet_modify import ensure_sheet_modify_listener, flush_sheet_modify_pass_for_tests
from plugin.calc.python.spill import PreparedSpill, perform_deferred_spill, prepare_auto_spill, queue_deferred_spill_write
from plugin.calc.python.workbook_state import WorkbookState
from plugin.doc.udprops import get_document_property
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_blocked_respill_clears_previous_cells_uno(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    code = "result = [1, 2, 3]"
    origin_cell = sheet.getCellByPosition(0, 0)
    origin_cell.setFormula(f'=PY("{code}")')

    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = st.key
    try:
        perform_deferred_spill(
            sheet_name,
            0,
            0,
            [[1], [2], [3]],
            doc=doc,
            sheet=sheet,
            code=code,
            key=key,
        )
        assert sheet.getCellByPosition(0, 1).getValue() == 2.0
        assert sheet.getCellByPosition(0, 2).getValue() == 3.0

        # Block row 3 (A4) and try to spill 4 rows via late-collision path.
        sheet.getCellByPosition(0, 3).setValue(7.0)
        perform_deferred_spill(
            sheet_name,
            0,
            0,
            [[1], [2], [3], [4]],
            doc=doc,
            sheet=sheet,
            code=code,
            key=key,
        )

        assert sheet.getCellByPosition(0, 1).getType() == EMPTY
        assert sheet.getCellByPosition(0, 2).getType() == EMPTY
        assert sheet.getCellByPosition(0, 3).getValue() == 7.0
        raw = get_document_property(doc, "WriterAgentSpillRegistry", "")
        data = json.loads(raw)
        assert data[f"{sheet_name}:0,0"]["rows"] == 1

        # Prepare-time release path: spill 3 rows again, then block and flush debounce token.
        sheet.getCellByPosition(0, 3).setString("")
        perform_deferred_spill(
            sheet_name,
            0,
            0,
            [[1], [2], [3]],
            doc=doc,
            sheet=sheet,
            code=code,
            key=key,
        )
        sheet.getCellByPosition(0, 3).setValue(8.0)
        res = prepare_auto_spill(
            ctx,
            doc,
            st,
            FormulaOrigin(sheet, origin_cell, sheet_name, 0, 0),
            code,
            [[1], [2], [3], [4]],
        )
        assert res == "#SPILL!"
        st.debounce.flush_for_tests(f"spill:{sheet_name}:0:0")
        assert sheet.getCellByPosition(0, 1).getType() == EMPTY
        assert sheet.getCellByPosition(0, 2).getType() == EMPTY
        assert sheet.getCellByPosition(0, 3).getValue() == 8.0
        data = json.loads(get_document_property(doc, "WriterAgentSpillRegistry", ""))
        assert data[f"{sheet_name}:0,0"]["rows"] == 1
    finally:
        WorkbookState.forget_key(key)


@native_test
@with_native_doc("calc")
def test_blocked_respill_protected_sheet_keeps_record_uno(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    code = "result = [1, 2, 3]"
    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code}")')

    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = st.key
    try:
        perform_deferred_spill(
            sheet_name,
            0,
            0,
            [[1], [2], [3]],
            doc=doc,
            sheet=sheet,
            code=code,
            key=key,
        )
        sheet.getCellByPosition(0, 3).setValue(7.0)
        sheet.protect("")
        perform_deferred_spill(
            sheet_name,
            0,
            0,
            [[1], [2], [3], [4]],
            doc=doc,
            sheet=sheet,
            code=code,
            key=key,
        )

        # Protected cells cannot be cleared; record keeps ownership (rows == 3).
        assert sheet.getCellByPosition(0, 1).getValue() == 2.0
        assert sheet.getCellByPosition(0, 2).getValue() == 3.0
        data = json.loads(get_document_property(doc, "WriterAgentSpillRegistry", ""))
        assert data[f"{sheet_name}:0,0"]["rows"] == 3

        # After unprotecting and freeing A4, the formula can reclaim its cells.
        sheet.unprotect("")
        sheet.getCellByPosition(0, 3).setString("")
        perform_deferred_spill(
            sheet_name,
            0,
            0,
            [[1], [2], [3], [4]],
            doc=doc,
            sheet=sheet,
            code=code,
            key=key,
        )
        assert sheet.getCellByPosition(0, 1).getValue() == 2.0
        assert sheet.getCellByPosition(0, 2).getValue() == 3.0
        assert sheet.getCellByPosition(0, 3).getValue() == 4.0
        data = json.loads(get_document_property(doc, "WriterAgentSpillRegistry", ""))
        assert data[f"{sheet_name}:0,0"]["rows"] == 4
    finally:
        sheet.unprotect("")
        WorkbookState.forget_key(key)


@native_test
@with_native_doc("calc")
def test_row_insert_then_immediate_recalc_respills_uno(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    code = "result = [1, 2, 3]"
    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code}")')

    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = st.key
    try:
        perform_deferred_spill(
            sheet_name,
            0,
            0,
            [[1], [2], [3]],
            doc=doc,
            sheet=sheet,
            code=code,
            key=key,
        )
        ensure_sheet_modify_listener(ctx, doc, sheet)
        saved_before = get_document_property(doc, "WriterAgentSpillRegistry", "")

        sheet.getRows().insertByIndex(0, 1)
        st.debounce.cancel(f"sheet:{sheet_name}")
        origin_cell = sheet.getCellByPosition(0, 1)
        grid4 = [[10], [20], [30], [40]]
        prepared = prepare_auto_spill(
            ctx,
            doc,
            st,
            FormulaOrigin(sheet, origin_cell, sheet_name, 1, 0),
            code,
            grid4,
        )
        assert isinstance(prepared, PreparedSpill)
        assert get_document_property(doc, "WriterAgentSpillRegistry", "") == saved_before

        queue_deferred_spill_write(st, doc, prepared, grid4)
        st.debounce.flush_for_tests(f"spill:{sheet_name}:1:0")
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)

        assert sheet.getCellByPosition(0, 0).getType() == EMPTY
        assert [sheet.getCellByPosition(0, r).getValue() for r in (2, 3, 4)] == [20.0, 30.0, 40.0]
        data = json.loads(get_document_property(doc, "WriterAgentSpillRegistry", ""))
        assert list(data.keys()) == [f"{sheet_name}:1,0"]
        assert data[f"{sheet_name}:1,0"]["rows"] == 4

        # Two inserts in one debounce window with a prepare and queued write after each.
        sheet.getRows().insertByIndex(0, 1)
        st.debounce.cancel(f"sheet:{sheet_name}")
        grid_a = [[100], [200], [300], [400]]
        prep_a = prepare_auto_spill(
            ctx,
            doc,
            st,
            FormulaOrigin(sheet, sheet.getCellByPosition(0, 2), sheet_name, 2, 0),
            code,
            grid_a,
        )
        assert isinstance(prep_a, PreparedSpill)
        queue_deferred_spill_write(st, doc, prep_a, grid_a)
        sheet.getRows().insertByIndex(0, 1)
        st.debounce.cancel(f"sheet:{sheet_name}")
        grid_two = [[101], [202], [303], [404]]
        prep_b = prepare_auto_spill(
            ctx,
            doc,
            st,
            FormulaOrigin(sheet, sheet.getCellByPosition(0, 3), sheet_name, 3, 0),
            code,
            grid_two,
        )
        assert isinstance(prep_b, PreparedSpill)
        queue_deferred_spill_write(st, doc, prep_b, grid_two)
        st.debounce.flush_for_tests(f"spill:{sheet_name}:2:0")
        st.debounce.flush_for_tests(f"spill:{sheet_name}:3:0")
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)
        assert [sheet.getCellByPosition(0, r).getValue() for r in (4, 5, 6)] == [202.0, 303.0, 404.0]

        # Delete rows above: still respills and clears the shrunk tail (A5).
        sheet.getRows().removeByIndex(0, 2)
        st.debounce.cancel(f"sheet:{sheet_name}")
        grid_del = [[7], [8], [9]]
        prep_del = prepare_auto_spill(
            ctx,
            doc,
            st,
            FormulaOrigin(sheet, sheet.getCellByPosition(0, 1), sheet_name, 1, 0),
            code,
            grid_del,
        )
        assert isinstance(prep_del, PreparedSpill)
        queue_deferred_spill_write(st, doc, prep_del, grid_del)
        st.debounce.flush_for_tests(f"spill:{sheet_name}:1:0")
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)
        assert [sheet.getCellByPosition(0, r).getValue() for r in (2, 3)] == [8.0, 9.0]
        assert sheet.getCellByPosition(0, 4).getType() == EMPTY
    finally:
        WorkbookState.forget_key(key)


@native_test
@with_native_doc("calc")
def test_row_insert_pass_first_then_recalc_write_uno(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    code = "result = [1, 2, 3]"
    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code}")')

    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = st.key
    try:
        perform_deferred_spill(
            sheet_name,
            0,
            0,
            [[1], [2], [3]],
            doc=doc,
            sheet=sheet,
            code=code,
            key=key,
        )
        ensure_sheet_modify_listener(ctx, doc, sheet)
        saved_before = get_document_property(doc, "WriterAgentSpillRegistry", "")

        sheet.getRows().insertByIndex(0, 1)
        st.debounce.cancel(f"sheet:{sheet_name}")
        origin_cell = sheet.getCellByPosition(0, 1)
        grid4 = [[10], [20], [30], [40]]
        prepared = prepare_auto_spill(
            ctx,
            doc,
            st,
            FormulaOrigin(sheet, origin_cell, sheet_name, 1, 0),
            code,
            grid4,
        )
        assert isinstance(prepared, PreparedSpill)
        assert get_document_property(doc, "WriterAgentSpillRegistry", "") == saved_before

        queue_deferred_spill_write(st, doc, prepared, grid4)
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)
        assert (sheet_name, 0, 0) not in st.spills
        assert st.spills[(sheet_name, 1, 0)].rows == 3

        st.debounce.flush_for_tests(f"spill:{sheet_name}:1:0")
        assert sheet.getCellByPosition(0, 0).getType() == EMPTY
        assert [sheet.getCellByPosition(0, r).getValue() for r in (2, 3, 4)] == [20.0, 30.0, 40.0]
        data = json.loads(get_document_property(doc, "WriterAgentSpillRegistry", ""))
        assert list(data.keys()) == [f"{sheet_name}:1,0"]
        assert data[f"{sheet_name}:1,0"]["rows"] == 4
    finally:
        WorkbookState.forget_key(key)


@native_test
@with_native_doc("calc")
def test_adjacent_spills_survive_row_insert_recalc_uno(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    code_a = "result = [[1, 2]]"
    code_b = "result = [[3, 4, 5]]"
    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code_a}")')
    sheet.getCellByPosition(0, 1).setFormula(f'=PY("{code_b}")')

    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = st.key
    try:
        ensure_sheet_modify_listener(ctx, doc, sheet)
        perform_deferred_spill(sheet_name, 0, 0, [[1, 2]], doc=doc, sheet=sheet, code=code_a, key=key)
        perform_deferred_spill(sheet_name, 1, 0, [[3, 4, 5]], doc=doc, sheet=sheet, code=code_b, key=key)
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)

        sheet.getRows().insertByIndex(0, 1)
        st.debounce.cancel(f"sheet:{sheet_name}")
        prep_a = prepare_auto_spill(
            ctx, doc, st, FormulaOrigin(sheet, sheet.getCellByPosition(0, 1), sheet_name, 1, 0), code_a, [[10, 20]]
        )
        prep_b = prepare_auto_spill(
            ctx, doc, st, FormulaOrigin(sheet, sheet.getCellByPosition(0, 2), sheet_name, 2, 0), code_b, [[30, 40, 50]]
        )
        assert isinstance(prep_a, PreparedSpill)
        assert isinstance(prep_b, PreparedSpill)
        queue_deferred_spill_write(st, doc, prep_a, [[10, 20]])
        queue_deferred_spill_write(st, doc, prep_b, [[30, 40, 50]])
        st.debounce.flush_for_tests(f"spill:{sheet_name}:1:0")
        st.debounce.flush_for_tests(f"spill:{sheet_name}:2:0")
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)

        assert sheet.getCellByPosition(1, 0).getType() == EMPTY
        assert sheet.getCellByPosition(2, 0).getType() == EMPTY
        assert sheet.getCellByPosition(1, 1).getValue() == 20.0
        assert sheet.getCellByPosition(1, 2).getValue() == 40.0
        assert sheet.getCellByPosition(2, 2).getValue() == 50.0
        data = json.loads(get_document_property(doc, "WriterAgentSpillRegistry", ""))
        assert set(data.keys()) == {f"{sheet_name}:1,0", f"{sheet_name}:2,0"}
        assert data[f"{sheet_name}:1,0"]["cols"] == 2
        assert data[f"{sheet_name}:2,0"]["cols"] == 3
    finally:
        WorkbookState.forget_key(key)


@native_test
@with_native_doc("calc")
def test_cut_formula_does_not_take_over_user_cells_uno(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    code = "result = [1, 2, 3]"
    sheet.getCellByPosition(0, 4).setFormula(f'=PY("{code}")')

    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = st.key
    try:
        perform_deferred_spill(sheet_name, 4, 0, [[1], [2], [3]], doc=doc, sheet=sheet, code=code, key=key)
        ensure_sheet_modify_listener(ctx, doc, sheet)
        sheet.getCellByPosition(5, 9).setValue(1.0)  # F10
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)
        assert st.used_ends[sheet_name] == (9, 5)

        sheet.getCellByPosition(3, 5).setValue(500.0)  # D6
        sheet.moveRange(
            sheet.getCellByPosition(3, 4).getCellAddress(),
            sheet.getCellRangeByPosition(0, 4, 0, 4).getRangeAddress(),
        )
        res = prepare_auto_spill(
            ctx,
            doc,
            st,
            FormulaOrigin(sheet, sheet.getCellByPosition(3, 4), sheet_name, 4, 3),
            code,
            [[1], [2], [3]],
        )
        assert res == "#SPILL!"
        st.debounce.flush_for_tests(f"spill:{sheet_name}:4:3")
        assert sheet.getCellByPosition(0, 5).getValue() == 2.0
        assert sheet.getCellByPosition(0, 6).getValue() == 3.0
        assert sheet.getCellByPosition(3, 5).getValue() == 500.0
    finally:
        WorkbookState.forget_key(key)


@native_test
@with_native_doc("calc")
def test_growth_check_matches_cell_types_uno(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    code = "result = [1, 2, 3, 4]"
    origin_cell = sheet.getCellByPosition(0, 0)
    origin_cell.setFormula(f'=PY("{code}")')

    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = st.key
    try:
        sheet.getAnnotations().insertNew(sheet.getCellByPosition(0, 1).getCellAddress(), "note")
        sheet.getCellByPosition(0, 2).setPropertyValue("CellBackColor", 0xFF0000)

        origin = FormulaOrigin(sheet, origin_cell, sheet_name, 0, 0)
        assert isinstance(prepare_auto_spill(ctx, doc, st, origin, code, [[1], [2], [3]]), PreparedSpill)

        sheet.getCellByPosition(0, 3).setFormula('=""')
        assert prepare_auto_spill(ctx, doc, st, origin, code, [[1], [2], [3], [4]]) == "#SPILL!"
    finally:
        WorkbookState.forget_key(key)


@native_test
@with_native_doc("calc")
def test_paste_with_blank_corner_keeps_pasted_table_uno(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    sheet.getCellByPosition(9, 30).setValue(1.0)  # J31 anchor keeps delta == (0, 0)

    code = "result = [[1, 2], [3, 4], [5, 6]]"
    sheet.getCellByPosition(0, 4).setFormula(f'=PY("{code}")')
    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = st.key
    try:
        ensure_sheet_modify_listener(ctx, doc, sheet)
        perform_deferred_spill(
            sheet_name,
            4,
            0,
            [[1, 2], [3, 4], [5, 6]],
            doc=doc,
            sheet=sheet,
            code=code,
            key=key,
        )
        st.used_ends[sheet_name] = (30, 9)

        # Source table at D5:E7 with blank top-left D5.
        sheet.getCellByPosition(4, 4).setString("Jan")
        sheet.getCellByPosition(3, 5).setString("Ann")
        sheet.getCellByPosition(4, 5).setValue(10.0)
        sheet.getCellByPosition(3, 6).setString("Bob")
        sheet.getCellByPosition(4, 6).setValue(20.0)

        sheet.copyRange(
            sheet.getCellByPosition(0, 4).getCellAddress(),
            sheet.getCellRangeByPosition(3, 4, 4, 6).getRangeAddress(),
        )
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)

        assert (sheet_name, 4, 0) not in st.spills
        assert sheet.getCellByPosition(0, 4).getType() == EMPTY
        assert sheet.getCellByPosition(1, 4).getString() == "Jan"
        assert sheet.getCellByPosition(0, 5).getString() == "Ann"
        assert sheet.getCellByPosition(1, 5).getValue() == 10.0
        assert sheet.getCellByPosition(0, 6).getString() == "Bob"
        assert sheet.getCellByPosition(1, 6).getValue() == 20.0
    finally:
        WorkbookState.forget_key(key)


@native_test
@with_native_doc("calc")
def test_delete_cells_up_keeps_pulled_values_uno(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    sheet.getCellByPosition(9, 30).setValue(1.0)

    code = "result = [[1, 2, 3]]"
    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = st.key
    try:
        ensure_sheet_modify_listener(ctx, doc, sheet)

        # Case 1: Delete Cells UP pulls empty A6 and user B6:C6=(100, 200) into row 5.
        sheet.getCellByPosition(0, 4).setFormula(f'=PY("{code}")')
        perform_deferred_spill(
            sheet_name, 4, 0, [[1, 2, 3]], doc=doc, sheet=sheet, code=code, key=key
        )
        sheet.getCellByPosition(1, 5).setValue(100.0)
        sheet.getCellByPosition(2, 5).setValue(200.0)
        st.used_ends[sheet_name] = (30, 9)

        sheet.removeRange(sheet.getCellRangeByPosition(0, 4, 2, 4).getRangeAddress(), UP)
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)

        assert (sheet_name, 4, 0) not in st.spills
        assert sheet.getCellByPosition(0, 4).getType() == EMPTY
        assert sheet.getCellByPosition(1, 4).getValue() == 100.0
        assert sheet.getCellByPosition(2, 4).getValue() == 200.0

        # Case 2: Delete Cells UP pulls =PY("other") into A5 and user B6:C6=(300, 400) into row 5.
        sheet.getCellByPosition(0, 4).setFormula(f'=PY("{code}")')
        perform_deferred_spill(
            sheet_name, 4, 0, [[1, 2, 3]], doc=doc, sheet=sheet, code=code, key=key
        )
        sheet.getCellByPosition(0, 5).setFormula('=PY("other")')
        sheet.getCellByPosition(1, 5).setValue(300.0)
        sheet.getCellByPosition(2, 5).setValue(400.0)
        st.used_ends[sheet_name] = (30, 9)

        sheet.removeRange(sheet.getCellRangeByPosition(0, 4, 2, 4).getRangeAddress(), UP)
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)

        assert (sheet_name, 4, 0) not in st.spills
        assert is_matching_py_formula(sheet.getCellByPosition(0, 4).getFormula(), "other")
        assert sheet.getCellByPosition(1, 4).getValue() == 300.0
        assert sheet.getCellByPosition(2, 4).getValue() == 400.0
    finally:
        WorkbookState.forget_key(key)


@native_test
@with_native_doc("calc")
def test_delete_cells_up_then_respill_keeps_pulled_values_uno(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    sheet.getCellByPosition(9, 30).setValue(1.0)

    code = "result = [[1, 2, 3]]"
    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = st.key
    try:
        ensure_sheet_modify_listener(ctx, doc, sheet)
        sheet.getCellByPosition(0, 4).setFormula(f'=PY("{code}")')
        perform_deferred_spill(
            sheet_name, 4, 0, [[1, 2, 3]], doc=doc, sheet=sheet, code=code, key=key
        )
        sheet.getCellByPosition(0, 5).setFormula('=PY("other")')
        sheet.getCellByPosition(1, 5).setValue(100.0)
        sheet.getCellByPosition(2, 5).setValue(200.0)
        st.used_ends[sheet_name] = (30, 9)

        sheet.removeRange(sheet.getCellRangeByPosition(0, 4, 2, 4).getRangeAddress(), UP)
        # Respill runs before the modify pass.
        perform_deferred_spill(
            sheet_name, 4, 0, [[7, 8, 9]], doc=doc, sheet=sheet, code="other", key=key
        )
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)

        assert (sheet_name, 4, 0) not in st.spills
        assert sheet.getCellByPosition(1, 4).getValue() == 100.0
        assert sheet.getCellByPosition(2, 4).getValue() == 200.0
    finally:
        WorkbookState.forget_key(key)


@native_test
@with_native_doc("calc")
def test_row_insert_respill_of_neighbour_formula_keeps_user_cells_uno(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = st.key
    try:
        ensure_sheet_modify_listener(ctx, doc, sheet)
        # x at A5 (row 4) has spilled; y at A4 (row 3) is blocked by user data in B4:C4.
        sheet.getCellByPosition(0, 4).setFormula('=PY("x")')
        perform_deferred_spill(
            sheet_name, 4, 0, [[1, 2, 3]], doc=doc, sheet=sheet, code="x", key=key
        )
        sheet.getCellByPosition(0, 3).setFormula('=PY("y")')
        sheet.getCellByPosition(1, 3).setValue(100.0)
        sheet.getCellByPosition(2, 3).setValue(200.0)
        st.used_ends[sheet_name] = (4, 2)

        # Insert row above row 4: y shifts to A5 (row 4), x shifts to A6 (row 5).
        sheet.getRows().insertByIndex(3, 1)
        perform_deferred_spill(
            sheet_name, 4, 0, [[7, 8, 9]], doc=doc, sheet=sheet, code="y", key=key
        )
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)

        assert (sheet_name, 4, 0) not in st.spills
        assert (sheet_name, 5, 0) in st.spills
        assert st.spills[(sheet_name, 5, 0)].code == "x"
        assert sheet.getCellByPosition(1, 4).getValue() == 100.0
        assert sheet.getCellByPosition(2, 4).getValue() == 200.0
    finally:
        WorkbookState.forget_key(key)


@native_test
@with_native_doc("calc")
def test_constant_or_formula_over_origin_leaves_block_uno(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    sheet.getCellByPosition(9, 30).setValue(1.0)
    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = st.key
    try:
        ensure_sheet_modify_listener(ctx, doc, sheet)
        for replacement in ("42", "=SUM(1)"):
            code = f"result = [1, 2, {replacement!r}]"
            sheet.getCellByPosition(0, 4).setFormula(f'=PY("{code}")')
            perform_deferred_spill(
                sheet_name, 4, 0, [[1], [2], [3]], doc=doc, sheet=sheet, code=code, key=key
            )
            st.used_ends[sheet_name] = (30, 9)

            if replacement == "42":
                sheet.getCellByPosition(0, 4).setValue(42.0)
            else:
                sheet.getCellByPosition(0, 4).setFormula(replacement)
            flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)

            assert (sheet_name, 4, 0) not in st.spills
            assert sheet.getCellByPosition(0, 5).getValue() == 2.0
            assert sheet.getCellByPosition(0, 6).getValue() == 3.0
    finally:
        WorkbookState.forget_key(key)


@native_test
@with_native_doc("calc")
def test_emptied_or_edited_formula_still_clears_block_uno(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    sheet_name = str(sheet.getName() or "Sheet1")
    sheet.getCellByPosition(9, 30).setValue(1.0)
    st = WorkbookState.for_doc(doc)
    assert st is not None
    key = st.key
    try:
        ensure_sheet_modify_listener(ctx, doc, sheet)

        # 1. Empty origin after an earlier edited neighbour -> clears entire block.
        sheet.getCellByPosition(0, 4).setFormula('=PY("a")')
        perform_deferred_spill(sheet_name, 4, 0, [[1], [2], [3]], doc=doc, sheet=sheet, code="a", key=key)
        st.used_ends[sheet_name] = (30, 9)
        sheet.getCellByPosition(0, 5).setValue(50.0)
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)
        sheet.getCellByPosition(0, 4).setFormula("")
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)
        assert (sheet_name, 4, 0) not in st.spills
        assert sheet.getCellByPosition(0, 5).getType() == EMPTY
        assert sheet.getCellByPosition(0, 6).getType() == EMPTY

        # 2. Code edit with pass first, after an earlier edited neighbour.
        sheet.getCellByPosition(0, 4).setFormula('=PY("b1")')
        perform_deferred_spill(sheet_name, 4, 0, [[1], [2], [3]], doc=doc, sheet=sheet, code="b1", key=key)
        sheet.getCellByPosition(0, 5).setValue(60.0)
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)
        sheet.getCellByPosition(0, 4).setFormula('=PY("b2")')
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)
        assert sheet.getCellByPosition(0, 6).getType() == EMPTY
        perform_deferred_spill(sheet_name, 4, 0, [[10], [20]], doc=doc, sheet=sheet, code="b2", key=key)
        assert sheet.getCellByPosition(0, 5).getValue() == 20.0
        assert sheet.getCellByPosition(0, 6).getType() == EMPTY

        # 3. Code edit with write first, after an earlier edited neighbour.
        sheet.getCellByPosition(0, 4).setFormula('=PY("c1")')
        perform_deferred_spill(sheet_name, 4, 0, [[1], [2], [3]], doc=doc, sheet=sheet, code="c1", key=key)
        sheet.getCellByPosition(0, 5).setValue(70.0)
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)
        sheet.getCellByPosition(0, 4).setFormula('=PY("c2")')
        perform_deferred_spill(sheet_name, 4, 0, [[11], [22]], doc=doc, sheet=sheet, code="c2", key=key)
        flush_sheet_modify_pass_for_tests(ctx, doc, sheet, sheet_name=sheet_name)
        assert sheet.getCellByPosition(0, 5).getValue() == 22.0
        assert sheet.getCellByPosition(0, 6).getType() == EMPTY
    finally:
        WorkbookState.forget_key(key)
