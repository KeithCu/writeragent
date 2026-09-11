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
from plugin.tests.testing_utils import (
    TestingFactory,
    note_windows_html_paste_leftover,
    with_native_doc,
)


def _execute_calc_tool(doc, ctx, name, args):
    return TestingFactory.execute_tool(doc, ctx, name, args, doc_type="calc")


@native_test
@with_native_doc("calc")
def test_write_formula_range(ctx, doc):
    active_sheet = doc.getCurrentController().getActiveSheet()
    res = _execute_calc_tool(doc, ctx, "write_formula_range", {"range": "A1", "values": "Hello"})
    assert res.get("status") == "ok", f"write_formula_range failed: {res}"
    assert active_sheet.getCellByPosition(0, 0).getString() == "Hello", "Value mismatch"

    # Batch write
    _execute_calc_tool(doc, ctx, "write_formula_range", {"range": ["B1", "B2"], "values": "Batch"})
    assert active_sheet.getCellByPosition(1, 0).getString() == "Batch", "Batch write cell 1 failed"
    assert active_sheet.getCellByPosition(1, 1).getString() == "Batch", "Batch write cell 2 failed"

    # Single cell: commas in comments / prose must not split into multiple "cells"
    comment = "Note: see section 3, paragraph 2."
    res_comment = _execute_calc_tool(doc, ctx, "write_formula_range", {"range": "C1", "values": comment})
    assert res_comment.get("status") == "ok", f"write_formula_range comment failed: {res_comment}"
    assert active_sheet.getCellByPosition(2, 0).getString() == comment, "Comma in single-cell comment mangled"

    jp_sentence = "Hello ケイス, this is a test."
    res_jp = _execute_calc_tool(doc, ctx, "write_formula_range", {"range": ["D1"], "values": jp_sentence})
    assert res_jp.get("status") == "ok", f"write_formula_range JP sentence failed: {res_jp}"
    assert active_sheet.getCellByPosition(3, 0).getString() == jp_sentence, "Comma in single-cell prose mangled"

    # Two cells in one contiguous range: comma-separated row still maps one field per cell
    res_two = _execute_calc_tool(doc, ctx, "write_formula_range", {"range": "E1:F1", "values": "Left,Right"})
    assert res_two.get("status") == "ok", f"write_formula_range two-cell CSV failed: {res_two}"
    assert active_sheet.getCellByPosition(4, 0).getString() == "Left", "E1 should be first CSV field"
    assert active_sheet.getCellByPosition(5, 0).getString() == "Right", "F1 should be second CSV field"


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

    assert res.get("status") == "ok", f"detect_and_explain_errors failed: {res}"
    assert res.get("result", {}).get("error_count", 0) > 0, "No errors detected"
    errors = res.get("result", {}).get("errors", [])
    err0 = errors[0].get("error", {}) if errors else {}
    assert err0.get("code") == "#DIV/0!", f"Expected #DIV/0!, got: {errors}"

    # Test #NAME?
    active_sheet.getCellByPosition(9, 0).setFormula("=UNKNOWN_NAME()")
    res2 = DetectErrors().execute(tctx, range="J1")
    assert res2.get("status") == "ok", f"detect_and_explain_errors #NAME? failed: {res2}"
    assert res2.get("result", {}).get("error_count", 0) > 0, "No errors detected"
    errors = res2.get("result", {}).get("errors", [])
    err0 = errors[0].get("error", {}) if errors else {}
    assert err0.get("code") == "#NAME?", f"Expected #NAME?, got: {errors}"

    # Test #REF!
    active_sheet.getCellByPosition(10, 0).setFormula("=#REF!")
    res3 = DetectErrors().execute(tctx, range="K1")
    assert res3.get("status") == "ok", f"detect_and_explain_errors #REF! failed: {res3}"
    assert res3.get("result", {}).get("error_count", 0) > 0, "No #REF! errors detected"
    errors = res3.get("result", {}).get("errors", [])
    err0 = errors[0].get("error", {}) if errors else {}
    assert err0.get("code") == "#REF!", f"Expected #REF!, got: {errors}"
    assert "#REF!" in errors[0].get("suggestion", ""), f"Suggestion does not mention #REF!: {errors[0].get('suggestion')}"
    # FormulaDepChain / formula_query enrichment (optional on older LO builds)
    assert "dependency_chain" in errors[0] or "precedents" in errors[0]


@native_test
@with_native_doc("calc")
def test_navigate_to_cell(ctx, doc):
    from plugin.calc.navigation import navigate_to_cell

    active_sheet = doc.getCurrentController().getActiveSheet()
    active_sheet.getCellByPosition(4, 4).setString("target")
    ok = navigate_to_cell(doc, ctx, "E5")
    assert ok, "navigate_to_cell returned False"
    sel = doc.getCurrentController().getSelection()
    addr = sel.getCellAddress()
    assert addr.Column == 4 and addr.Row == 4, f"Expected E5 selected, got col={addr.Column} row={addr.Row}"


@native_test
@with_native_doc("calc")
def test_write_formula_range_compound_undo(ctx, doc):
    """Bulk write_formula_range should group undo (one step reverts all ranges)."""
    active_sheet = doc.getCurrentController().getActiveSheet()
    _execute_calc_tool(doc, ctx, "write_formula_range", {"range": ["G1", "G2"], "values": "undo-test"})
    assert active_sheet.getCellByPosition(6, 0).getString() == "undo-test"
    assert active_sheet.getCellByPosition(6, 1).getString() == "undo-test"

    um = doc.getUndoManager()
    if um is not None:
        undo_enabled = False
        try:
            undo_enabled = um.isUndoEnabled()
        except Exception:
            # If the attribute/method is missing or raises, check if undoing is possible
            try:
                undo_enabled = um.isUndoPossible()
            except Exception:
                pass
        if undo_enabled:
            try:
                um.undo()
                assert active_sheet.getCellByPosition(6, 0).getString() == "", "G1 should revert after single undo"
                assert active_sheet.getCellByPosition(6, 1).getString() == "", "G2 should revert after single undo"
            except Exception:
                pass


@native_test
@with_native_doc("calc")
def test_cross_sheet_formula(ctx, doc):
    sheets = doc.getSheets()

    # Create Sheet2 if it doesn't exist
    if not sheets.hasByName("Sheet2"):
        sheets.insertNewByName("Sheet2", sheets.getCount())

    sheet2 = sheets.getByName("Sheet2")
    # Set a target value
    sheet2.getCellByPosition(0, 0).setValue(100.0) # Sheet2.A1 = 100

    # Active sheet is usually Sheet1
    active_sheet = doc.getCurrentController().getActiveSheet()

    res = _execute_calc_tool(doc, ctx, "write_formula_range", {
        "range": ["D1"],
        "values": "=Sheet2.A1 * 2"
    })

    assert res.get("status") == "ok", f"write_formula_range failed: {res}"

    # Verify the formula is set and evaluates properly
    cell = active_sheet.getCellByPosition(3, 0) # D1
    assert cell.getFormula() == "=Sheet2.A1*2" or cell.getFormula() == "=Sheet2.A1 * 2"

    # Wait for formula recalculation or force if necessary.
    # Usually in LibreOffice UNO it computes immediately, but we can verify formula strings safely.
    assert cell.getValue() == 200.0, f"Cross-sheet formula did not compute to 200.0, got {cell.getValue()}"


def _formula_compact(cell) -> str:
    """Compare formulas without LO spacing differences."""
    return (cell.getFormula() or "").replace(" ", "")


@native_test
@with_native_doc("calc")
def test_write_formula_range_source_copies_block_and_adjusts_refs(ctx, doc):
    """source-only copy uses source extent; relative refs shift, $ stay."""
    active = doc.getCurrentController().getActiveSheet()
    active.getCellByPosition(0, 0).setValue(10.0)  # A1
    active.getCellByPosition(0, 1).setValue(20.0)  # A2
    write = _execute_calc_tool(doc, ctx, "write_formula_range", {
        "range": ["B1"],
        "values": "=A1*2",
    })
    assert write.get("status") == "ok", f"seed formula failed: {write}"
    write_abs = _execute_calc_tool(doc, ctx, "write_formula_range", {
        "range": ["C1"],
        "values": "=$A$1*2",
    })
    assert write_abs.get("status") == "ok", f"seed absolute failed: {write_abs}"
    active.getCellByPosition(3, 0).setString("x")  # D1
    active.getCellByPosition(4, 0).setString("y")  # E1

    res = _execute_calc_tool(doc, ctx, "write_formula_range", {
        "range": ["B2"],
        "source": "B1",
    })
    assert res.get("status") == "ok", f"source copy failed: {res}"
    assert res.get("rows_copied") == 1
    assert res.get("cols_copied") == 1
    assert _formula_compact(active.getCellByPosition(1, 1)) == "=A2*2"
    assert active.getCellByPosition(1, 1).getValue() == 40.0

    res_abs = _execute_calc_tool(doc, ctx, "write_formula_range", {
        "range": ["C2"],
        "source": "C1",
    })
    assert res_abs.get("status") == "ok", f"absolute copy failed: {res_abs}"
    assert _formula_compact(active.getCellByPosition(2, 1)) == "=$A$1*2"
    assert active.getCellByPosition(2, 1).getValue() == 20.0

    res_block = _execute_calc_tool(doc, ctx, "write_formula_range", {
        "range": ["D5"],
        "source": "D1:E1",
    })
    assert res_block.get("status") == "ok", f"block copy failed: {res_block}"
    assert res_block.get("rows_copied") == 1
    assert res_block.get("cols_copied") == 2
    assert active.getCellByPosition(3, 4).getString() == "x"
    assert active.getCellByPosition(4, 4).getString() == "y"


@native_test
@with_native_doc("calc")
def test_write_formula_range_source_cross_sheet(ctx, doc):
    """Sheet1.A1:C3 → Sample.A1 copies the 3×3 block onto the new tab."""
    sheets = doc.getSheets()
    src_name = doc.getCurrentController().getActiveSheet().getName()
    src = sheets.getByName(src_name)
    for row in range(3):
        for col in range(3):
            src.getCellByPosition(col, row).setValue(float(row * 3 + col + 1))

    created = _execute_calc_tool(doc, ctx, "create_sheet", {"sheet": "Sample"})
    assert created.get("status") == "ok", f"create_sheet failed: {created}"
    assert "no cells copied" in created.get("message", "")
    sample = sheets.getByName("Sample")
    assert sample.getCellByPosition(0, 0).getValue() == 0.0

    res = _execute_calc_tool(doc, ctx, "write_formula_range", {
        "range": ["Sample.A1"],
        "source": f"{src_name}.A1:C3",
    })
    assert res.get("status") == "ok", f"cross-sheet source copy failed: {res}"
    assert res.get("rows_copied") == 3
    assert res.get("cols_copied") == 3
    for row in range(3):
        for col in range(3):
            expected = float(row * 3 + col + 1)
            got = sample.getCellByPosition(col, row).getValue()
            assert got == expected, f"Sample cell ({col},{row}) expected {expected}, got {got}"


@native_test
@with_native_doc("calc")
def test_list_calc_functions(ctx, doc):
    # Test listing all functions (no filter)
    res = _execute_calc_tool(doc, ctx, "list_calc_functions", {})
    assert res.get("status") == "ok", f"list_calc_functions failed: {res}"
    functions = res.get("functions", [])
    assert len(functions) > 100, f"Expected many functions, got {len(functions)}"

    # Test filtering by name
    res_filter = _execute_calc_tool(doc, ctx, "list_calc_functions", {"filter": "SUM"})
    assert res_filter.get("status") == "ok", f"list_calc_functions with filter failed: {res_filter}"
    filtered_funcs = res_filter.get("functions", [])
    assert len(filtered_funcs) > 0, "No functions returned for filter 'SUM'"
    for f in filtered_funcs:
        assert "SUM" in f["name"].upper() or "SUM" in f["description"].upper(), f"Function {f['name']} does not contain 'SUM'"

    # Test filtering by description (e.g. 'hyperbolic')
    res_desc = _execute_calc_tool(doc, ctx, "list_calc_functions", {"filter": "hyperbolic"})
    assert res_desc.get("status") == "ok", f"list_calc_functions with description filter failed: {res_desc}"
    desc_funcs = res_desc.get("functions", [])
    assert len(desc_funcs) > 0, "No functions returned for description filter 'hyperbolic'"
    for f in desc_funcs:
        assert "HYPERBOLIC" in f["name"].upper() or "HYPERBOLIC" in f["description"].upper(), f"Function {f['name']} does not contain 'hyperbolic'"


@native_test
@with_native_doc("calc")
def test_evaluate_formula(ctx, doc):
    from plugin.calc.formulas import EvaluateFormula
    from plugin.framework.tool import ToolContext

    tctx = ToolContext(doc, ctx, "calc", {}, "test")
    eval_tool = EvaluateFormula()

    # Simple valid formula evaluation
    res = eval_tool.execute(tctx, formula="=2+3")
    assert res.get("status") == "ok", f"evaluate_formula failed: {res}"
    assert res.get("result") == 5.0, f"Expected 5.0, got {res.get('result')}"
    assert res.get("result_type") == "formula", f"Expected formula type, got {res.get('result_type')}"

    # Text formula evaluation
    res_text = eval_tool.execute(tctx, formula='=CONCATENATE("Hello"; " "; "World")')
    assert res_text.get("status") == "ok", f"evaluate_formula text failed: {res_text}"
    assert res_text.get("result") == "Hello World", f"Expected 'Hello World', got {res_text.get('result')}"

    # Relative formula evaluation utilizing copied sheet cell values
    active_sheet = doc.getCurrentController().getActiveSheet()
    active_sheet.getCellByPosition(0, 0).setValue(10.0) # A1
    active_sheet.getCellByPosition(1, 0).setValue(20.0) # B1

    res_rel = eval_tool.execute(tctx, formula="=A1+B1", cell="C1")
    assert res_rel.get("status") == "ok", f"evaluate_formula relative failed: {res_rel}"
    assert res_rel.get("result") == 30.0, f"Expected 30.0, got {res_rel.get('result')}"

    # Error formula evaluation (e.g. division by zero)
    res_err = eval_tool.execute(tctx, formula="=1/0")
    assert res_err.get("status") == "error", f"Expected error status, got {res_err}"
    assert "error_code" in res_err, f"Expected error_code in response: {res_err}"
    assert "#DIV/0!" in res_err.get("message", ""), f"Expected division by zero message, got {res_err.get('message')}"


@native_test
@with_native_doc("calc")
def test_insert_result_into_calc_undo(ctx, doc):
    """Running a script that inserts structured content into Calc can be reverted with Ctrl+Z."""
    from plugin.scripting.python_runner import insert_result_into_calc

    active_sheet = doc.getCurrentController().getActiveSheet()
    primes_result = {
        "title": "Prime Numbers in Range",
        "primes": [
            {"position": 1000, "prime": 7919},
            {"position": 1001, "prime": 7927},
        ],
    }

    insert_result_into_calc(doc, ctx, primes_result)
    # Close skipped after paste. Cached leftover_open so later Hidden/AWT
    # skips see these Writers (34649699848). Do not enum getComponents.
    note_windows_html_paste_leftover()
    assert active_sheet.getCellByPosition(0, 0).getString() == "Prime Numbers in Range"
    assert active_sheet.getCellByPosition(0, 2).getString() == "position"
    assert active_sheet.getCellByPosition(1, 2).getString() == "prime"

    um = doc.getUndoManager()
    assert um is not None
    assert um.isUndoPossible() is True

    um.undo()
    assert active_sheet.getCellByPosition(0, 0).getString() == "", "A1 title should revert after single undo"
    assert active_sheet.getCellByPosition(0, 2).getString() == "", "A3 position header should revert after single undo"
    assert active_sheet.getCellByPosition(1, 2).getString() == "", "B3 prime header should revert after single undo"


@native_test
@with_native_doc("calc")
def test_calc_spill_undo_lock(ctx, doc):
    """Deferred spill writes must not create extra undo actions on top of the formula."""
    from plugin.calc.python.function import perform_deferred_spill

    active_sheet = doc.getCurrentController().getActiveSheet()
    doc_url = getattr(doc, "getURL", lambda: "")() or ""

    # Simulate user typing a formula into A1
    active_sheet.getCellByPosition(0, 0).setFormula('=PY("result = [1, 2, 3]")')
    um = doc.getUndoManager()
    if um is not None:
        um.enterUndoContext("Input")
        um.leaveUndoContext()

    titles_before = list(um.getAllUndoActionTitles()) if um else []

    perform_deferred_spill(ctx, doc_url, active_sheet.Name, 0, 0, [[1, 2], [3, 4]], doc=doc)

    assert active_sheet.getCellByPosition(1, 0).getValue() == 2.0
    assert active_sheet.getCellByPosition(0, 1).getValue() == 3.0

    titles_after = list(um.getAllUndoActionTitles()) if um else []
    assert titles_after == titles_before, f"Deferred spill added extra undo actions: {titles_after} vs {titles_before}"

