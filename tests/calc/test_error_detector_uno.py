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


@native_test
@with_native_doc("calc")
def test_detect_and_explain_errors(ctx, doc):
    from plugin.calc.errors import DetectErrors
    from plugin.framework.tool import ToolContext

    active_sheet = doc.getCurrentController().getActiveSheet()

    # Test #DIV/0!
    active_sheet.getCellByPosition(8, 0).setFormula("=1/0")
    tctx = ToolContext(doc, ctx, "calc", {}, "test")
    res = DetectErrors().execute(tctx, range="I1")

    assert res.get("status") == "ok", f"error_detect failed: {res}"
    assert res.get("result", {}).get("error_count", 0) > 0, "No errors detected"
    errors = res.get("result", {}).get("errors", [])
    err0 = errors[0].get("error", {}) if errors else {}
    assert err0.get("code") == "#DIV/0!", f"Expected #DIV/0!, got: {errors}"

    # Test #NAME?
    active_sheet.getCellByPosition(9, 0).setFormula("=UNKNOWN_NAME()")
    res2 = DetectErrors().execute(tctx, range="J1")
    assert res2.get("status") == "ok", f"error_detect #NAME? failed: {res2}"
    assert res2.get("result", {}).get("error_count", 0) > 0, "No errors detected"
    errors = res2.get("result", {}).get("errors", [])
    err0 = errors[0].get("error", {}) if errors else {}
    assert err0.get("code") == "#NAME?", f"Expected #NAME?, got: {errors}"

    # Test #REF!
    active_sheet.getCellByPosition(10, 0).setFormula("=#REF!")
    res3 = DetectErrors().execute(tctx, range="K1")
    assert res3.get("status") == "ok", f"error_detect #REF! failed: {res3}"
    assert res3.get("result", {}).get("error_count", 0) > 0, "No #REF! errors detected"
    errors = res3.get("result", {}).get("errors", [])
    err0 = errors[0].get("error", {}) if errors else {}
    assert err0.get("code") == "#REF!", f"Expected #REF!, got: {errors}"
    assert "#REF!" in errors[0].get("suggestion", ""), f"Suggestion does not mention #REF!: {errors[0].get('suggestion')}"
    # FormulaDepChain / formula_query enrichment (optional on older LO builds)
    assert "dependency_chain" in errors[0] or "precedents" in errors[0]
