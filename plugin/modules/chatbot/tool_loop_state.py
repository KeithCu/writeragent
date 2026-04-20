import dataclasses
from enum import Enum, auto
from typing import Any, Dict, List, Optional, NamedTuple

from plugin.framework.state import BaseState, FsmTransition
from plugin.framework.types import UIEffectKind
from plugin.modules.chatbot.memory import format_upsert_memory_chat_line

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
    role: str # "assistant" or "tool"
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    call_id: Optional[str] = None

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
                effects.append(AddMessageEffect(role="assistant", content=content))
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

            effects.append(LogAgentEffect(
                location="chat_panel.py:tool_round",
                message="Tool loop round response",
                data={
                    "round": state.round_num,
                    "has_tool_calls": bool(tool_calls),
                    "num_tool_calls": len(tool_calls) if tool_calls else 0,
                },
                hypothesis_id="A"
            ))

            if not tool_calls:
                effects.append(LogAgentEffect(
                    location="chat_panel.py:exit_no_tools",
                    message="Exiting loop: no tool_calls",
                    data={"round": state.round_num},
                    hypothesis_id="A"
                ))
                if content:
                    effects.append(ToolLoopUIEffect(kind="debug", text="Tool loop: Adding assistant message to session"))
                    effects.append(AddMessageEffect(role="assistant", content=content))
                    effects.append(ToolLoopUIEffect(kind="append", text="\n"))
                elif finish_reason == "length":
                    effects.append(ToolLoopUIEffect(kind="append", text="\n[Response truncated -- the model ran out of tokens...]\n"))
                elif finish_reason == "content_filter":
                    effects.append(ToolLoopUIEffect(kind="append", text="\n[Content filter: response was truncated.]\n"))
                else:
                    effects.append(ToolLoopUIEffect(kind="append", text="\n[No text from model; any tool changes were still applied.]\n"))

                effects.append(ToolLoopUIEffect(kind="status", text="Ready"))
                effects.append(ExitLoopEffect())
                return FsmTransition(dataclasses.replace(state, status="Ready"), effects)

            else:
                effects.append(AddMessageEffect(role="assistant", content=content, tool_calls=tool_calls))
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
                    effects.append(LogAgentEffect(
                        location="chat_panel.py:exit_exhausted",
                        message="Exiting loop: exhausted max_tool_rounds",
                        data={"rounds": state.max_rounds},
                        hypothesis_id="A"
                    ))
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

                effects.append(ToolLoopUIEffect(kind="status", text=f"Running: {func_name}"))
                # web_research: chat shows internal DuckDuckGo `web_search` steps only (see
                # web_research.py + web_research_chat.py), not a separate outer research banner.
                if func_name == "upsert_memory":
                    run_line = format_upsert_memory_chat_line(func_args)
                else:
                    run_line = f"[Running tool: {func_name}...]\n"
                effects.append(
                    ToolLoopUIEffect(
                        kind="append",
                        text=run_line,
                    )
                )
                effects.append(UpdateActivityStateEffect(action="tool_execute", round_num=state.round_num, tool_name=func_name))

                effects.append(LogAgentEffect(
                    location="chat_panel.py:tool_execute",
                    message="Executing tool",
                    data={"tool": func_name, "round": state.round_num},
                    hypothesis_id="C,D,E"
                ))
                effects.append(ToolLoopUIEffect(kind="debug", text=f"Tool call: {func_name}({func_args_str})"))

                is_async = func_name in state.async_tools
                effects.append(SpawnToolWorkerEffect(
                    call_id=call_id,
                    func_name=func_name,
                    func_args_str=func_args_str,
                    func_args=func_args,
                    is_async=is_async
                ))

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

            if result_data.get("status") == "error":
                import json
                error_msg = result_data.get("message", "Unknown error")
                details = result_data.get("details", {})

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
