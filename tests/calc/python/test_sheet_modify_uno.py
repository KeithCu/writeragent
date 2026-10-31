# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for sheet modify dispatcher and rename migration."""

from __future__ import annotations

from types import SimpleNamespace

from plugin.calc.python.function import SHEET_MODIFY_LISTENERS, _spill_registry_doc_key, perform_deferred_spill
from plugin.calc.python.sheet_modify import (
    dispatch_sheet_modified,
    ensure_sheet_modify_listener,
    flush_sheet_modify_pass_for_tests,
)
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_sheet_rename_migrates_modify_listener_and_spill(ctx, doc):
    sheets = doc.getSheets()
    sheet = sheets.getByIndex(0)
    original_name = str(sheet.getName() or "Sheet1")
    new_name = "RenamedSheet"

    # 1. Register modify listener
    listener = ensure_sheet_modify_listener(ctx, doc, sheet)
    assert listener is not None

    # 2. Enter formula in A1 (col 0, row 0) and spill 2x2
    code = "result = [[1, 2], [3, 4]]"
    sheet.getCellByPosition(0, 0).setFormula(f'=PY("{code}")')
    # Pass the spill registry key (RuntimeUID), like the real caller. A pooled doc can
    # be a saved file, and its URL would not match, so the spill would be skipped.
    perform_deferred_spill(_spill_registry_doc_key(doc), original_name, 0, 0, [[1, 2], [3, 4]], doc=doc, sheet=sheet, code=code)

    assert sheet.getCellByPosition(1, 0).getValue() == 2.0
    assert sheet.getCellByPosition(0, 1).getValue() == 3.0
    assert sheet.getCellByPosition(1, 1).getValue() == 4.0

    # 3. Rename the sheet
    sheet.setName(new_name)

    # 4. Trigger modify event on the renamed sheet
    dispatch_sheet_modified(ctx, doc.getURL(), original_name, SimpleNamespace(Source=sheet), doc=doc, dispatcher=listener)

    # Assert SHEET_MODIFY_LISTENERS has entry under new name and old name is gone
    entries_for_doc = [k for k in SHEET_MODIFY_LISTENERS if k[1] in (original_name, new_name)]
    assert any(k[1] == new_name for k in entries_for_doc)
    assert not any(k[1] == original_name for k in entries_for_doc)

    # 5. Clear the formula cell in A1
    sheet.getCellByPosition(0, 0).setFormula("")
    sheet.getCellByPosition(0, 0).setString("")

    # Flush modify pass to run spill orphan cleanup
    flush_sheet_modify_pass_for_tests(ctx, doc, sheet, doc_url=doc.getURL(), sheet_name=new_name)

    # 6. Spilled neighbors should be cleared
    assert sheet.getCellByPosition(1, 0).getString() == "" or sheet.getCellByPosition(1, 0).getValue() == 0.0
    assert sheet.getCellByPosition(0, 1).getString() == "" or sheet.getCellByPosition(0, 1).getValue() == 0.0
    assert sheet.getCellByPosition(1, 1).getString() == "" or sheet.getCellByPosition(1, 1).getValue() == 0.0
