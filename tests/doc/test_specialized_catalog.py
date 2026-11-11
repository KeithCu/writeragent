from __future__ import annotations

import json

from plugin.doc.specialized_catalog import (
    FINISH_TOOL_NAME,
    NO_EDIT_TOOL_NOTE,
    NO_TOOL_CALLS_MESSAGE,
    InnerToolCall,
    apply_inner_outcome,
    partition_tool_errors,
)


def test_partition_tool_errors_same_tool_later_ok_is_recovered() -> None:
    err_x = {"status": "error", "message": "x failed"}
    ok_x = {"status": "ok"}
    ok_y = {"status": "ok"}
    err_x2 = {"status": "error", "message": "x failed again"}

    # [x err, x ok] -> ([], [err_x])
    calls1 = [
        InnerToolCall("tool_x", err_x, mutating=True),
        InnerToolCall("tool_x", ok_x, mutating=True),
    ]
    unrecovered1, recovered1 = partition_tool_errors(calls1)
    assert unrecovered1 == []
    assert recovered1 == [err_x]

    # [x err, y ok] -> ([err_x], [])
    calls2 = [
        InnerToolCall("tool_x", err_x, mutating=True),
        InnerToolCall("tool_y", ok_y, mutating=True),
    ]
    unrecovered2, recovered2 = partition_tool_errors(calls2)
    assert unrecovered2 == [err_x]
    assert recovered2 == []

    # [x err, x err] -> ([err_x, err_x2], [])
    calls3 = [
        InnerToolCall("tool_x", err_x, mutating=True),
        InnerToolCall("tool_x", err_x2, mutating=True),
    ]
    unrecovered3, recovered3 = partition_tool_errors(calls3)
    assert unrecovered3 == [err_x, err_x2]
    assert recovered3 == []


def test_apply_inner_outcome_finish_only_is_no_tool_calls() -> None:
    answer = "No tools needed, task finished directly."
    final_ans = {"status": "ok", "finished": True, "answer": answer}
    payload = dict(final_ans)
    calls = [InnerToolCall(FINISH_TOOL_NAME, final_ans, mutating=False)]

    result = apply_inner_outcome(payload, final_ans, calls, expect_edits=True)
    assert result["status"] == "incomplete"
    assert result["code"] == "SPECIALIZED_NO_TOOL_CALLS"
    assert result["message"] == NO_TOOL_CALLS_MESSAGE
    assert "finished" not in result
    assert result["answer"] == answer
    assert json.dumps(result).count(answer) == 1


def test_apply_inner_outcome_recovered_error_stays_ok() -> None:
    err = {"status": "error", "message": "Syntax error in python script"}
    ok = {"status": "ok", "output": "24133"}
    finish = {"status": "ok", "finished": True, "answer": "The sum is 24133"}
    payload = dict(finish)
    calls = [
        InnerToolCall("run_venv_python_script", err, mutating=True),
        InnerToolCall("run_venv_python_script", ok, mutating=True),
        InnerToolCall(FINISH_TOOL_NAME, finish, mutating=False),
    ]

    result = apply_inner_outcome(payload, finish, calls, expect_edits=True)
    assert result["status"] == "ok"
    assert result["finished"] is True
    assert result["recovered_tool_errors"] == ["Syntax error in python script"]
    assert "code" not in result


def test_apply_inner_outcome_unrecovered_error_is_error() -> None:
    chart_err = {"status": "error", "message": "Invalid range address"}
    finish = {"status": "ok", "finished": True, "answer": "Chart created"}
    payload = dict(finish)
    calls = [
        InnerToolCall("chart_upsert", chart_err, mutating=True),
        InnerToolCall(FINISH_TOOL_NAME, finish, mutating=False),
    ]

    result = apply_inner_outcome(payload, finish, calls, expect_edits=True)
    assert result["status"] == "error"
    assert result["code"] == "SPECIALIZED_TOOL_ERROR"
    assert result["message"] == "Invalid range address"
    assert result["error"] == "Invalid range address"
    assert result["answer"] == "Chart created"


def test_apply_inner_outcome_read_only_calls_note() -> None:
    finish = {"status": "ok", "finished": True, "answer": "Inspected document"}

    calls_readonly = [
        InnerToolCall("get_document_tree", {"status": "ok"}, mutating=False),
        InnerToolCall(FINISH_TOOL_NAME, finish, mutating=False),
    ]
    # expect_edits=True -> note attached
    payload1 = dict(finish)
    res1 = apply_inner_outcome(payload1, finish, calls_readonly, expect_edits=True)
    assert res1["status"] == "ok"
    assert res1.get("note") == NO_EDIT_TOOL_NOTE

    # expect_edits=False -> no note attached
    payload2 = dict(finish)
    res2 = apply_inner_outcome(payload2, finish, calls_readonly, expect_edits=False)
    assert res2["status"] == "ok"
    assert "note" not in res2

    # Mutating tool ran -> no note attached
    calls_mutating = [
        InnerToolCall("image_generate", {"status": "ok"}, mutating=True),
        InnerToolCall(FINISH_TOOL_NAME, finish, mutating=False),
    ]
    payload3 = dict(finish)
    res3 = apply_inner_outcome(payload3, finish, calls_mutating, expect_edits=True)
    assert res3["status"] == "ok"
    assert "note" not in res3


def test_apply_inner_outcome_unobserved_finished_dict_stays_ok() -> None:
    finish = {"status": "ok", "finished": True, "answer": "Done"}
    payload = dict(finish)
    result = apply_inner_outcome(payload, finish, calls=[], expect_edits=True)
    assert result["status"] == "ok"
    assert result["finished"] is True
    assert "note" not in result
    assert "code" not in result


def test_apply_inner_outcome_plain_text_is_incomplete() -> None:
    final_ans = "text"
    payload = {"status": "ok", "result": "text"}
    result = apply_inner_outcome(payload, final_ans, calls=[], expect_edits=True)
    assert result["status"] == "incomplete"
    assert result["code"] == "SPECIALIZED_WORKFLOW_INCOMPLETE"
    assert result["message"] == "text"
    assert "result" not in result
