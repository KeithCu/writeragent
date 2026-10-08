# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for native Calc array formulas (setArrayFormula)."""

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import execute_calc_tool as _execute_calc_tool, with_native_doc


@native_test
@with_native_doc("calc")
def test_sequence_writes_array_block(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    res = _execute_calc_tool(
        doc, ctx, "write_formula_range", {"range": ["A1"], "values": "=SEQUENCE(3;2)"}
    )
    assert res.get("status") == "ok", f"SEQUENCE write failed: {res}"
    assert res.get("rows") == 3, f"expected 3 rows: {res}"
    assert res.get("cols") == 2, f"expected 2 cols: {res}"
    assert res.get("array_range") == "A1:B3", f"array_range: {res}"

    rng = sheet.getCellRangeByPosition(0, 0, 1, 2)
    stored = rng.getArrayFormula()
    assert "SEQUENCE" in stored.upper(), f"getArrayFormula={stored!r}"

    assert sheet.getCellByPosition(0, 0).getValue() == 1
    assert sheet.getCellByPosition(1, 0).getValue() == 2
    assert sheet.getCellByPosition(0, 2).getValue() == 5
    assert sheet.getCellByPosition(1, 2).getValue() == 6


@native_test
@with_native_doc("calc")
def test_occupied_neighbor_refuses_and_leaves_origin(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    sheet.getCellByPosition(1, 1).setString("keep-me")
    origin = sheet.getCellByPosition(0, 0)
    origin.setString("before")

    res = _execute_calc_tool(
        doc, ctx, "write_formula_range", {"range": ["A1"], "values": "=SEQUENCE(3;2)"}
    )
    assert res.get("status") == "error", f"occupied write should fail: {res}"
    msg = res.get("message") or res.get("error") or ""
    assert "B2" in msg, f"error should name blocking address: {res}"
    assert origin.getString() == "before", f"origin changed: {origin.getString()!r}"
    assert sheet.getCellByPosition(1, 1).getString() == "keep-me"


@native_test
@with_native_doc("calc")
def test_sum_of_filter_stays_scalar(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    sheet.getCellByPosition(0, 0).setValue(1)
    sheet.getCellByPosition(0, 1).setValue(2)
    sheet.getCellByPosition(0, 2).setValue(3)
    sheet.getCellByPosition(1, 0).setValue(1)
    sheet.getCellByPosition(1, 1).setValue(0)
    sheet.getCellByPosition(1, 2).setValue(1)

    res = _execute_calc_tool(
        doc,
        ctx,
        "write_formula_range",
        {"range": ["C1"], "values": "=SUM(FILTER(A1:A3;B1:B3>0))"},
    )
    assert res.get("status") == "ok", f"SUM(FILTER) write failed: {res}"
    assert "array_range" not in res, f"should stay scalar: {res}"

    cell = sheet.getCellByPosition(2, 0)
    assert cell.getFormula().upper().startswith("=SUM("), cell.getFormula()
    try:
        array_txt = sheet.getCellRangeByPosition(2, 0, 2, 0).getArrayFormula()
    except Exception:
        array_txt = ""
    assert not array_txt, f"C1 should not be an array formula: {array_txt!r}"
    assert cell.getValue() == 4, f"SUM(FILTER) value={cell.getValue()}"
    assert sheet.getCellByPosition(2, 1).getString() == ""


@native_test
@with_native_doc("calc")
def test_array_true_forces_set_array_formula(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    sheet.getCellByPosition(2, 0).setValue(10)
    sheet.getCellByPosition(2, 1).setValue(20)
    res = _execute_calc_tool(
        doc,
        ctx,
        "write_formula_range",
        {"range": ["A1"], "values": "=C1:C2", "array": True},
    )
    assert res.get("status") == "ok", f"forced array write failed: {res}"
    stored = sheet.getCellRangeByPosition(0, 0, 0, 1).getArrayFormula()
    assert stored, f"expected array formula, got {stored!r}"
    assert sheet.getCellByPosition(0, 0).getValue() == 10
    assert sheet.getCellByPosition(0, 1).getValue() == 20
