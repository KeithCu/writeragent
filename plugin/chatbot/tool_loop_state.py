import dataclasses
import json
from enum import Enum, auto
from typing import Any, Dict, List, Mapping, Optional, NamedTuple

from plugin.framework.service import BaseState, FsmTransition
from plugin.chatbot.state_machine import UIEffectKind
from plugin.chatbot.memory import format_upsert_memory_chat_line
from plugin.framework.client.stream_normalizer import reasoning_replay_from_assistant_response

# Short sidebar chat labels for delegate_to_specialized_*_toolset gateway tools.
DELEGATE_GATEWAY_TOOL_NAMES = frozenset(
    {
        "delegate_to_specialized_writer_toolset",
        "delegate_to_specialized_calc_toolset",
        "delegate_to_specialized_draw_toolset",
    }
)
DELEGATE_TASK_CHAT_MAX = 120
_EMPTY_MODEL_DEBUG_CONTENT_PREVIEW_MAX = 120


def _describe_empty_response_content(content: Any) -> str:
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


def _describe_empty_response_tool_calls(tool_calls: Any) -> str:
    if tool_calls is None:
        return "none"
    if isinstance(tool_calls, list):
        return str(len(tool_calls))
    return "present"


def format_empty_model_response_debug(round_num: int, response: Mapping[str, Any]) -> str:
    """Compact API summary for sidebar when STREAM_DONE has no content and no tools."""
    parts = [
        f"round={round_num}",
        f"finish_reason={response.get('finish_reason')!r}",
        f"content={_describe_empty_response_content(response.get('content'))}",
        f"tool_calls={_describe_empty_response_tool_calls(response.get('tool_calls'))}",
    ]
    usage = response.get("usage")
    if isinstance(usage, dict) and usage:
        parts.append(f"usage={json.dumps(usage, separators=(',', ':'))}")
    images = response.get("images")
    if isinstance(images, list) and images:
        parts.append(f"images={len(images)}")
    return ", ".join(parts)


def is_delegate_gateway(func_name: str) -> bool:
    return func_name in DELEGATE_GATEWAY_TOOL_NAMES


def domain_from_delegate_args(func_args: Mapping[str, Any]) -> str:
    domain = func_args.get("domain")
    if isinstance(domain, str) and domain.strip():
        return domain.strip()
    return "?"


def delegate_status_label(func_args: Mapping[str, Any]) -> str:
    return f"delegate ({domain_from_delegate_args(func_args)})"


def _truncate_delegate_task(task: str, max_len: int = DELEGATE_TASK_CHAT_MAX) -> str:
    one_line = task.replace("\n", " ").replace("\r", " ").strip()
    if len(one_line) <= max_len:
        return one_line
    return one_line[: max_len - 3] + "..."


def format_delegate_running_chat_line(func_args: Mapping[str, Any]) -> str:
    """One-line chat preview when a delegate gateway tool starts."""
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


def format_delegate_result_chat_line(func_args: Mapping[str, Any], result_data: Mapping[str, Any]) -> str:
    """Completion line for delegate gateway tools (domain shown; success is short)."""
    domain = domain_from_delegate_args(func_args)
    if result_data.get("status") == "error":
        error_msg = result_data.get("message", "Unknown error")
        return f"[delegate ({domain}) failed: {error_msg}]\n"
    from plugin.chatbot.web_research_chat import format_research_cache_result_chat

    cache_block = format_research_cache_result_chat(result_data) if domain == "web_research" else ""
    return cache_block + f"[delegate ({domain}): done]\n"


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
    kind: UIEffectKind
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


# --- State Machine Transition ---
def next_state(state: ToolLoopState, event: ToolLoopEvent) -> FsmTransition[ToolLoopState]:
    """Pure transition function for the tool-calling loop."""
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

            effects.append(LogAgentEffect(location="chat_panel.py:tool_round", message="Tool loop round response", data={"round": state.round_num, "has_tool_calls": bool(tool_calls), "num_tool_calls": len(tool_calls) if tool_calls else 0}, hypothesis_id="A"))

            if not tool_calls:
                effects.append(LogAgentEffect(location="chat_panel.py:exit_no_tools", message="Exiting loop: no tool_calls", data={"round": state.round_num}, hypothesis_id="A"))
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
                    effects.append(ToolLoopUIEffect(kind="append", text="\n[Response truncated -- the model ran out of tokens...]\n"))
                elif finish_reason == "content_filter":
                    effects.append(ToolLoopUIEffect(kind="append", text="\n[Content filter: response was truncated.]\n"))
                else:
                    effects.append(ToolLoopUIEffect(kind="append", text="\n[No text from model; any tool changes were still applied.]\n"))
                    effects.append(
                        ToolLoopUIEffect(
                            kind="append",
                            text=f"\n[Debug: {format_empty_model_response_debug(state.round_num, response)}]\n",
                        )
                    )

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
                    effects.append(LogAgentEffect(location="chat_panel.py:exit_exhausted", message="Exiting loop: exhausted max_tool_rounds", data={"rounds": state.max_rounds}, hypothesis_id="A"))
                    effects.append(SpawnFinalStreamEffect())
                    capped_round_num = max(state.round_num, state.max_rounds)
                    return FsmTransition(dataclasses.replace(state, round_num=capped_round_num), effects)
                else:
                    effects.append(SpawnLLMWorkerEffect(round_num=new_round_num))
                    return FsmTransition(dataclasses.replace(state, round_num=new_round_num), effects)

            else:
                tc = state.pending_tools[0]
                if not isinstance(tc, dict):
                    tc = {}
                func_data = tc.get("function", {})
                if not isinstance(func_data, dict):
                    func_data = {}

                func_name = func_data.get("name", "unknown")
                func_args_str = func_data.get("arguments", "{}")
                call_id = tc.get("id", "")

                from plugin.framework.errors import safe_json_loads

                func_args = safe_json_loads(func_args_str) if func_args_str else {}
                if not isinstance(func_args, dict):
                    func_args = {}

                if is_delegate_gateway(func_name):
                    status_text = f"Running: {delegate_status_label(func_args)}"
                    run_line = format_delegate_running_chat_line(func_args)
                elif func_name == "upsert_memory":
                    status_text = f"Running: {func_name}"
                    run_line = format_upsert_memory_chat_line(func_args)
                else:
                    status_text = f"Running: {func_name}"
                    run_line = f"[Running tool: {func_name}...]\n"
                effects.append(ToolLoopUIEffect(kind="status", text=status_text))
                # web_research: chat shows internal DuckDuckGo `web_search` steps only (see
                # web_research.py + web_research_chat.py), not a separate outer research banner.
                effects.append(ToolLoopUIEffect(kind="append", text=run_line))
                effects.append(UpdateActivityStateEffect(action="tool_execute", round_num=state.round_num, tool_name=func_name))

                effects.append(LogAgentEffect(location="chat_panel.py:tool_execute", message="Executing tool", data={"tool": func_name, "round": state.round_num}, hypothesis_id="C,D,E"))
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

            result_data = safe_json_loads(result) if result else {}
            if not isinstance(result_data, dict):
                result_data = {}

            effects.append(ToolLoopUIEffect(kind="debug", text=f"Tool result: {result}"))

            func_args = safe_json_loads(func_args_str) if func_args_str else {}
            if not isinstance(func_args, dict):
                func_args = {}

            if result_data.get("status") == "error":
                import json

                error_msg = result_data.get("message", "Unknown error")
                details = result_data.get("details", {})

                if is_delegate_gateway(func_name):
                    detailed_text = format_delegate_result_chat_line(func_args, result_data)
                else:
                    detailed_text = f"[{func_name} failed: {error_msg}]\n"
                if details:
                    tb = details.pop("traceback", None)
                    if details:
                        detailed_text += f"Details: {json.dumps(details, indent=2)}\n"
                    if tb and tb.strip() != "NoneType: None":
                        detailed_text += f"Traceback:\n{tb}\n"

                effects.append(ToolLoopUIEffect(kind="append", text=detailed_text))
                note = error_msg
            else:
                note = result_data.get("message", result_data.get("status", "done"))
                if is_delegate_gateway(func_name):
                    effects.append(ToolLoopUIEffect(kind="append", text=format_delegate_result_chat_line(func_args, result_data)))
                elif func_name == "web_research":
                    from plugin.chatbot.web_research_chat import format_research_cache_result_chat

                    cache_block = format_research_cache_result_chat(result_data)
                    effects.append(ToolLoopUIEffect(kind="append", text=cache_block + f"[{func_name}: {note}]\n"))
                else:
                    effects.append(ToolLoopUIEffect(kind="append", text=f"[{func_name}: {note}]\n"))

            if func_name == "apply_document_content" and isinstance(note, str) and note.strip().startswith("Replaced 0 occurrence"):
                params_display = func_args_str if len(func_args_str) <= 800 else func_args_str[:800] + "..."
                effects.append(ToolLoopUIEffect(kind="append", text=f"[Debug: params {params_display}]\n"))

            effects.append(AddMessageEffect(role="tool", call_id=call_id, content=result))

            is_success = result_data.get("success") is True or result_data.get("status") == "ok"
            if is_success and mutates_document:
                effects.append(UpdateDocumentContextEffect())

            effects.append(TriggerNextToolEffect())
            return FsmTransition(state, effects)

    return FsmTransition(state, effects)
