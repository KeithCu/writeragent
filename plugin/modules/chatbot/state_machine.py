"""Pure state machine for chat sidebar send handlers."""

import time
import dataclasses
from dataclasses import dataclass
from typing import List, Any, Optional, NamedTuple
from plugin.modules.http.errors import format_error_for_display
from plugin.framework.state import BaseState, FsmTransition
from plugin.framework.types import SendHandlerCompleteStatus, SendHandlerFsmStatus, SendHandlerKind, UIEffectKind

try:
    import deal  # type: ignore
except ImportError:
    # Dummy decorators for production where deal is not installed
    class _DummyDeal:
        @staticmethod
        def pre(func):
            return lambda f: f

        @staticmethod
        def post(func):
            return lambda f: f

        @staticmethod
        def ensure(func):
            return lambda f: f

    deal = _DummyDeal()

# 1. Define State (frozen dataclass)


@dataclass(frozen=True)
class SendHandlerState(BaseState):
    handler_type: SendHandlerKind
    status: SendHandlerFsmStatus
    query_text: str = ""
    model: Any = None
    doc_type_str: str = ""
    round_num: int = 0
    pending_tools: tuple = ()
    max_rounds: int = 10
    recent_effects: tuple = ()

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
    result: dict


SendHandlerEvent = StartEvent | StreamChunkEvent | StreamDoneEvent | ErrorEvent | StopRequestedEvent | ToolResultEvent

# 3. Define Effects (Commands)


class SpawnAudioWorkerEffect(NamedTuple):
    wav_path: str
    stt_model: str
    model: Any
    query_text: str


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
    kind: UIEffectKind
    text: str
    is_thinking: bool = False


class ProceedToChatEffect(NamedTuple):
    combined_text: str
    model: Any
    doc_type_str: str


class CompleteJobEffect(NamedTuple):
    terminal_status: SendHandlerCompleteStatus


SendHandlerEffect = SpawnAudioWorkerEffect | SpawnDirectImageEffect | SpawnAgentWorkerEffect | SpawnWebWorkerEffect | SendHandlerUIEffect | ProceedToChatEffect | CompleteJobEffect

# 5. Effect Interpreter Interface/Placeholder
# The EffectInterpreter class executes the side effects returned by next_state.
# It will be instantiated and called by SendHandlersMixin in send_handlers.py.


class EffectInterpreter:
    def __init__(self, handler_mixin):
        self.handler = handler_mixin
        self.current_state: SendHandlerState | None = None

    def interpret(self, effect: SendHandlerEffect):
        match effect:
            case SendHandlerUIEffect("append", text, _):
                # The _append_response handles thinking implicitly by default args, but we match it
                # For this handler, we just append text. Thinking is checked in the loop.
                self.handler._append_response(text)
            case SendHandlerUIEffect("status", text, _):
                self.handler._set_status(text)
            case CompleteJobEffect(terminal_status=status):
                self.handler._terminal_status = status
                if status not in ("Error", "Stopped"):
                    self.handler._terminal_status = "Ready"
                    self.handler._set_status("Ready")
            case SpawnAudioWorkerEffect(wav_path=wp, stt_model=sm, model=mod, query_text=qt):
                self.handler._execute_audio_effect(wp, sm, mod, qt, self.current_state, self)
            case SpawnDirectImageEffect(query_text=qt, model=mod):
                self.handler._execute_direct_image_effect(qt, mod, self.current_state, self)
            case SpawnAgentWorkerEffect(query_text=qt, model=mod, doc_type_str=dts):
                self.handler._execute_agent_backend_effect(qt, mod, dts, self.current_state, self)
            case SpawnWebWorkerEffect(query_text=qt, model=mod):
                self.handler._execute_web_research_effect(qt, mod, self.current_state, self)
            case ProceedToChatEffect(combined_text=ct, model=mod, doc_type_str=dts):
                self.handler._do_send_chat_with_tools(ct, mod, dts)


# 4. Pure Transition Function


def handle_error(state: SendHandlerState, event: ErrorEvent) -> FsmTransition[SendHandlerState]:
    """Simple error handling - transition to error state"""
    effects: List[SendHandlerEffect] = []

    # Notify user using format_error_for_display
    err_msg = format_error_for_display(event.error)
    effects.append(SendHandlerUIEffect("status", "Error"))

    if state.handler_type == "audio":
        effects.append(SendHandlerUIEffect("append", f"\n[Transcription error: {err_msg}]\n"))
    elif state.handler_type == "web":
        effects.append(SendHandlerUIEffect("append", f"\n[Research Chat error: {err_msg}]\n"))
    else:
        effects.append(SendHandlerUIEffect("append", f"\n[Operation failed: {err_msg}]\n"))

    effects.append(CompleteJobEffect("Error"))

    # Transition to error state
    new_state = dataclasses.replace(state, status="error", last_error=str(event.error), error_time=event.error_time or time.time(), recent_effects=tuple(effects))

    return FsmTransition(new_state, effects)


@deal.pre(lambda state, event: state.round_num <= state.max_rounds)
@deal.post(lambda result: result.state.round_num <= result.state.max_rounds)
@deal.ensure(
    lambda state, event, result: (
        not (isinstance(event, StopRequestedEvent) and any(isinstance(e, (SpawnAudioWorkerEffect, SpawnDirectImageEffect, SpawnAgentWorkerEffect, SpawnWebWorkerEffect)) for e in result.effects))
    )
)
def next_state(state: SendHandlerState, event: SendHandlerEvent) -> FsmTransition[SendHandlerState]:
    """Pure state transition - NO SIDE EFFECTS"""

    if state.status == "error":
        return FsmTransition(state, [])

    effects: List[SendHandlerEffect] = []

    match event:
        case StopRequestedEvent():
            effects.append(SendHandlerUIEffect("status", "Stopped"))
            if state.handler_type == "agent":
                effects.append(SendHandlerUIEffect("append", "\n[Stopped by user]\n"))
            effects.append(CompleteJobEffect("Stopped"))
            new_state = SendHandlerState(
                handler_type=state.handler_type,
                status="stopped",
                query_text=state.query_text,
                model=state.model,
                doc_type_str=state.doc_type_str,
                round_num=state.round_num,
                pending_tools=state.pending_tools,
                max_rounds=state.max_rounds,
                recent_effects=tuple(effects),
            )
            return FsmTransition(new_state, effects)

        case ErrorEvent():
            return handle_error(state, event)

        case StreamChunkEvent(chunk_text=text, is_thinking=thinking):
            effects.append(SendHandlerUIEffect("append", text, is_thinking=thinking))
            new_state = SendHandlerState(
                handler_type=state.handler_type,
                status=state.status,
                query_text=state.query_text,
                model=state.model,
                doc_type_str=state.doc_type_str,
                round_num=state.round_num,
                pending_tools=state.pending_tools,
                max_rounds=state.max_rounds,
                recent_effects=tuple(effects),
            )
            return FsmTransition(new_state, effects)

        case StreamDoneEvent(response=resp):
            if state.status in ("error", "stopped"):
                return FsmTransition(state, effects)

            if state.handler_type == "audio":
                transcript_text = resp if resp else ""
                combined_text = state.query_text
                if transcript_text:
                    combined_text = (combined_text + "\n" + transcript_text).strip() if combined_text else transcript_text

                if combined_text:
                    effects.append(ProceedToChatEffect(combined_text, state.model, state.doc_type_str))
                else:
                    effects.append(SendHandlerUIEffect("status", "Ready"))
                    effects.append(CompleteJobEffect("Ready"))
            elif state.handler_type in ("image", "agent", "web"):
                effects.append(SendHandlerUIEffect("status", "Ready"))
                effects.append(CompleteJobEffect("Ready"))

            new_state = SendHandlerState(
                handler_type=state.handler_type,
                status="done",
                query_text=state.query_text,
                model=state.model,
                doc_type_str=state.doc_type_str,
                round_num=state.round_num,
                pending_tools=state.pending_tools,
                max_rounds=state.max_rounds,
                recent_effects=tuple(effects),
            )
            return FsmTransition(new_state, effects)

        case StartEvent(query_text=q_text, model=mod, doc_type_str=doc_type, wav_path=w_path, stt_model=stt_mod):
            if state.handler_type == "audio":
                effects.append(SendHandlerUIEffect("status", "Transcribing audio..."))
                effects.append(SendHandlerUIEffect("append", "\n[Transcribing audio...]\n"))
                if w_path and stt_mod:
                    effects.append(SpawnAudioWorkerEffect(wav_path=w_path, stt_model=stt_mod, model=mod, query_text=q_text))
            elif state.handler_type == "image":
                effects.append(SendHandlerUIEffect("append", f"\nYou: {q_text}\n"))
                effects.append(SendHandlerUIEffect("append", "\n[Using image model (direct).]\n"))
                effects.append(SendHandlerUIEffect("append", "AI: Creating image...\n"))
                effects.append(SendHandlerUIEffect("status", "Creating image..."))
                effects.append(SpawnDirectImageEffect(q_text, mod))
            elif state.handler_type == "agent":
                effects.append(SendHandlerUIEffect("append", f"\nYou: {q_text}\n"))
                effects.append(SendHandlerUIEffect("append", "\n[Using external agent backend.]\n"))
                effects.append(SendHandlerUIEffect("append", "AI: "))
                effects.append(SendHandlerUIEffect("status", "Starting agent..."))
                effects.append(SpawnAgentWorkerEffect(q_text, mod, doc_type))
            elif state.handler_type == "web":
                effects.append(SendHandlerUIEffect("append", f"\nYou: {q_text}\n"))
                # effects.append(SendHandlerUIEffect("append", "\n[Using research chat.]\n"))
                effects.append(SendHandlerUIEffect("status", "Starting research..."))
                effects.append(SpawnWebWorkerEffect(q_text, mod))

            new_state = SendHandlerState(
                handler_type=state.handler_type,
                status="starting",
                query_text=q_text,
                model=mod,
                doc_type_str=doc_type,
                round_num=state.round_num,
                pending_tools=state.pending_tools,
                max_rounds=state.max_rounds,
                recent_effects=tuple(effects),
            )
            return FsmTransition(new_state, effects)

    return FsmTransition(state, effects)
