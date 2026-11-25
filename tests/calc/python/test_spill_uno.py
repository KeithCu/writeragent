# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for Calc auto-spill behavior."""

from __future__ import annotations

import json

from com.sun.star.table.CellContentType import EMPTY

from plugin.calc.python.formula_locator_cache import FormulaOrigin
from plugin.calc.python.spill import perform_deferred_spill, prepare_auto_spill
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
