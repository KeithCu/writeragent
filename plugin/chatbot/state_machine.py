"""Pure state machine for chat sidebar send handlers."""

import dataclasses
from dataclasses import dataclass
from typing import List, Any, Optional, NamedTuple, Literal

from plugin.framework.client.errors import format_error_for_display
from plugin.framework.deal_shim import DEAL_MAX_SOURCE, DEAL_MAX_TOKEN, str_bounded, deal
from plugin.framework.service import BaseState, FsmTransition

# Send-handler FSM status and kind
SendHandlerKind = Literal["image", "agent", "web"]
SendHandlerFsmStatus = Literal["ready", "starting", "running", "done", "error", "stopped"]
# CompleteJobEffect.terminal_status (UI / job completion; capitalized)
SendHandlerCompleteStatus = Literal["Error", "Stopped", "Ready"]

# Tool-loop and send-handler UI channel effects (see ToolLoopUIEffect, SendHandlerUIEffect)
UIEffectKind = Literal["append", "status", "debug", "info"]

# 1. Define State (frozen dataclass)


@dataclass(frozen=True)
class SendHandlerState(BaseState):
    # str (not Literal aliases) — CrossHair cannot proxy typing.Literal in synthesized fields.
    handler_type: str
    status: str
    query_text: str = ""
    model: Any = None
    doc_type_str: str = ""

    # Simple error info
    last_error: Optional[str] = None
    error_time: Optional[float] = None


# 2. Define Events


class StartEvent(NamedTuple):
    query_text: str
    model: Any
    doc_type_str: str
    wav_path: Optional[str] = None
    stt_model: Optional[str] = None


class StreamChunkEvent(NamedTuple):
    chunk_text: str
    is_thinking: bool = False


class StreamDoneEvent(NamedTuple):
    response: Any = None


class ErrorEvent(NamedTuple):
    error: Exception
    context: str = "state_machine"
    error_time: Optional[float] = None


class StopRequestedEvent(NamedTuple):
    pass


class ToolResultEvent(NamedTuple):
    tool_id: str
    result: dict[str, Any]


SendHandlerEvent = StartEvent | StreamChunkEvent | StreamDoneEvent | ErrorEvent | StopRequestedEvent | ToolResultEvent

# 3. Define Effects (Commands)


class SpawnDirectImageEffect(NamedTuple):
    query_text: str
    model: Any


class SpawnAgentWorkerEffect(NamedTuple):
    query_text: str
    model: Any
    doc_type_str: str


class SpawnWebWorkerEffect(NamedTuple):
    query_text: str
    model: Any


class SendHandlerUIEffect(NamedTuple):
    kind: str  # UIEffectKind — str for CrossHair cover (Literal is not proxyable)
    text: str
    is_thinking: bool = False
    role: str = "assistant"


class CompleteJobEffect(NamedTuple):
    terminal_status: str  # SendHandlerCompleteStatus — str for CrossHair cover


SendHandlerEffect = SpawnDirectImageEffect | SpawnAgentWorkerEffect | SpawnWebWorkerEffect | SendHandlerUIEffect | CompleteJobEffect

# 5. Effect Interpreter Interface/Placeholder
# The EffectInterpreter class executes the side effects returned by next_state.
# It will be instantiated and called by SendHandlersMixin in send_handlers.py.


class EffectInterpreter:
    handler: Any

    def __init__(self, handler_mixin: Any) -> None:
        self.handler = handler_mixin
        self.current_state: SendHandlerState | None = None

    def interpret(self, effect: SendHandlerEffect) -> None:
        # crosshair: off
        match effect:
            case SendHandlerUIEffect("append", text, is_thinking, role):
                # Pass is_thinking through. StreamChunkEvent already carries
                # it; dropping the flag appends a web-research THINKING chunk
                # (show_search_thinking on) as a normal assistant row.
                self.handler._append_response(text, is_thinking=is_thinking, role=role)
            case SendHandlerUIEffect("status", text, _):
                self.handler._set_status(text)
            case CompleteJobEffect(terminal_status=status):
                self.handler._terminal_status = status
                if status not in ("Error", "Stopped"):
                    self.handler._terminal_status = "Ready"
                    self.handler._set_status("Ready")
            case SpawnDirectImageEffect(query_text=qt, model=mod):
                self.handler._execute_direct_image_effect(qt, mod, self.current_state, self)
            case SpawnAgentWorkerEffect(query_text=qt, model=mod, doc_type_str=dts):
                self.handler._execute_agent_backend_effect(qt, mod, dts, self.current_state, self)
            case SpawnWebWorkerEffect(query_text=qt, model=mod):
                self.handler._execute_web_research_effect(qt, mod, self.current_state, self)
            case _:
                # SendHandlerUIEffect kinds beyond append/status (and future effects): no-op.
                pass


# 4. Pure helpers + transition


# Names only — isinstance(e, (NamedTuple, ...)) crash-frames CrossHair on symbolic objects.
_SPAWN_EFFECT_TYPE_NAMES = frozenset(
    {
        "SpawnDirectImageEffect",
        "SpawnAgentWorkerEffect",
        "SpawnWebWorkerEffect",
    }
)


@deal.post(lambda result: type(result) is bool)
def stop_effects_exclude_spawns(effects: object) -> bool:
    """True when *effects* contain no image/agent/web spawn workers (STOP invariant)."""
    if type(effects) is not list and type(effects) is not tuple:
        return True
    for e in effects:
        if type(e).__name__ in _SPAWN_EFFECT_TYPE_NAMES:
            return False
    return True


@deal.pre(
    lambda handler_type, err_msg: str_bounded(handler_type, DEAL_MAX_TOKEN)
    and str_bounded(err_msg, DEAL_MAX_SOURCE)
)
@deal.post(lambda result: isinstance(result, tuple) and len(result) == 2 and type(result[0]) is str and type(result[1]) is str and result[0] == "Error" and result[1].endswith("\n"))
def ui_lines_for_handler_error(handler_type: str, err_msg: str) -> tuple[str, str]:
    """Status + append chat lines for a send-handler error (string message, not Exception)."""
    if type(handler_type) is not str:
        handler_type = ""
    if type(err_msg) is not str:
        err_msg = ""
    if handler_type == "web":
        append = f"\n[Research Chat error: {err_msg}]\n"
    else:
        append = f"\n[Operation failed: {err_msg}]\n"
    return "Error", append


@deal.pre(
    lambda handler_type, query_text, model, doc_type_str: (
        str_bounded(handler_type, DEAL_MAX_TOKEN)
        and str_bounded(query_text, DEAL_MAX_SOURCE)
        and str_bounded(doc_type_str, DEAL_MAX_TOKEN)
    )
)
@deal.post(lambda result: isinstance(result, list))
def spawn_effects_for_start(
    handler_type: str,
    query_text: str,
    model: Any,
    doc_type_str: str,
) -> list[SendHandlerEffect]:
    """UI + spawn effects for a StartEvent, keyed by handler_type. No I/O."""
    effects: List[SendHandlerEffect] = []
    if handler_type == "image":
        effects.append(SendHandlerUIEffect("append", query_text, role="user"))
        effects.append(SendHandlerUIEffect("append", "\n[Using image model (direct).]\n"))
        effects.append(SendHandlerUIEffect("append", "AI: Creating image...\n"))
        effects.append(SendHandlerUIEffect("status", "Creating image..."))
        effects.append(SpawnDirectImageEffect(query_text, model))
    elif handler_type == "agent":
        # The user line is painted next to add_user_message, after the
        # backend exists. Painting it here left a You: row on screen when
        # the adapter was missing and history had no user turn.
        effects.append(SendHandlerUIEffect("append", "\n[Using external agent backend.]\n"))
        effects.append(SendHandlerUIEffect("append", "AI: "))
        effects.append(SendHandlerUIEffect("status", "Starting agent..."))
        effects.append(SpawnAgentWorkerEffect(query_text, model, doc_type_str))
    elif handler_type == "web":
        effects.append(SendHandlerUIEffect("append", query_text, role="user"))
        effects.append(SendHandlerUIEffect("status", "Starting research..."))
        effects.append(SpawnWebWorkerEffect(query_text, model))
    return effects


def handle_error(state: SendHandlerState, event: ErrorEvent) -> FsmTransition[SendHandlerState]:
    """Simple error handling - transition to error state"""
    # crosshair: off
    effects: List[SendHandlerEffect] = []

    err_msg = format_error_for_display(event.error)
    status_text, append_text = ui_lines_for_handler_error(state.handler_type, err_msg)
    effects.append(SendHandlerUIEffect("status", status_text))
    effects.append(SendHandlerUIEffect("append", append_text))
    effects.append(CompleteJobEffect("Error"))

    new_state = dataclasses.replace(state, status="error", last_error=str(event.error), error_time=event.error_time)

    return FsmTransition(new_state, effects)


@deal.ensure(lambda state, event, result: (not isinstance(event, StopRequestedEvent)) or stop_effects_exclude_spawns(result.effects))
def next_state(state: SendHandlerState, event: SendHandlerEvent) -> FsmTransition[SendHandlerState]:
    """Pure state transition - NO SIDE EFFECTS"""
    # crosshair: off
    if state.status == "error":
        return FsmTransition(state, [])

    effects: List[SendHandlerEffect] = []

    match event:
        case StopRequestedEvent():
            # The stop line is a message the turn writes. Painting it here
            # by handler type made agent Stop a special case.
            effects.append(SendHandlerUIEffect("status", "Stopped"))
            effects.append(CompleteJobEffect("Stopped"))
            return FsmTransition(dataclasses.replace(state, status="stopped"), effects)

        case ErrorEvent():
            return handle_error(state, event)

        case StreamChunkEvent(chunk_text=text, is_thinking=thinking):
            effects.append(SendHandlerUIEffect("append", text, is_thinking=thinking))
            return FsmTransition(state, effects)

        case StreamDoneEvent():
            if state.status in ("error", "stopped"):
                return FsmTransition(state, effects)

            if state.handler_type in ("image", "agent", "web"):
                effects.append(SendHandlerUIEffect("status", "Ready"))
                effects.append(CompleteJobEffect("Ready"))

            return FsmTransition(dataclasses.replace(state, status="done"), effects)

        case StartEvent(query_text=q_text, model=mod, doc_type_str=doc_type):
            effects.extend(spawn_effects_for_start(state.handler_type, q_text, mod, doc_type))
            new_state = dataclasses.replace(state, status="starting", query_text=q_text, model=mod, doc_type_str=doc_type)
            return FsmTransition(new_state, effects)

        case _:
            # ToolResultEvent belongs to the tool-loop FSM; ignore here.
            pass

    return FsmTransition(state, effects)
