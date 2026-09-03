import dataclasses
import json
from enum import Enum, auto
from typing import Any, Dict, List, Mapping, Optional, NamedTuple, cast

from plugin.framework.service import BaseState, FsmTransition
from plugin.chatbot.memory import format_upsert_memory_chat_line
from plugin.framework.client.stream_normalizer import reasoning_replay_from_assistant_response
from plugin.framework.deal_shim import (
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    UNDER_CROSSHAIR,
    ascii_bounded,
    deal,
    str_bounded,
)

# Short sidebar chat labels for delegate_to_specialized_*_toolset gateway tools.
DELEGATE_GATEWAY_TOOL_NAMES = frozenset(
    {
        "delegate_to_specialized_writer_toolset",
        "delegate_to_specialized_calc_toolset",
        "delegate_to_specialized_draw_toolset",
    }
)
DELEGATE_TASK_CHAT_MAX = 120
# Truncate/describe still ~56m after token/dict halves (33211730747); floor to 1.
_DEAL_TRUNCATE_TASK_LEN = 1 if UNDER_CROSSHAIR else DEAL_MAX_SOURCE
_DEAL_TRUNCATE_MAX_LEN = 1 if UNDER_CROSSHAIR else DELEGATE_TASK_CHAT_MAX
_DEAL_EMPTY_TC_LEN = 1 if UNDER_CROSSHAIR else DEAL_MAX_CMD_ARGS
_DEAL_EMPTY_TOKEN_LEN = 1 if UNDER_CROSSHAIR else DEAL_MAX_TOKEN
_DEAL_FUNC_ARG_TOKEN_LEN = 1 if UNDER_CROSSHAIR else DEAL_MAX_TOKEN
_DEAL_FUNC_ARG_DICT_LEN = 1 if UNDER_CROSSHAIR else DEAL_MAX_CMD_ARGS
_EMPTY_MODEL_DEBUG_CONTENT_PREVIEW_MAX = 120


def _describe_empty_response_content(content: Any) -> str:
    # crosshair: off
    # cover-all 33689813185 leftover: Any content (~932 ex). Doable later: str-only preview domain.
    if content is None:
        return "None"
    if content == "":
        return "empty"
    if not isinstance(content, str):
        content = str(content)
    if len(content) <= _EMPTY_MODEL_DEBUG_CONTENT_PREVIEW_MAX:
        return f"{len(content)} chars: {content!r}"
    preview = content[: _EMPTY_MODEL_DEBUG_CONTENT_PREVIEW_MAX - 3]
    return f"{len(content)} chars: {preview!r}..."


@deal.pre(
    lambda tool_calls, *_unused, **__: tool_calls is None
    or (
        type(tool_calls) is list
        and len(tool_calls) <= _DEAL_EMPTY_TC_LEN
        and all(
            type(item) is dict
            # Master dropped item.values() (values are not always tiny strings).
            # Keep that keys-only shape; use the PR CrossHair length caps.
            and len(item) <= _DEAL_EMPTY_TC_LEN
            and all(type(k) is str and ascii_bounded(k, _DEAL_EMPTY_TOKEN_LEN) for k in item)
            for item in tool_calls
        )
    )
)
def _describe_empty_response_tool_calls(tool_calls: Any) -> str:
    # crosshair: off  # dict values still Any; three-line helper. Doable later with value bounds (cover-all 33258921875: 311k lines).
    if tool_calls is None:
        return "none"
    if isinstance(tool_calls, list):
        return str(len(tool_calls))
    return "present"


@deal.pre(
    lambda round_num, response: isinstance(round_num, int)
    and 0 <= round_num <= 10_000
    and isinstance(response, dict)
    and len(response) <= DEAL_MAX_CMD_ARGS
)
@deal.post(lambda result: isinstance(result, str) and "round=" in result)
def format_empty_model_response_debug(round_num: int, response: Mapping[str, Any]) -> str:
    """Compact API summary for sidebar when STREAM_DONE has no content and no tools."""
    # Deep check-all run 32840960268: CHECK ERROR (CrossHair engine traceback) after 1:53.
    # crosshair: off
    parts = [
        f"round={round_num}",
        f"finish_reason={response.get('finish_reason')!r}",
        f"content={_describe_empty_response_content(response.get('content'))}",
        f"tool_calls={_describe_empty_response_tool_calls(response.get('tool_calls'))}",
    ]
    usage = response.get("usage")
    if isinstance(usage, dict) and usage:
        try:
            parts.append(f"usage={json.dumps(usage, separators=(',', ':'))}")
        except Exception:
            parts.append(f"usage={len(usage)} entries")
    images = response.get("images")
    if isinstance(images, list) and images:
        parts.append(f"images={len(images)}")
    return ", ".join(parts)


def is_delegate_gateway(func_name: str) -> bool:
    return func_name in DELEGATE_GATEWAY_TOOL_NAMES


def _deal_func_args_ok_pytest(func_args: object) -> bool:
    return type(func_args) is dict and len(func_args) <= DEAL_MAX_CMD_ARGS and all(
        type(k) is str and ascii_bounded(k, DEAL_MAX_TOKEN) and (v is None or (isinstance(v, str) and str_bounded(v, DEAL_MAX_SOURCE)))
        for k, v in func_args.items()
    )


def _deal_func_args_ok_crosshair(func_args: object) -> bool:
    return type(func_args) is dict and len(func_args) <= _DEAL_FUNC_ARG_DICT_LEN and all(
        type(k) is str and ascii_bounded(k, _DEAL_FUNC_ARG_TOKEN_LEN) and (v is None or (isinstance(v, str) and ascii_bounded(v, _DEAL_FUNC_ARG_TOKEN_LEN)))
        for k, v in func_args.items()
    )


_deal_func_args_ok = _deal_func_args_ok_crosshair if UNDER_CROSSHAIR else _deal_func_args_ok_pytest


@deal.pre(lambda func_args: _deal_func_args_ok(func_args))
def domain_from_delegate_args(func_args: Mapping[str, Any]) -> str:
    # crosshair: off  # Mapping Any domain extract (cover-all 33569420452: 4647 examples / ~634s est despite UNDER_CROSSHAIR dual-profile). Doable later.
    domain = func_args.get("domain")
    if isinstance(domain, str) and domain.strip():
        return domain.strip()
    return "?"


@deal.pre(lambda func_args: _deal_func_args_ok(func_args))
def delegate_status_label(func_args: Mapping[str, Any]) -> str:
    # crosshair: off  # thin wrapper over domain_from_delegate_args (cover-all 33569420452: 2063 examples / ~282s est). Doable later.
    return f"delegate ({domain_from_delegate_args(func_args)})"


@deal.pre(
    lambda task, max_len=DELEGATE_TASK_CHAT_MAX, *_unused, **__: ascii_bounded(task, _DEAL_TRUNCATE_TASK_LEN)
    and type(max_len) is int
    and 1 <= max_len <= _DEAL_TRUNCATE_MAX_LEN
)
def _truncate_delegate_task(task: str, max_len: int = DELEGATE_TASK_CHAT_MAX) -> str:
    one_line = task.replace("\n", " ").replace("\r", " ").strip()
    if len(one_line) <= max_len:
        return one_line
    return one_line[: max_len - 3] + "..."


@deal.pre(
    lambda func_args: isinstance(func_args, dict)
    and len(func_args) <= DEAL_MAX_CMD_ARGS
    and all(not isinstance(k, str) or str_bounded(k, DEAL_MAX_TOKEN) for k in func_args)
    and all(not isinstance(v, str) or str_bounded(v, DEAL_MAX_SOURCE) for v in func_args.values())
)
@deal.post(lambda result: isinstance(result, str) and result.startswith("[Running delegate") and result.endswith("\n"))
def format_delegate_running_chat_line(func_args: Mapping[str, Any]) -> str:
    """One-line chat preview when a delegate gateway tool starts."""
    # Deep check-all run 32840960268: Prev 56:34, 493k lines.
    # crosshair: off
    domain = domain_from_delegate_args(func_args)
    raw_task = func_args.get("task")
    if raw_task is None:
        task_preview = ""
    elif isinstance(raw_task, str):
        task_preview = _truncate_delegate_task(raw_task)
    else:
        task_preview = _truncate_delegate_task(str(raw_task))
    if task_preview:
        return f"[Running delegate ({domain}): {task_preview}]\n"
    return f"[Running delegate ({domain})...]\n"


@deal.pre(
    lambda func_args, result_data: _deal_func_args_ok(func_args)
    and type(result_data) is dict
    and len(result_data) <= _DEAL_FUNC_ARG_DICT_LEN
    and all(type(k) is str and ascii_bounded(k, _DEAL_FUNC_ARG_TOKEN_LEN) for k in result_data)
    and all(v is None or (isinstance(v, str) and ascii_bounded(v, _DEAL_FUNC_ARG_TOKEN_LEN)) for v in result_data.values())
)
def format_delegate_result_chat_line(func_args: Mapping[str, Any], result_data: Mapping[str, Any]) -> str:
    # crosshair: off  # dual Mapping + web_research import (cover-all 33569420452: 2249 examples / ~307s est). Doable later.
    """Completion line for delegate gateway tools (domain shown; success is short)."""
    domain = domain_from_delegate_args(func_args)
    if result_data.get("status") == "error":
        error_msg = result_data.get("message", "Unknown error")
        return f"[delegate ({domain}) failed: {error_msg}]\n"
    from plugin.chatbot.web_research_chat import format_research_cache_result_chat

    cache_block = format_research_cache_result_chat(result_data) if domain == "web_research" else ""
    return cache_block + f"[delegate ({domain}): done]\n"


@deal.post(lambda result: isinstance(result, dict))
def object_dict_or_empty(value: object) -> dict[str, Any]:
    """Return *value* when it is a dict; otherwise ``{}`` (post-JSON coerce)."""
    # crosshair: off
    # Plain dict only — CrossHair AttrDict is isinstance(dict) but .get/items can crash.
    return cast("dict[str, Any]", value) if type(value) is dict else {}


@deal.post(lambda result: isinstance(result, tuple) and len(result) == 3 and all(isinstance(x, str) for x in result))
def pending_tool_call_fields(tc: object) -> tuple[str, str, str]:
    """Normalize a pending tool-call entry to ``(func_name, func_args_str, call_id)``."""
    # crosshair: off
    tc_dict = object_dict_or_empty(tc)
    func_data = object_dict_or_empty(tc_dict.get("function"))
    func_name = func_data.get("name", "unknown")
    func_args_str = func_data.get("arguments", "{}")
    call_id = tc_dict.get("id", "")
    if type(func_name) is not str:
        func_name = "unknown"
    if type(func_args_str) is not str:
        func_args_str = "{}"
    if type(call_id) is not str:
        call_id = ""
    return func_name, func_args_str, call_id


@deal.post(lambda result: isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], str) and isinstance(result[1], str) and result[0] and result[1].endswith("\n"))
def format_tool_running_ui(func_name: str, func_args: Mapping[str, Any]) -> tuple[str, str]:
    """Status bar text and chat run-line when a tool starts executing."""
    # crosshair: off
    if is_delegate_gateway(func_name):
        return f"Running: {delegate_status_label(func_args)}", format_delegate_running_chat_line(func_args)
    if func_name == "upsert_memory":
        return f"Running: {func_name}", format_upsert_memory_chat_line(func_args)
    return f"Running: {func_name}", f"[Running tool: {func_name}...]\n"


@deal.post(lambda result: isinstance(result, str) and result.endswith("\n"))
def format_tool_result_chat_text(func_name: str, func_args: Mapping[str, Any], result_data: Mapping[str, Any]) -> str:
    """Chat append body for a tool result (error or success); does not mutate *result_data*."""
    # crosshair: off
    if result_data.get("status") == "error":
        error_msg = result_data.get("message", "Unknown error")
        if is_delegate_gateway(func_name):
            detailed_text = format_delegate_result_chat_line(func_args, result_data)
        else:
            detailed_text = f"[{func_name} failed: {error_msg}]\n"
        raw_details = result_data.get("details", {})
        # Copy before popping traceback so callers' result_data is not mutated.
        details = dict(raw_details) if isinstance(raw_details, dict) else {}
        if details:
            tb = details.pop("traceback", None)
            if details:
                detailed_text += f"Details: {json.dumps(details, indent=2)}\n"
            if isinstance(tb, str) and tb.strip() and tb.strip() != "NoneType: None":
                detailed_text += f"Traceback:\n{tb}\n"
        return detailed_text

    note = result_data.get("message", result_data.get("status", "done"))
    if is_delegate_gateway(func_name):
        return format_delegate_result_chat_line(func_args, result_data)
    if func_name == "web_research":
        from plugin.chatbot.web_research_chat import format_research_cache_result_chat

        cache_block = format_research_cache_result_chat(result_data)
        return cache_block + f"[{func_name}: {note}]\n"
    return f"[{func_name}: {note}]\n"


@deal.post(lambda result: isinstance(result, bool))
def is_replaced_zero_result(result_data: Mapping[str, Any], note: object) -> bool:
    """True when apply_document_content reported zero replacements (structured or legacy message)."""
    # crosshair: off
    # Plain dict/str only — isinstance(str) is true for CrossHair LazyIntSymbolicStr.
    if type(result_data) is not dict:
        return False
    if result_data.get("replaced_count") == 0:
        return True
    # TODO(follow-up): drop legacy prefix once all callers emit replaced_count.
    if type(note) is str:
        return note.strip().startswith("Replaced 0 occurrence")
    return False


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
    data: Dict[str, Any] = {}


# --- Effects ---
# Control-flow and UI effects use frozen dataclasses (interpreted in tool_loop._execute_effect).


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
    kind: str  # UIEffectKind — str for CrossHair cover (Literal is not proxyable)
    text: str = ""


@dataclasses.dataclass(frozen=True)
class LogAgentEffect:
    location: str
    message: str
    data: Dict[str, Any]
    hypothesis_id: str


@dataclasses.dataclass(frozen=True)
class AddMessageEffect:
    role: str  # "assistant" or "tool"
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    call_id: Optional[str] = None
    reasoning_replay: Optional[Dict[str, Any]] = None


@dataclasses.dataclass(frozen=True)
class UpdateActivityStateEffect:
    action: str
    round_num: Optional[int] = None
    tool_name: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class CleanupAudioEffect:
    pass


@deal.post(lambda result: type(result) is bool)
def stopped_effects_exclude_tool_spawns(state: object, effects: object) -> bool:
    """True unless *state* is stopped and *effects* contain a tool-worker spawn.

    Named legality: stopped-latched pending tools never spawn. NEXT_TOOL while
    ``is_stopped`` must not emit ``SpawnToolWorkerEffect`` (the
    ``or state.is_stopped`` guard). STREAM_DONE after stop may still append
    pending and emit ``TriggerNextToolEffect`` — the interpreter queues
    NEXT_TOOL; the FSM must still not spawn a tool worker.
    """
    if not getattr(state, "is_stopped", False):
        return True
    if type(effects) is not list and type(effects) is not tuple:
        return True
    return not any(isinstance(e, SpawnToolWorkerEffect) for e in effects)


# --- State Machine Transition ---
@deal.pre(lambda state, event: isinstance(state.max_rounds, int) and state.max_rounds > 0 and state.round_num >= 0)
@deal.post(lambda result: result.state.round_num >= 0)
@deal.ensure(
    lambda state, event, result: event.kind != EventKind.STOP_REQUESTED
    or any(isinstance(e, ExitLoopEffect) for e in result.effects)
)
@deal.ensure(lambda state, event, result: event.kind != EventKind.STOP_REQUESTED or result.state.is_stopped)
@deal.ensure(lambda state, event, result: not state.is_stopped or result.state.is_stopped)
@deal.ensure(lambda state, event, result: not state.is_stopped or len(result.state.pending_tools) >= len(state.pending_tools))
@deal.ensure(lambda state, event, result: stopped_effects_exclude_tool_spawns(result.state, result.effects))
@deal.ensure(lambda state, event, result: result.state.round_num <= max(state.round_num + 1, state.max_rounds))
def next_state(state: ToolLoopState, event: ToolLoopEvent) -> FsmTransition[ToolLoopState]:
    """Pure transition function for the tool-calling loop."""
    # crosshair: off
    effects: List[Any] = []

    match event.kind:
        case EventKind.STOP_REQUESTED:
            # Stop mid-stream or stop clicked
            effects.append(AddMessageEffect(role="assistant", content="No response."))
            effects.append(ToolLoopUIEffect(kind="status", text="Stopped"))
            effects.append(ToolLoopUIEffect(kind="append", text="\n[Stopped by user]\n"))
            effects.append(ExitLoopEffect())
            return FsmTransition(dataclasses.replace(state, is_stopped=True, status="Stopped"), effects)

        case EventKind.FINAL_DONE:
            content = event.data.get("content")
            if content:
                effects.append(AddMessageEffect(role="assistant", content=content, reasoning_replay=reasoning_replay_from_assistant_response(event.data)))
                effects.append(ToolLoopUIEffect(kind="append", text="\n"))
            effects.append(ToolLoopUIEffect(kind="status", text="Ready"))
            effects.append(ExitLoopEffect())
            return FsmTransition(dataclasses.replace(state, status="Ready"), effects)

        case EventKind.ERROR:
            # The caller handles rendering the actual error message
            effects.append(ExitLoopEffect())
            return FsmTransition(dataclasses.replace(state, status="Error"), effects)

        case EventKind.STREAM_DONE:
            response = event.data.get("response", {})
            has_audio = event.data.get("has_audio", False)
            tool_calls = response.get("tool_calls")
            if isinstance(tool_calls, list) and len(tool_calls) == 0:
                tool_calls = None
            content = response.get("content")
            finish_reason = response.get("finish_reason")

            if not isinstance(tool_calls, list):
                tool_calls = None

            if has_audio:
                effects.append(CleanupAudioEffect())

            effects.append(LogAgentEffect(location="tool_loop.py:tool_round", message="Tool loop round response", data={"round": state.round_num, "has_tool_calls": bool(tool_calls), "num_tool_calls": len(tool_calls) if tool_calls else 0}, hypothesis_id="A"))

            if not tool_calls:
                effects.append(LogAgentEffect(location="tool_loop.py:exit_no_tools", message="Exiting loop: no tool_calls", data={"round": state.round_num}, hypothesis_id="A"))
                if content:
                    effects.append(ToolLoopUIEffect(kind="debug", text="Tool loop: Adding assistant message to session"))
                    effects.append(
                        AddMessageEffect(
                            role="assistant",
                            content=content,
                            reasoning_replay=reasoning_replay_from_assistant_response(response),
                        )
                    )
                    effects.append(ToolLoopUIEffect(kind="append", text="\n"))
                elif finish_reason == "length":
                    # Session must get this banner. Otherwise finalize still sees the
                    # previous HTML assistant and pastes it over the new turn (Packet C
                    # after hello: truncated STREAM_DONE, sidebar showed leftover Mock notes).
                    banner = "[Response truncated -- the model ran out of tokens...]"
                    effects.append(ToolLoopUIEffect(kind="append", text="\n" + banner + "\n"))
                    effects.append(AddMessageEffect(role="assistant", content=banner))
                elif finish_reason == "content_filter":
                    banner = "[Content filter: response was truncated.]"
                    effects.append(ToolLoopUIEffect(kind="append", text="\n" + banner + "\n"))
                    effects.append(AddMessageEffect(role="assistant", content=banner))
                else:
                    banner = "[No text from model; any tool changes were still applied.]"
                    debug = "[Debug: %s]" % format_empty_model_response_debug(state.round_num, response)
                    effects.append(ToolLoopUIEffect(kind="append", text="\n" + banner + "\n"))
                    effects.append(ToolLoopUIEffect(kind="append", text="\n" + debug + "\n"))
                    effects.append(AddMessageEffect(role="assistant", content=banner + "\n" + debug))

                effects.append(ToolLoopUIEffect(kind="status", text="Ready"))
                effects.append(ExitLoopEffect())
                return FsmTransition(dataclasses.replace(state, status="Ready"), effects)

            else:
                effects.append(
                    AddMessageEffect(
                        role="assistant",
                        content=content,
                        tool_calls=tool_calls,
                        reasoning_replay=reasoning_replay_from_assistant_response(response),
                    )
                )
                if content:
                    effects.append(ToolLoopUIEffect(kind="append", text="\n"))

                new_pending_tools = list(state.pending_tools) + tool_calls
                effects.append(TriggerNextToolEffect())
                return FsmTransition(dataclasses.replace(state, pending_tools=new_pending_tools), effects)

        case EventKind.NEXT_TOOL:
            if not state.pending_tools or state.is_stopped:
                if not state.is_stopped:
                    effects.append(ToolLoopUIEffect(kind="status", text="Sending results to AI..."))

                new_round_num = state.round_num + 1
                if new_round_num >= state.max_rounds:
                    effects.append(LogAgentEffect(location="tool_loop.py:exit_exhausted", message="Exiting loop: exhausted max_tool_rounds", data={"rounds": state.max_rounds}, hypothesis_id="A"))
                    effects.append(SpawnFinalStreamEffect())
                    capped_round_num = max(state.round_num, state.max_rounds)
                    return FsmTransition(dataclasses.replace(state, round_num=capped_round_num), effects)
                else:
                    effects.append(SpawnLLMWorkerEffect(round_num=new_round_num))
                    return FsmTransition(dataclasses.replace(state, round_num=new_round_num), effects)

            else:
                func_name, func_args_str, call_id = pending_tool_call_fields(state.pending_tools[0])

                from plugin.framework.errors import safe_json_loads

                func_args = object_dict_or_empty(safe_json_loads(func_args_str) if func_args_str else {})
                status_text, run_line = format_tool_running_ui(func_name, func_args)
                effects.append(ToolLoopUIEffect(kind="status", text=status_text))
                # web_research: chat shows internal DuckDuckGo `web_search` steps only (see
                # web_research.py + web_research_chat.py), not a separate outer research banner.
                effects.append(ToolLoopUIEffect(kind="append", text=run_line))
                effects.append(UpdateActivityStateEffect(action="tool_execute", round_num=state.round_num, tool_name=func_name))

                effects.append(LogAgentEffect(location="tool_loop.py:tool_execute", message="Executing tool", data={"tool": func_name, "round": state.round_num}, hypothesis_id="C,D,E"))
                effects.append(ToolLoopUIEffect(kind="debug", text=f"Tool call: {func_name}({func_args_str})"))

                is_async = func_name in state.async_tools
                effects.append(SpawnToolWorkerEffect(call_id=call_id, func_name=func_name, func_args_str=func_args_str, func_args=func_args, is_async=is_async))

                # The pending tool is consumed
                return FsmTransition(dataclasses.replace(state, pending_tools=state.pending_tools[1:]), effects)

        case EventKind.TOOL_RESULT:
            from plugin.framework.errors import safe_json_loads

            result = event.data.get("result", "")
            func_name = event.data.get("func_name", "")
            func_args_str = event.data.get("func_args_str", "")
            call_id = event.data.get("call_id", "")
            mutates_document = event.data.get("mutates_document", False)

            result_data = object_dict_or_empty(safe_json_loads(result) if result else {})
            effects.append(ToolLoopUIEffect(kind="debug", text=f"Tool result: {result}"))

            func_args = object_dict_or_empty(safe_json_loads(func_args_str) if func_args_str else {})

            if result_data.get("status") == "error":
                note = result_data.get("message", "Unknown error")
            else:
                note = result_data.get("message", result_data.get("status", "done"))
            effects.append(ToolLoopUIEffect(kind="append", text=format_tool_result_chat_text(func_name, func_args, result_data)))

            if func_name == "apply_document_content" and is_replaced_zero_result(result_data, note):
                params_display = func_args_str if len(func_args_str) <= 800 else func_args_str[:800] + "..."
                effects.append(ToolLoopUIEffect(kind="append", text=f"[Debug: params {params_display}]\n"))

            effects.append(AddMessageEffect(role="tool", call_id=call_id, content=result))

            is_success = result_data.get("success") is True or result_data.get("status") == "ok"
            if is_success and mutates_document:
                effects.append(UpdateDocumentContextEffect())

            effects.append(TriggerNextToolEffect())
            return FsmTransition(state, effects)

    return FsmTransition(state, effects)
