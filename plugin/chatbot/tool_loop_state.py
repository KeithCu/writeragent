# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure state machine and effect definitions for the tool-calling loop.

This module contains only pure state transitions and effect descriptions.
No UNO calls, no I/O, no UI formatting, and no safe_json_loads parsing.
UI formatting and side effects are performed by the action interpreter in
plugin.chatbot.tool_loop_actions.
"""

from __future__ import annotations

import dataclasses
from enum import Enum, StrEnum, auto
from typing import Any, Dict, List, Optional, NamedTuple, assert_never, cast

from plugin.framework.client.stream_normalizer import reasoning_replay_from_assistant_response
from plugin.framework.deal_shim import deal
from plugin.framework.service import BaseState, FsmTransition


def object_dict_or_empty(value: object) -> dict[str, Any]:
    """Return *value* when it is a dict; otherwise ``{}`` (post-JSON coerce)."""
    # crosshair: off
    # Plain dict only — CrossHair AttrDict is isinstance(dict) but .get/items can crash.
    return cast("dict[str, Any]", value) if type(value) is dict else {}


def pending_tool_call_fields(tc: object) -> tuple[str, str, str]:
    """Normalize a pending tool-call entry to ``(func_name, func_args_str, call_id)``."""
    # crosshair: off
    tc_dict = object_dict_or_empty(tc)
    func_data = object_dict_or_empty(tc_dict.get("function"))
    func_name = func_data.get("name", "unknown")
    if not isinstance(func_name, str) or not func_name.strip():
        func_name = "unknown"
    func_args_str = func_data.get("arguments", "{}")
    if not isinstance(func_args_str, str):
        func_args_str = "{}"
    call_id = tc_dict.get("id", "")
    if type(call_id) is not str:
        call_id = ""
    return func_name, func_args_str, call_id


def pending_tool_call_args(tc: object) -> dict[str, Any]:
    """Return pre-parsed arguments dict from pending tool call."""
    # crosshair: off
    tc_dict = object_dict_or_empty(tc)
    parsed = tc_dict.get("parsed_args")
    if parsed is None:
        func_data = object_dict_or_empty(tc_dict.get("function"))
        parsed = func_data.get("parsed_args")
    assert isinstance(parsed, dict), f"Pre-parsed arguments missing from pending tool call: {tc!r}"
    return parsed


def with_tool_call_ids(tool_calls: List[Any], round_num: int) -> List[Any]:
    """Copy *tool_calls*, giving each dict with a missing or blank id ``call_<round>_<index>``."""
    # crosshair: off
    out: List[Any] = []
    for index, tc in enumerate(tool_calls):
        if type(tc) is dict:
            call_id = tc.get("id")
            if not (isinstance(call_id, str) and call_id.strip()):
                tc = {**tc, "id": f"call_{round_num}_{index}"}
        out.append(tc)
    return out


@dataclasses.dataclass(frozen=True)
class ToolLoopState(BaseState):
    round_num: int
    pending_tools: List[Dict[str, Any]]
    max_rounds: int
    status: str
    is_stopped: bool = False
    doc_type: str = ""
    async_tools: frozenset[str] = frozenset()


# --- Events ---
# Background threads enqueue tuples whose first element is StreamQueueKind
# (see plugin.framework.async_stream); ToolCallingMixin turns them into
# ToolLoopEvent / EventKind via _create_event_from_stream_item.
class EventKind(Enum):
    STOP_REQUESTED = auto()
    STREAM_DONE = auto()
    NEXT_TOOL = auto()
    TOOL_RESULT = auto()
    FINAL_DONE = auto()
    ERROR = auto()


class ToolLoopEvent(NamedTuple):
    kind: EventKind
    # The omitted value is None. ``data: Dict[str, Any] = {}`` is one dict
    # shared by every event that omits data (NEXT_TOOL), because NamedTuple
    # evaluates that default once. typing.NamedTuple rejects a custom
    # ``__new__``. ``next_state`` treats None as {}.
    data: Optional[Dict[str, Any]] = None


# --- Effects ---
# Control-flow and UI effects use frozen dataclasses (interpreted in tool_loop_actions.ToolLoopEffectInterpreter).


class UIEffectKind(StrEnum):
    APPEND = "append"
    STATUS = "status"
    DEBUG = "debug"
    INFO = "info"
    TOOL_RUNNING = "tool_running"
    TOOL_RESULT = "tool_result"
    EMPTY_MODEL_DEBUG = "empty_model_debug"


class MessageRole(StrEnum):
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclasses.dataclass(frozen=True)
class ExitLoopEffect:
    pass


@dataclasses.dataclass(frozen=True)
class TriggerNextToolEffect:
    pass


@dataclasses.dataclass(frozen=True)
class SpawnFinalStreamEffect:
    pass


@dataclasses.dataclass(frozen=True)
class UpdateDocumentContextEffect:
    pass


@dataclasses.dataclass(frozen=True)
class SpawnLLMWorkerEffect:
    round_num: int


@dataclasses.dataclass(frozen=True)
class SpawnToolWorkerEffect:
    call_id: str
    func_name: str
    func_args_str: str
    func_args: Dict[str, Any]
    is_async: bool


@dataclasses.dataclass(frozen=True)
class ToolLoopUIEffect:
    kind: UIEffectKind
    text: str = ""
    is_empty_model_debug: bool = False
    func_name: str = ""
    func_args: Optional[Dict[str, Any]] = None
    func_args_str: str = ""
    result_data: Optional[Dict[str, Any]] = None
    round_num: int = 0
    response: Optional[Dict[str, Any]] = None


@dataclasses.dataclass(frozen=True)
class LogAgentEffect:
    location: str
    message: str
    data: Dict[str, Any]


@dataclasses.dataclass(frozen=True)
class AddMessageEffect:
    role: MessageRole
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    call_id: Optional[str] = None
    reasoning_replay: Optional[Dict[str, Any]] = None
    round_num: int = 0
    response: Optional[Dict[str, Any]] = None


@dataclasses.dataclass(frozen=True)
class UpdateActivityStateEffect:
    action: str
    round_num: Optional[int] = None
    tool_name: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class CleanupAudioEffect:
    pass


def stopped_effects_exclude_tool_spawns(state: object, effects: object) -> bool:
    """True unless *state* is stopped and *effects* start more loop work.

    NEXT_TOOL while ``is_stopped`` must not emit ``SpawnToolWorkerEffect``,
    ``SpawnLLMWorkerEffect``, or ``SpawnFinalStreamEffect``. STREAM_DONE after
    stop may still append pending and emit ``TriggerNextToolEffect`` — the
    interpreter queues NEXT_TOOL; that NEXT_TOOL exits instead of spawning.
    """
    # crosshair: off
    if not getattr(state, "is_stopped", False):
        return True
    if type(effects) is not list and type(effects) is not tuple:
        return True
    banned = (SpawnToolWorkerEffect, SpawnLLMWorkerEffect, SpawnFinalStreamEffect)
    return not any(isinstance(e, banned) for e in effects)


def _transition_invariants(state: ToolLoopState, event: ToolLoopEvent, result: FsmTransition[ToolLoopState]) -> bool:
    """Stop latch, pending monotonicity, spawn exclusion, and the round bound."""
    # crosshair: off
    if result.state.round_num < 0:
        return False
    if result.state.round_num > max(state.round_num + 1, state.max_rounds):
        return False
    if event.kind == EventKind.STOP_REQUESTED:
        if not result.state.is_stopped:
            return False
        if not any(isinstance(effect, ExitLoopEffect) for effect in result.effects):
            return False
    if state.is_stopped:
        if not result.state.is_stopped:
            return False
        if len(result.state.pending_tools) < len(state.pending_tools):
            return False
        if event.kind == EventKind.NEXT_TOOL and not any(isinstance(effect, ExitLoopEffect) for effect in result.effects):
            return False
    return stopped_effects_exclude_tool_spawns(result.state, result.effects)


# --- State Machine Transition ---
@deal.pre(lambda state, event: isinstance(state.max_rounds, int) and state.max_rounds > 0 and state.round_num >= 0)
@deal.ensure(_transition_invariants)
def next_state(state: ToolLoopState, event: ToolLoopEvent) -> FsmTransition[ToolLoopState]:
    """Pure transition function for the tool-calling loop."""
    # crosshair: off
    # Omitted event data is None. Reads below need a dict.
    event_data = event.data if event.data is not None else {}
    effects: List[Any] = []

    match event.kind:
        case EventKind.STOP_REQUESTED:
            # Stop mid-stream. The turn commits the open row, closes tool
            # calls that have no result, and writes the stop line. This
            # transition only latches the loop. Partial tool_calls never
            # reach this event: the worker does not enqueue them.
            effects.append(ToolLoopUIEffect(kind=UIEffectKind.STATUS, text="Stopped"))
            effects.append(ExitLoopEffect())
            return FsmTransition(dataclasses.replace(state, is_stopped=True, status="Stopped"), effects)

        case EventKind.FINAL_DONE:
            content = event_data.get("content")
            if content:
                effects.append(
                    AddMessageEffect(
                        role=MessageRole.ASSISTANT,
                        content=content,
                        reasoning_replay=reasoning_replay_from_assistant_response(event_data),
                    )
                )
                effects.append(ToolLoopUIEffect(kind=UIEffectKind.APPEND, text="\n"))
            effects.append(ToolLoopUIEffect(kind=UIEffectKind.STATUS, text="Ready"))
            effects.append(ExitLoopEffect())
            return FsmTransition(dataclasses.replace(state, status="Ready"), effects)

        case EventKind.ERROR:
            # The caller handles rendering the actual error message
            effects.append(ExitLoopEffect())
            return FsmTransition(dataclasses.replace(state, status="Error"), effects)

        case EventKind.STREAM_DONE:
            # A non-dict payload is an empty response. ``dict.get`` returns
            # None when the key is present and null, and ``response.get``
            # would then raise inside this pure function. The drain reports
            # that as a stream error.
            response = object_dict_or_empty(event_data.get("response"))
            has_audio = event_data.get("has_audio", False)
            tool_calls = response.get("tool_calls")
            content = response.get("content")
            finish_reason = response.get("finish_reason")

            if not isinstance(tool_calls, list):
                tool_calls = None

            if has_audio:
                effects.append(CleanupAudioEffect())

            effects.append(
                LogAgentEffect(
                    location="tool_loop_state.py:tool_round",
                    message="Tool loop round response",
                    data={
                        "round": state.round_num,
                        "has_tool_calls": bool(tool_calls),
                        "num_tool_calls": len(tool_calls) if tool_calls else 0,
                    },
                )
            )

            if not tool_calls:
                effects.append(
                    LogAgentEffect(
                        location="tool_loop_state.py:exit_no_tools",
                        message="Exiting loop: no tool_calls",
                        data={"round": state.round_num},
                    )
                )
                if content:
                    effects.append(ToolLoopUIEffect(kind=UIEffectKind.DEBUG, text="Tool loop: Adding assistant message to session"))
                    effects.append(
                        AddMessageEffect(
                            role=MessageRole.ASSISTANT,
                            content=content,
                            reasoning_replay=reasoning_replay_from_assistant_response(response),
                        )
                    )
                    effects.append(ToolLoopUIEffect(kind=UIEffectKind.APPEND, text="\n"))
                elif finish_reason == "length":
                    # Session must get this banner. Otherwise finalize still sees the
                    # previous HTML assistant and pastes it over the new turn.
                    banner = "[Response truncated -- the model ran out of tokens...]"
                    effects.append(ToolLoopUIEffect(kind=UIEffectKind.APPEND, text="\n" + banner + "\n"))
                    effects.append(AddMessageEffect(role=MessageRole.ASSISTANT, content=banner))
                elif finish_reason == "content_filter":
                    banner = "[Content filter: response was truncated.]"
                    effects.append(ToolLoopUIEffect(kind=UIEffectKind.APPEND, text="\n" + banner + "\n"))
                    effects.append(AddMessageEffect(role=MessageRole.ASSISTANT, content=banner))
                else:
                    banner = "[No text from model; any tool changes were still applied.]"
                    effects.append(ToolLoopUIEffect(kind=UIEffectKind.APPEND, text="\n" + banner + "\n"))
                    effects.append(
                        ToolLoopUIEffect(
                            kind=UIEffectKind.EMPTY_MODEL_DEBUG,
                            is_empty_model_debug=True,
                            round_num=state.round_num,
                            response=response,
                        )
                    )
                    effects.append(
                        AddMessageEffect(
                            role=MessageRole.ASSISTANT,
                            round_num=state.round_num,
                            response=response,
                        )
                    )

                effects.append(ToolLoopUIEffect(kind=UIEffectKind.STATUS, text="Ready"))
                effects.append(ExitLoopEffect())
                return FsmTransition(dataclasses.replace(state, status="Ready"), effects)

            else:
                # Some providers send tool calls with no id. The tool reply must
                # carry the same id as its call, so fill one in here, before the
                # assistant message and the pending list are built from it.
                tool_calls = with_tool_call_ids(tool_calls, state.round_num)
                effects.append(
                    AddMessageEffect(
                        role=MessageRole.ASSISTANT,
                        content=content,
                        tool_calls=tool_calls,
                        reasoning_replay=reasoning_replay_from_assistant_response(response),
                    )
                )
                if content:
                    effects.append(ToolLoopUIEffect(kind=UIEffectKind.APPEND, text="\n"))

                raw_parsed = event_data.get("parsed_tool_args")
                assert raw_parsed is not None, f"STREAM_DONE event missing parsed_tool_args: {event_data!r}"
                assert len(raw_parsed) == len(tool_calls), (
                    f"STREAM_DONE parsed_tool_args length mismatch: {len(raw_parsed)} != {len(tool_calls)}"
                )
                parsed_tools: List[Dict[str, Any]] = [
                    {**tc, "parsed_args": parsed} if isinstance(tc, dict) else {"parsed_args": parsed}
                    for tc, parsed in zip(tool_calls, raw_parsed)
                ]
                new_pending_tools = list(state.pending_tools) + parsed_tools
                effects.append(TriggerNextToolEffect())
                return FsmTransition(dataclasses.replace(state, pending_tools=new_pending_tools), effects)

        case EventKind.NEXT_TOOL:
            if state.is_stopped:
                # Leave. Do not bump the round or touch pending tools.
                effects.append(ExitLoopEffect())
                return FsmTransition(state, effects)

            if not state.pending_tools:
                effects.append(ToolLoopUIEffect(kind=UIEffectKind.STATUS, text="Sending results to AI..."))

                new_round_num = state.round_num + 1
                if new_round_num >= state.max_rounds:
                    effects.append(
                        LogAgentEffect(
                            location="tool_loop_state.py:exit_exhausted",
                            message="Exiting loop: exhausted max_tool_rounds",
                            data={"rounds": state.max_rounds},
                        )
                    )
                    effects.append(SpawnFinalStreamEffect())
                    capped_round_num = max(state.round_num, state.max_rounds)
                    return FsmTransition(dataclasses.replace(state, round_num=capped_round_num), effects)
                else:
                    effects.append(SpawnLLMWorkerEffect(round_num=new_round_num))
                    return FsmTransition(dataclasses.replace(state, round_num=new_round_num), effects)

            else:
                func_name, func_args_str, call_id = pending_tool_call_fields(state.pending_tools[0])
                func_args = pending_tool_call_args(state.pending_tools[0])

                effects.append(
                    ToolLoopUIEffect(
                        kind=UIEffectKind.TOOL_RUNNING,
                        func_name=func_name,
                        func_args=func_args,
                    )
                )
                effects.append(UpdateActivityStateEffect(action="tool_execute", round_num=state.round_num, tool_name=func_name))

                effects.append(
                    LogAgentEffect(
                        location="tool_loop_state.py:tool_execute",
                        message="Executing tool",
                        data={"tool": func_name, "round": state.round_num},
                    )
                )
                effects.append(ToolLoopUIEffect(kind=UIEffectKind.DEBUG, text=f"Tool call: {func_name}({func_args_str})"))

                is_async = func_name in state.async_tools
                effects.append(
                    SpawnToolWorkerEffect(
                        call_id=call_id,
                        func_name=func_name,
                        func_args_str=func_args_str,
                        func_args=func_args,
                        is_async=is_async,
                    )
                )

                # The pending tool is consumed
                return FsmTransition(dataclasses.replace(state, pending_tools=state.pending_tools[1:]), effects)

        case EventKind.TOOL_RESULT:
            # Unpack tool result fields explicitly without assuming non-None values.
            raw_result = event_data.get("result")
            result = raw_result if isinstance(raw_result, str) else ""
            raw_func_name = event_data.get("func_name")
            func_name = raw_func_name if isinstance(raw_func_name, str) and raw_func_name.strip() else "unknown"
            raw_func_args_str = event_data.get("func_args_str")
            func_args_str = raw_func_args_str if isinstance(raw_func_args_str, str) else ""
            raw_call_id = event_data.get("call_id")
            call_id = raw_call_id if isinstance(raw_call_id, str) else ""
            mutates_document = bool(event_data.get("mutates_document", False))

            raw_result_data = event_data.get("result_data")
            result_data = raw_result_data if isinstance(raw_result_data, dict) else {}

            raw_func_args = event_data.get("func_args")
            func_args = raw_func_args if isinstance(raw_func_args, dict) else {}

            effects.append(ToolLoopUIEffect(kind=UIEffectKind.DEBUG, text=f"Tool result: {result}"))
            effects.append(
                ToolLoopUIEffect(
                    kind=UIEffectKind.TOOL_RESULT,
                    func_name=func_name,
                    func_args=func_args,
                    func_args_str=func_args_str,
                    result_data=result_data,
                )
            )

            effects.append(AddMessageEffect(role=MessageRole.TOOL, call_id=call_id, content=result))

            is_success = result_data.get("success") is True or result_data.get("status") == "ok"
            if is_success and mutates_document:
                effects.append(UpdateDocumentContextEffect())

            effects.append(TriggerNextToolEffect())
            return FsmTransition(state, effects)

    # Closed enum. A new EventKind must fail here instead of a no-op transition.
    assert_never(event.kind)
