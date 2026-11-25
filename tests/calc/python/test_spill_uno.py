# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for Calc auto-spill behavior."""

from __future__ import annotations

import json

from com.sun.star.table.CellContentType import EMPTY

from plugin.calc.python.formula_locator_cache import FormulaOrigin
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
    for write_first in (True, False):
        sheet = doc.getSheets().getByIndex(0)
        sheet_name = str(sheet.getName() or "Sheet1")
        # Reset sheet contents between sub-runs.
        sheet.getCellRangeByPosition(0, 0, 5, 20).clearContents(1 | 2 | 4 | 16)

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
            saved_before = get_document_property(doc, "WriterAgentSpillRegistry", "")

            sheet.getRows().insertByIndex(0, 1)
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
            tokens = (
                (f"spill:{sheet_name}:1:0", f"sheet:{sheet_name}")
                if write_first
                else (f"sheet:{sheet_name}", f"spill:{sheet_name}:1:0")
            )
            for tok in tokens:
                st.debounce.flush_for_tests(tok)

            assert sheet.getCellByPosition(0, 0).getType() == EMPTY
            assert [sheet.getCellByPosition(0, r).getValue() for r in (2, 3, 4)] == [20.0, 30.0, 40.0]
            data = json.loads(get_document_property(doc, "WriterAgentSpillRegistry", ""))
            assert list(data.keys()) == [f"{sheet_name}:1,0"]
            assert data[f"{sheet_name}:1,0"]["rows"] == 4

            # Two inserts in one debounce window with a prepare after each.
            sheet.getRows().insertByIndex(0, 1)
            prep_a = prepare_auto_spill(
                ctx,
                doc,
                st,
                FormulaOrigin(sheet, sheet.getCellByPosition(0, 2), sheet_name, 2, 0),
                code,
                [[100], [200], [300], [400]],
            )
            assert isinstance(prep_a, PreparedSpill)
            sheet.getRows().insertByIndex(0, 1)
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
            st.debounce.flush_for_tests(f"spill:{sheet_name}:3:0")
            st.debounce.flush_for_tests(f"sheet:{sheet_name}")
            assert [sheet.getCellByPosition(0, r).getValue() for r in (4, 5, 6)] == [202.0, 303.0, 404.0]

            # Delete rows above: still respills.
            sheet.getRows().removeByIndex(0, 2)
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
            st.debounce.flush_for_tests(f"sheet:{sheet_name}")
            assert [sheet.getCellByPosition(0, r).getValue() for r in (2, 3)] == [8.0, 9.0]
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
        perform_deferred_spill(sheet_name, 0, 0, [[1, 2]], doc=doc, sheet=sheet, code=code_a, key=key)
        perform_deferred_spill(sheet_name, 1, 0, [[3, 4, 5]], doc=doc, sheet=sheet, code=code_b, key=key)
        # Typing A2 leaves a pending modify pass; flush it before inserting.
        st.debounce.flush_for_tests(f"sheet:{sheet_name}")

        sheet.getRows().insertByIndex(0, 1)
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
        st.debounce.flush_for_tests(f"sheet:{sheet_name}")

        assert sheet.getCellByPosition(1, 1).getValue() == 20.0
        assert sheet.getCellByPosition(1, 2).getValue() == 40.0
        assert sheet.getCellByPosition(2, 2).getValue() == 50.0
        data = json.loads(get_document_property(doc, "WriterAgentSpillRegistry", ""))
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
        sheet.getCellByPosition(5, 9).setValue(1.0)  # F10
        st.debounce.flush_for_tests(f"sheet:{sheet_name}")

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
