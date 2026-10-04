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
    WRITING_PLAN_REPLY_SPEC,
    StickyReplyToUserTool,
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
        assert out["status"] == "switch_mode"
        assert out["result"] == "Bye"
        assert "spec_saved" not in out
    else:
        assert out == "Bye"


def test_stay_is_plain_string():
    tool = StickyReplyToUserTool(LIBRARIAN_REPLY_SPEC)
    assert tool.execute(MagicMock(), answer="<p>Hi</p>") == "<p>Hi</p>"


def test_brainstorming_extra_bool_only_on_leave():
    tool = StickyReplyToUserTool(BRAINSTORMING_REPLY_SPEC)
    stay = tool.execute(MagicMock(), answer="<p>Q?</p>", spec_saved=True)
    assert stay == "<p>Q?</p>"
    leave = tool.execute(
        MagicMock(),
        answer="<p>Done</p>",
        brainstorming_finished=True,
        spec_saved=True,
    )
    assert leave["status"] == "finished"
    assert leave["spec_saved"] is True
    assert leave["result"] == "<p>Done</p>"


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
    assert leave["status"] == "finished"
    assert leave["result"] == "<p>All sections</p>"
    assert "plan_completed" not in leave


def test_interpret_string_is_ok():
    assert interpret_sticky_final_answer("hello", leave_status="switch_mode") == {
        "status": "ok",
        "result": "hello",
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
