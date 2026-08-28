# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Hypothesis strategies for chat / MCP pure FSMs (light in verify, deep via make vhs)."""

from __future__ import annotations

from hypothesis import strategies as st

from plugin.chatbot.audio_recorder_state import (
    AudioRecorderState,
    DeviceReadyEvent,
    ErrorOccurredEvent,
    StartRequestedEvent,
    StopRequestedEvent as AudioStopRequestedEvent,
    _VALID_AUDIO_STATUSES,
)
from plugin.chatbot.send_state import SendButtonState, SendEvent, SendEventKind
from plugin.chatbot.state_machine import (
    ErrorEvent,
    SendHandlerState,
    StartEvent,
    StopRequestedEvent,
    StreamChunkEvent,
    StreamDoneEvent,
)
from plugin.chatbot.tool_loop_state import EventKind as ToolEventKind
from plugin.chatbot.tool_loop_state import ToolLoopEvent, ToolLoopState
from plugin.mcp.mcp_state import EventKind as MCPEventKind
from plugin.mcp.mcp_state import MCPEvent, MCPState, MCPStateStr
from tests.vhs_budget import vhs_extensive

_FSM_HYP_LIGHT = {
    "send": 80,
    "audio": 60,
    "tool_loop": 60,
    "send_handler": 60,
    "mcp": 80,
    "sequences": 40,
}
_FSM_HYP_EXTENSIVE = {
    "send": 800,
    "audio": 600,
    "tool_loop": 600,
    "send_handler": 600,
    "mcp": 800,
    "sequences": 400,
}


def fsm_hypothesis_max_examples() -> dict[str, int]:
    """Hypothesis max_examples per FSM fuzz test (light in make verify, heavy via make vhs)."""
    if vhs_extensive():
        return dict(_FSM_HYP_EXTENSIVE)
    return dict(_FSM_HYP_LIGHT)


@st.composite
def send_button_states(draw):
    # Avoid illegal busy+recording seeds; the transition must never produce that pair.
    is_busy = draw(st.booleans())
    is_recording = draw(st.booleans())
    if is_busy and is_recording:
        is_recording = False
    return SendButtonState(
        is_busy=is_busy,
        is_recording=is_recording,
        has_text=draw(st.booleans()),
        has_audio=draw(st.booleans()),
        audio_supported=draw(st.booleans()),
    )


@st.composite
def send_events(draw):
    kind = draw(st.sampled_from(list(SendEventKind)))
    if kind == SendEventKind.TEXT_UPDATED:
        return SendEvent(kind, {"has_text": draw(st.booleans())})
    return SendEvent(kind, {})


@st.composite
def audio_recorder_states(draw):
    return AudioRecorderState(status=draw(st.sampled_from(_VALID_AUDIO_STATUSES)))


@st.composite
def audio_recorder_events(draw):
    kind = draw(st.sampled_from(("start", "ready", "stop", "error")))
    if kind == "start":
        return StartRequestedEvent()
    if kind == "ready":
        return DeviceReadyEvent()
    if kind == "stop":
        return AudioStopRequestedEvent()
    return ErrorOccurredEvent(error_message=draw(st.sampled_from(("boom", "device", ""))))


def pending_tool_call() -> st.SearchStrategy[dict]:
    """One OpenAI-shaped tool_call dict matching ``pending_tool_call_fields``."""
    return st.fixed_dictionaries(
        {
            "id": st.sampled_from(("c1", "c2", "c3")),
            "function": st.fixed_dictionaries(
                {
                    "name": st.sampled_from(("noop", "web_search")),
                    "arguments": st.just("{}"),
                }
            ),
        }
    )


def pending_tool_call_list(*, min_size: int = 0, max_size: int = 3) -> st.SearchStrategy[list]:
    return st.lists(pending_tool_call(), min_size=min_size, max_size=max_size)


@st.composite
def tool_loop_states(draw):
    max_rounds = draw(st.integers(min_value=1, max_value=8))
    round_num = draw(st.integers(min_value=0, max_value=max_rounds))
    is_stopped = draw(st.booleans())
    # st.lists(min_size=0) is empty-heavy; one_of keeps leftover pending visible
    # under the light verify budget (60). When stopped, extra nonempty draws so
    # NEXT_TOOL hits the spawn-exclusion hole instead of mostly pending=[].
    nonempty = pending_tool_call_list(min_size=1, max_size=3)
    if is_stopped:
        pending = draw(st.one_of(st.just([]), nonempty, nonempty))
    else:
        pending = draw(st.one_of(st.just([]), nonempty))
    return ToolLoopState(
        round_num=round_num,
        pending_tools=pending,
        max_rounds=max_rounds,
        status=draw(st.sampled_from(("Thinking...", "Running tool...", "Stopped", "Ready"))),
        is_stopped=is_stopped,
    )


@st.composite
def tool_loop_events(draw):
    # Prefer kinds with simple data; STREAM_DONE / TOOL_RESULT use minimal payloads.
    kind = draw(
        st.sampled_from(
            (
                ToolEventKind.STOP_REQUESTED,
                ToolEventKind.FINAL_DONE,
                ToolEventKind.ERROR,
                ToolEventKind.NEXT_TOOL,
                ToolEventKind.STREAM_DONE,
                ToolEventKind.TOOL_RESULT,
            )
        )
    )
    if kind == ToolEventKind.FINAL_DONE:
        return ToolLoopEvent(kind, {"content": draw(st.sampled_from(("", "done", None)))})
    if kind == ToolEventKind.ERROR:
        return ToolLoopEvent(kind, {"message": draw(st.sampled_from(("err", "")))})
    if kind == ToolEventKind.STREAM_DONE:
        tool_calls = draw(st.one_of(st.just([]), pending_tool_call_list(min_size=1, max_size=2)))
        return ToolLoopEvent(kind, {"response": {"tool_calls": tool_calls, "content": "", "finish_reason": "stop"}})
    if kind == ToolEventKind.TOOL_RESULT:
        return ToolLoopEvent(
            kind,
            {
                "call_id": "c1",
                "func_name": "noop",
                "func_args_str": "{}",
                "result": '{"status": "ok"}',
            },
        )
    return ToolLoopEvent(kind, {})


@st.composite
def send_handler_states(draw):
    max_rounds = draw(st.integers(min_value=1, max_value=8))
    return SendHandlerState(
        handler_type=draw(st.sampled_from(("agent", "audio", "image", "web"))),
        status=draw(st.sampled_from(("ready", "starting", "running", "done", "stopped"))),
        query_text=draw(st.sampled_from(("", "hi", "q"))),
        round_num=draw(st.integers(min_value=0, max_value=max_rounds)),
        pending_tools=(),
        max_rounds=max_rounds,
    )


@st.composite
def send_handler_events(draw):
    kind = draw(st.sampled_from(("stop", "chunk", "done", "start", "error")))
    if kind == "stop":
        return StopRequestedEvent()
    if kind == "chunk":
        return StreamChunkEvent(chunk_text=draw(st.sampled_from(("x", ""))), is_thinking=draw(st.booleans()))
    if kind == "done":
        return StreamDoneEvent(response=None)
    if kind == "start":
        return StartEvent(
            query_text=draw(st.sampled_from(("hi", ""))),
            model=None,
            doc_type_str=draw(st.sampled_from(("writer", "calc", ""))),
        )
    return ErrorEvent(error=RuntimeError("boom"), context="hypothesis")


@st.composite
def mcp_states(draw):
    return MCPState(
        status=draw(st.sampled_from(list(MCPStateStr))),
        tool_name=draw(st.sampled_from((None, "ping", "read"))),
        arguments={},
        is_error=draw(st.booleans()),
    )


@st.composite
def mcp_events(draw):
    kind = draw(st.sampled_from(list(MCPEventKind)))
    if kind == MCPEventKind.REQUEST_RECEIVED:
        tool = draw(st.sampled_from((None, "", "ping", "echo")))
        data: dict = {"arguments": {}, "is_long_running": draw(st.booleans())}
        if tool is not None:
            data["tool_name"] = tool
        return MCPEvent(kind=kind, data=data)
    if kind == MCPEventKind.TOOL_COMPLETED:
        return MCPEvent(kind=kind, data={"result": draw(st.sampled_from(({"ok": True}, {"status": "error"})))})
    if kind == MCPEventKind.REQUEST_ERROR:
        return MCPEvent(kind=kind, data={"message": "fail", "code": draw(st.sampled_from(("X", "INTERNAL_ERROR")))})
    return MCPEvent(kind=kind, data={})
