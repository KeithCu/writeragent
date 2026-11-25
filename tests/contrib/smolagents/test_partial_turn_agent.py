"""Tests for partial-turn recovery in ToolCallingAgent."""

from __future__ import annotations

from unittest.mock import MagicMock

from plugin.contrib.smolagents.agents import ToolCallingAgent
from plugin.contrib.smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
)
from plugin.contrib.smolagents.monitoring import LogLevel
from plugin.contrib.smolagents.tools import Tool
from tests.harness.smol_messages import flatten_messages


def _assistant_msg(*calls: tuple[str, dict, str]) -> ChatMessage:
    return ChatMessage(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[
            ChatMessageToolCall(
                function=ChatMessageToolCallFunction(name=name, arguments=args),
                id=call_id,
                type="function",
            )
            for name, args, call_id in calls
        ],
    )


class _EditTool(Tool):
    name = "edit_doc"
    description = "Edit document."
    inputs = {"n": {"type": "integer", "description": "Count"}}
    output_type = "string"

    def __init__(self, *, fail_on: int | None = None) -> None:
        super().__init__()
        self.calls: list[int] = []
        self._fail_on = fail_on

    def forward(self, n: int) -> str:
        self.calls.append(n)
        if self._fail_on is not None and len(self.calls) == self._fail_on:
            raise RuntimeError("boom")
        return f"edited:{n}"


class _FinalTool(Tool):
    name = "final_answer"
    description = "Finish."
    inputs = {"answer": {"type": "string", "description": "Answer"}}
    output_type = "string"

    def forward(self, answer: str) -> str:
        return answer


def _make_agent(model: MagicMock, *tools: Tool):
    return ToolCallingAgent(
        tools=list(tools),
        model=model,
        max_steps=4,
        instructions="i",
        final_answer_tool_name="final_answer",
        verbosity_level=LogLevel.OFF,
    )


def test_double_final_keeps_earlier_result_and_skips_trailing_call():
    edit = _EditTool()
    seen: list[str] = []

    def _gen(messages, **kwargs):
        del kwargs
        seen.append(flatten_messages(messages))
        if len(seen) == 1:
            return _assistant_msg(
                ("edit_doc", {"n": 1}, "c1"),
                ("final_answer", {"answer": "a"}, "c2"),
                ("final_answer", {"answer": "b"}, "c3"),
                ("edit_doc", {"n": 2}, "c4"),
            )
        return _assistant_msg(("final_answer", {"answer": "clean"}, "c5"))

    model = MagicMock()
    model.generate.side_effect = _gen
    agent = _make_agent(model, edit, _FinalTool())

    assert agent.run("t") == "clean"
    assert edit.calls == [1]
    req2 = seen[1]
    assert "Call 1 (edit_doc) ran:\nedited:1" in req2
    assert "Call 2 (final_answer) was not accepted because this turn failed." in req2
    assert "Call 3 (final_answer) was not accepted because this turn failed." in req2
    assert "Call 4 (edit_doc) did not run because this turn had already failed." in req2
    assert "failed; its error is below." not in req2


def test_raising_tool_records_ran_failed_and_did_not_run():
    edit = _EditTool(fail_on=2)
    seen: list[str] = []

    def _gen(messages, **kwargs):
        del kwargs
        seen.append(flatten_messages(messages))
        if len(seen) == 1:
            return _assistant_msg(
                ("edit_doc", {"n": 1}, "c1"),
                ("edit_doc", {"n": 2}, "c2"),
                ("edit_doc", {"n": 3}, "c3"),
            )
        return _assistant_msg(("final_answer", {"answer": "done"}, "c4"))

    model = MagicMock()
    model.generate.side_effect = _gen
    agent = _make_agent(model, edit, _FinalTool())

    assert agent.run("t") == "done"
    assert edit.calls == [1, 2]
    req2 = seen[1]
    assert "Call 1 (edit_doc) ran:\nedited:1" in req2
    assert "Call 2 (edit_doc) failed; its error is below." in req2
    assert "Call 3 (edit_doc) did not run because this turn had already failed." in req2


def test_bad_argument_type_keeps_earlier_result():
    edit = _EditTool()
    seen: list[str] = []

    def _gen(messages, **kwargs):
        del kwargs
        seen.append(flatten_messages(messages))
        if len(seen) == 1:
            return _assistant_msg(
                ("edit_doc", {"n": 7}, "c1"),
                ("edit_doc", {"n": "not-an-int"}, "c2"),
            )
        return _assistant_msg(("final_answer", {"answer": "done"}, "c3"))

    model = MagicMock()
    model.generate.side_effect = _gen
    agent = _make_agent(model, edit, _FinalTool())

    assert agent.run("t") == "done"
    assert edit.calls == [7]
    req2 = seen[1]
    assert "Call 1 (edit_doc) ran:\nedited:7" in req2
    assert "Call 2 (edit_doc) failed; its error is below." in req2


def test_finish_then_raising_edit_keeps_not_accepted_and_continues():
    edit = _EditTool(fail_on=1)
    seen: list[str] = []

    def _gen(messages, **kwargs):
        del kwargs
        seen.append(flatten_messages(messages))
        if len(seen) == 1:
            return _assistant_msg(
                ("final_answer", {"answer": "early"}, "c1"),
                ("edit_doc", {"n": 1}, "c2"),
            )
        return _assistant_msg(("final_answer", {"answer": "recovered"}, "c3"))

    model = MagicMock()
    model.generate.side_effect = _gen
    agent = _make_agent(model, edit, _FinalTool())

    assert agent.run("t") == "recovered"
    req2 = seen[1]
    assert "Call 1 (final_answer) was not accepted because this turn failed." in req2
    assert "Call 2 (edit_doc) failed; its error is below." in req2


def test_single_failing_call_leaves_observations_and_tool_calls_none():
    edit = _EditTool(fail_on=1)
    model = MagicMock()
    model.generate.side_effect = [
        _assistant_msg(("edit_doc", {"n": 1}, "c1")),
        _assistant_msg(("final_answer", {"answer": "done"}, "c2")),
    ]
    agent = _make_agent(model, edit, _FinalTool())

    assert agent.run("t") == "done"
    first_action = agent.memory.steps[1]
    assert first_action.observations is None
    assert first_action.tool_calls is None


def test_successful_multicall_turn_keeps_plain_observations():
    edit = _EditTool()
    model = MagicMock()
    model.generate.side_effect = [
        _assistant_msg(("edit_doc", {"n": 1}, "c1"), ("edit_doc", {"n": 2}, "c2")),
        _assistant_msg(("final_answer", {"answer": "done"}, "c3")),
    ]

    agent = _make_agent(model, edit, _FinalTool())
    assert agent.run("t") == "done"

    obs = agent.memory.steps[1].observations
    assert obs == "edited:1\nedited:2"


def test_secondary_finish_tool_is_not_accepted_when_turn_fails():
    class _SecondaryFinishTool(Tool):
        name = "secondary_finish"
        description = "Secondary finish."
        inputs = {}
        output_type = "string"
        is_final_answer_tool = True

        def forward(self) -> str:
            return "secondary-finished-payload"

    seen: list[str] = []

    def _gen(messages, **kwargs):
        del kwargs
        seen.append(flatten_messages(messages))
        if len(seen) == 1:
            return _assistant_msg(
                ("secondary_finish", {}, "c1"),
                ("no_such_tool", {}, "c2"),
            )
        return _assistant_msg(("final_answer", {"answer": "done"}, "c3"))

    model = MagicMock()
    model.generate.side_effect = _gen
    agent = _make_agent(model, _SecondaryFinishTool(), _FinalTool())

    assert agent.run("t") == "done"
    req2 = seen[1]
    assert "Call 1 (secondary_finish) was not accepted because this turn failed." in req2
    assert "secondary-finished-payload" not in req2
