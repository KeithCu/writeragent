# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for error_detect error.code matching Calc's displayed cell string."""

from plugin.calc.address_utils import parse_address, split_sheet_prefix
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import execute_calc_tool, with_native_doc


@native_test
@with_native_doc("calc")
def test_error_detect_code_matches_calc_cell_string(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    formulas = ("=1/0", "=SQRT(-1)", "=1+", "=NA()", "=A1:A2!B1:B2")
    for row_idx, formula in enumerate(formulas):
        sheet.getCellByPosition(0, row_idx).setFormula(formula)
    doc.calculateAll()

    res = execute_calc_tool(doc, ctx, "error_detect", {"range": "A1:A5"})
    assert res.get("status") == "ok", f"error_detect failed: {res}"
    errors = res["result"]["errors"]
    assert len(errors) == len(formulas)

    for item in errors:
        _sheet_name, bare = split_sheet_prefix(item["address"])
        col, row = parse_address(bare)
        displayed = sheet.getCellByPosition(col, row).getString()
        assert item["error"]["code"] == displayed
