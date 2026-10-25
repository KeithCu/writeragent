# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for sticky sidebar reply_to_user (leave flag = old tool name)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from plugin.chatbot.sticky_reply import (
    BRAINSTORMING_REPLY_SPEC,
    LIBRARIAN_REPLY_SPEC,
    STATUS_FINISHED,
    STATUS_INCOMPLETE,
    STATUS_OK,
    STATUS_SWITCH_MODE,
    StickyReplySpec,
    StickyReplyToUserTool,
    WRITING_PLAN_REPLY_SPEC,
    interpret_sticky_final_answer,
)
from plugin.chatbot.smol_agent import SmolToolAdapter
from plugin.contrib.smolagents.agents import ToolCallingAgent
from plugin.contrib.smolagents.default_tools import FinalAnswerTool


@pytest.mark.parametrize(
    ("flag_value", "expect_leave"),
    [
        (None, False),
        (False, False),
        ("false", False),
        (True, True),
        ("true", True),
        ("1", True),
        (1, True),
    ],
)
def test_librarian_leave_flag_coercion(flag_value, expect_leave):
    tool = StickyReplyToUserTool(LIBRARIAN_REPLY_SPEC)
    kwargs: dict = {"answer": "Bye"}
    if flag_value is not None:
        kwargs["switch_to_document_mode"] = flag_value
    out = tool.execute(MagicMock(), **kwargs)
    if expect_leave:
        assert isinstance(out, dict)
        assert out["status"] == STATUS_SWITCH_MODE
        assert out["result"] == "Bye"
        assert "spec_saved" not in out
    else:
        assert out == {"status": STATUS_OK, "result": "Bye"}


def test_stay_is_ok_dict():
    tool = StickyReplyToUserTool(LIBRARIAN_REPLY_SPEC)
    assert tool.execute(MagicMock(), answer="<p>Hi</p>") == {
        "status": STATUS_OK,
        "result": "<p>Hi</p>",
    }


def test_tool_is_async():
    tool = StickyReplyToUserTool(LIBRARIAN_REPLY_SPEC)
    assert tool.is_async() is True


def test_spec_validation_asserts():
    # Empty leave_flag
    with pytest.raises(AssertionError):
        StickyReplySpec(leave_flag="", leave_status="finished")
    # leave_flag collision with reserved
    with pytest.raises(AssertionError):
        StickyReplySpec(leave_flag="answer", leave_status="finished")
    # extra_bools collision with reserved
    with pytest.raises(AssertionError):
        StickyReplySpec(leave_flag="done", leave_status="finished", extra_bools=("status",))
    # extra_bools collision with leave_flag
    with pytest.raises(AssertionError):
        StickyReplySpec(leave_flag="done", leave_status="finished", extra_bools=("done",))
    # tuple (name, desc) allowed
    valid = StickyReplySpec(
        leave_flag="done",
        leave_status="finished",
        extra_bools=(("spec_saved", "recorded flag"),),
    )
    assert valid.leave_flag == "done"


def test_brainstorming_spec_saved_observed_not_claimed():
    tool = StickyReplyToUserTool(BRAINSTORMING_REPLY_SPEC)
    # Model claims spec_saved=True, but no save observed -> spec_saved is False
    leave = tool.execute(
        MagicMock(),
        answer="<p>Done</p>",
        brainstorming_finished=True,
        spec_saved=True,
    )
    assert leave["status"] == STATUS_FINISHED
    assert leave["spec_saved"] is False
    assert leave["result"] == "<p>Done</p>"

    # Observed save -> spec_saved is True even if omitted in kwargs
    tool.record_tool_result("save_design_spec", {"status": "ok"})
    leave2 = tool.execute(
        MagicMock(),
        answer="<p>Done</p>",
        brainstorming_finished=True,
    )
    assert leave2["spec_saved"] is True


def test_writing_plan_leave_drops_unused_plan_completed():
    """plan_completed was never read by the panel. It is not a schema field."""
    tool = StickyReplyToUserTool(WRITING_PLAN_REPLY_SPEC)
    props = (tool.parameters or {})["properties"]
    assert "plan_completed" not in props
    assert "writing_plan_finished" in props
    leave = tool.execute(
        MagicMock(),
        answer="<p>All sections</p>",
        writing_plan_finished="yes",
        plan_completed=1,
    )
    assert leave["status"] == STATUS_FINISHED
    assert leave["result"] == "<p>All sections</p>"
    assert "plan_completed" not in leave


def test_interpret_string_is_incomplete():
    # Item 38: string output from max steps becomes incomplete
    assert interpret_sticky_final_answer("hello", leave_status="switch_mode") == {
        "status": STATUS_INCOMPLETE,
        "result": "hello",
    }


def test_interpret_none_or_unknown_dict_is_incomplete():
    assert interpret_sticky_final_answer(None, leave_status="switch_mode") == {
        "status": STATUS_INCOMPLETE,
        "result": "",
    }
    assert interpret_sticky_final_answer({"status": "unknown", "result": "foo"}, leave_status="switch_mode") == {
        "status": STATUS_INCOMPLETE,
        "result": "foo",
    }


def test_interpret_ok_dict():
    raw = {"status": STATUS_OK, "result": "all good"}
    assert interpret_sticky_final_answer(raw, leave_status="finished") == {
        "status": STATUS_OK,
        "result": "all good",
    }


def test_interpret_leave_dict():
    raw = {"status": "switch_mode", "message": "Handoff", "result": "Handoff"}
    out = interpret_sticky_final_answer(raw, leave_status="switch_mode")
    assert out["status"] == "switch_mode"
    assert out["result"] == "Handoff"


def test_interpret_leave_dict_message_only():
    raw = {"status": "finished", "message": "Bye"}
    out = interpret_sticky_final_answer(raw, leave_status="finished")
    assert out["status"] == "finished"
    assert out["result"] == "Bye"


def test_interpret_error_passthrough():
    err = {"status": "error", "message": "nope"}
    assert interpret_sticky_final_answer(err, leave_status="finished") is err


def test_agent_uses_sticky_reply_not_stock_or_old_leave_tool():
    ctx = MagicMock()
    reply = SmolToolAdapter(StickyReplyToUserTool(LIBRARIAN_REPLY_SPEC), ctx, safe=False, inputs_style="librarian")
    agent = ToolCallingAgent(tools=[reply], model=MagicMock(), final_answer_tool_name="reply_to_user")
    assert "reply_to_user" in agent.tools
    assert "switch_to_document_mode" not in agent.tools
    assert "brainstorming_finished" not in agent.tools
    assert not isinstance(agent.tools["reply_to_user"], FinalAnswerTool)
    assert agent.final_answer_tool_name == "reply_to_user"
