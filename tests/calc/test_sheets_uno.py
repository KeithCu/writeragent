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
def test_create_sheet(ctx, doc):
    res = _execute_calc_tool(doc, ctx, "create_sheet", {"sheet": "NewSheet"})
    assert res.get("status") == "ok", f"create_sheet failed: {res}"
    assert "no cells copied" in res.get("message", "")
    assert doc.getSheets().hasByName("NewSheet"), "Sheet not created"


@native_test
@with_native_doc("calc")
def test_rename_duplicate_sheet(ctx, doc):
    _execute_calc_tool(doc, ctx, "create_sheet", {"sheet": "SheetA"})
    _execute_calc_tool(doc, ctx, "create_sheet", {"sheet": "SheetB"})

    # Attempt to rename SheetB to SheetA
    res_dup = _execute_calc_tool(doc, ctx, "rename_sheet", {"old_name": "SheetB", "new_name": "SheetA"})
    assert res_dup.get("status") == "error"
    assert len(res_dup.get("message", "")) > 0


@native_test
@with_native_doc("calc")
def test_add_row_and_column(ctx, doc):
    active_sheet = doc.getCurrentController().getActiveSheet()
    _execute_calc_tool(doc, ctx, "add_row", {"sheet_name": active_sheet.getName(), "row_index": 1, "count": 1})
    _execute_calc_tool(doc, ctx, "add_column", {"sheet_name": active_sheet.getName(), "col_index": 1, "count": 1})
    # we just test it didn't crash for now
