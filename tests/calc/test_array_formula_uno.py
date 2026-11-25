# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for native Calc array formulas (setArrayFormula)."""

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import execute_calc_tool as _execute_calc_tool, libreoffice_version, with_native_doc
from tests.harness.uno_profile_config import repo_venv_in_profile, venv_in_profile


@native_test
@with_native_doc("calc")
def test_sequence_writes_array_block(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    res = _execute_calc_tool(
        doc, ctx, "write_range", {"range": ["A1"], "values": "=SEQUENCE(3;2)"}
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
        doc, ctx, "write_range", {"range": ["A1"], "values": "=SEQUENCE(3;2)"}
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
        "write_range",
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
        "write_range",
        {"range": ["A1"], "values": "=C1:C2", "array": True},
    )
    assert res.get("status") == "ok", f"forced array write failed: {res}"
    stored = sheet.getCellRangeByPosition(0, 0, 0, 1).getArrayFormula()
    assert stored, f"expected array formula, got {stored!r}"
    assert sheet.getCellByPosition(0, 0).getValue() == 10
    assert sheet.getCellByPosition(0, 1).getValue() == 20


@native_test
@with_native_doc("calc")
def test_array_true_accepts_single_value(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    res = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["A1"], "values": "=XLOOKUP(1;{1|2};{7|8})", "array": True},
    )
    assert res.get("status") == "ok", f"XLOOKUP single-value array write failed: {res}"
    assert res.get("array_range") == "A1:A1"
    assert sheet.getCellByPosition(0, 0).getValue() == 7

    res_sum = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["C1:C3"], "values": "=SUM(1;2)", "array": True},
    )
    assert res_sum.get("status") == "ok", f"SUM array write failed: {res_sum}"
    for row in range(3):
        assert sheet.getCellByPosition(2, row).getValue() == 3
    warning = res_sum.get("warning", "")
    assert "repeated" in warning
    assert "#N/A" not in warning


@native_test
@with_native_doc("calc")
def test_single_row_array_repeats_down_the_range(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    res = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["E1:F3"], "values": "=SEQUENCE(1;2)", "array": True},
    )
    assert res.get("status") == "ok", f"SEQUENCE(1;2) array write failed: {res}"
    for row in range(3):
        assert sheet.getCellByPosition(4, row).getValue() == 1
        assert sheet.getCellByPosition(5, row).getValue() == 2
    warning = res.get("warning", "")
    assert "repeated" in warning
    assert "#N/A" not in warning


@native_test(timeout=60)
@with_native_doc("calc")
def test_array_true_py_list(ctx, doc):
    from plugin.calc.python.workbook_state import WorkbookState

    sheet = doc.getCurrentController().getActiveSheet()
    try:
        with repo_venv_in_profile():
            res = _execute_calc_tool(
                doc,
                ctx,
                "write_range",
                {"range": ["A1"], "values": '=PY("result = [10, 20, 30]")', "array": True},
            )
            if libreoffice_version(ctx) >= (26, 8):
                assert res.get("status") == "ok", f"PY array write failed: {res}"
                assert res.get("rows") == 3, f"expected 3 rows: {res}"
                assert [sheet.getCellByPosition(0, r).getValue() for r in range(3)] == [10, 20, 30]

                res_row_ref = _execute_calc_tool(
                    doc,
                    ctx,
                    "write_range",
                    {"range": ["H20"], "values": '=PY("result = [1, 2]"; 1:3)', "array": True},
                )
                assert res_row_ref.get("status") == "ok", res_row_ref
                assert [sheet.getCellByPosition(7, r).getValue() for r in (19, 20)] == [1, 2]
            else:
                assert res.get("status") == "error", f"expected refusal before 26.8: {res}"
                assert res.get("code") == "INVALID_PARAM"
                assert res.get("suggested_call", {}).get("arguments", {}).get("range") == [f"{sheet.getName()}.A1"]
                assert [sheet.getCellByPosition(0, r).getString() for r in range(3)] == ["", "", ""]
    finally:
        st = WorkbookState.known(doc)
        if st is not None:
            WorkbookState.forget_key(st.key)


@native_test
@with_native_doc("calc")
def test_py_array_own_data_refused_uno(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    for row, val in enumerate((1, 2, 3)):
        sheet.getCellByPosition(0, row).setValue(val)

    res = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["A1"], "values": '=PY("result = data"; A1:A3)', "array": True},
    )
    assert res.get("status") == "error", f"expected PY_CIRCULAR refusal: {res}"
    assert res.get("code") == "PY_CIRCULAR", res
    assert res.get("suggested_call", {}).get("arguments", {}).get("range") == [f"{sheet.getName()}.B1"], res
    assert sheet.getCellByPosition(0, 0).getValue() == 1


@native_test(timeout=60)
@with_native_doc("calc")
def test_py_setup_failure_refused_for_array_and_warned_for_scalar_uno(ctx, doc):
    from plugin.calc.python.workbook_state import WorkbookState

    sheet = doc.getCurrentController().getActiveSheet()
    try:
        with venv_in_profile("/nonexistent/venv"):
            res_arr = _execute_calc_tool(
                doc,
                ctx,
                "write_range",
                {"range": ["A1:A3"], "values": '=PY("result = 1")', "array": True},
            )
            assert res_arr.get("status") == "error", f"expected refusal: {res_arr}"
            if libreoffice_version(ctx) >= (26, 8):
                msg = res_arr.get("message") or res_arr.get("error") or ""
                assert "Python venv not found" in msg, res_arr
            else:
                assert res_arr.get("code") == "INVALID_PARAM", res_arr
            assert [sheet.getCellByPosition(0, r).getString() for r in range(3)] == ["", "", ""]

            res_scalar = _execute_calc_tool(
                doc,
                ctx,
                "write_range",
                {"range": ["D1"], "values": '=PY("result = 1")'},
            )
            assert res_scalar.get("status") == "ok", res_scalar
            assert "shows an error message" in (res_scalar.get("message") or ""), res_scalar
    finally:
        st = WorkbookState.known(doc)
        if st is not None:
            WorkbookState.forget_key(st.key)


@native_test
@with_native_doc("calc")
def test_array_error_hints_and_commit_code_uno(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    for row, val in enumerate((1, 2, 3)):
        sheet.getCellByPosition(0, row).setValue(val)

    res_filter = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["C1"], "values": "=FILTER(A1:A3;A1:A3>9)"},
    )
    assert res_filter.get("status") == "error", res_filter
    msg_filter = res_filter.get("message") or res_filter.get("error") or ""
    assert "533" in msg_filter and "FILTER" in msg_filter, res_filter

    res_div0 = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["D1"], "values": "=1/0", "array": True},
    )
    assert res_div0.get("status") == "error", res_div0
    msg_div0 = res_div0.get("message") or res_div0.get("error") or ""
    assert "532" in msg_div0 and "FILTER" not in msg_div0, res_div0

    res_circ = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["A1"], "values": "=SUM(A1:A3)", "array": True},
    )
    assert res_circ.get("status") == "error", res_circ
    msg_circ = res_circ.get("message") or res_circ.get("error") or ""
    assert "522" in msg_circ and "code 0" not in msg_circ, res_circ
    assert "circular" in msg_circ.lower(), res_circ
    assert sheet.getCellByPosition(0, 0).getValue() == 1


@native_test
@with_native_doc("calc")
def test_measure_avoids_referenced_rows_uno(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    for row, val in enumerate((1, 2, 3)):
        sheet.getCellByPosition(0, row).setValue(val)

    res_sum = _execute_calc_tool(doc, ctx, "write_range", {"range": ["H20"], "values": "=SUM(1:3)", "array": True})
    assert res_sum.get("status") == "ok", res_sum
    assert sheet.getCellByPosition(7, 19).getValue() == 6

    doc.getSheets().insertNewByName("Fresh", 1)
    fresh = doc.getSheets().getByName("Fresh")
    res_rect = _execute_calc_tool(doc, ctx, "write_range", {"range": ["Fresh.A1"], "values": "=SUM(F1:Z1000)", "array": True})
    assert res_rect.get("status") == "ok", res_rect
    assert fresh.getCellByPosition(0, 0).getValue() == 0


@native_test
@with_native_doc("calc")
def test_measure_keeps_last_column_cells_uno(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    sheet.getCellByPosition(16383, 0).setString("top1")
    sheet.getCellByPosition(16383, 1).setString("top2")
    sheet.getCellByPosition(16383, 50).setString("end")

    res = _execute_calc_tool(doc, ctx, "write_range", {"range": ["A1"], "values": "=SEQUENCE(2)"})
    assert res.get("status") == "ok", res
    assert [sheet.getCellByPosition(16383, r).getString() for r in (0, 1, 50)] == ["top1", "top2", "end"]


@native_test
@with_native_doc("calc")
def test_nested_py_array_refused_uno(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()
    sheet.getCellByPosition(0, 0).setValue(42)

    res_if = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["A1"], "values": '=IFERROR(PY("result = 1"; A1:A3);7)', "array": True},
    )
    assert res_if.get("status") == "error", res_if
    assert res_if.get("code") == "INVALID_PARAM", res_if
    assert sheet.getCellByPosition(0, 0).getValue() == 42

    res_tr = _execute_calc_tool(
        doc,
        ctx,
        "write_range",
        {"range": ["C1"], "values": '=TRANSPOSE(PY("result = 1"))', "array": True},
    )
    assert res_tr.get("status") == "error", res_tr
    assert res_tr.get("code") == "INVALID_PARAM", res_tr
    c1 = sheet.getCellByPosition(2, 0)
    assert c1.getString() == ""
    assert sheet.getCellRangeByPosition(2, 0, 2, 0).getArrayFormula() == ""
