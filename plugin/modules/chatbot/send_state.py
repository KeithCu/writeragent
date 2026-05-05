from dataclasses import dataclass
from typing import List, NamedTuple, Union
# import deal

from enum import Enum, auto

from plugin.framework.state import BaseState, FsmTransition

# Imperative effects for the send panel interpreter (distinct from
# audio_recorder_state.StartRecordingEffect / StopRecordingEffect types).

# --- State ---


@dataclass(frozen=True)
class SendButtonState(BaseState):
    is_busy: bool  # True when AI is generating (or transcribing)
    is_recording: bool  # True when audio is actively being recorded
    has_text: bool  # True when the query text area is non-empty
    has_audio: bool  # True when a recorded audio file exists and is ready to send
    audio_supported: bool  # True if audio recording feature is available on the platform


# --- Events ---


class SendEventKind(Enum):
    TEXT_UPDATED = auto()
    RECORD_CLICKED = auto()
    STOP_REC_CLICKED = auto()
    SEND_CLICKED = auto()
    STOP_CLICKED = auto()
    SEND_COMPLETED = auto()
    ERROR_OCCURRED = auto()


class SendEvent(NamedTuple):
    kind: SendEventKind
    data: dict = {}


# --- Effects ---


@dataclass(frozen=True)
class UpdateUIEffect:
    send_enabled: bool
    stop_enabled: bool
    send_label: str
    status_text: str


@dataclass(frozen=True)
class StartRecordingEffect:
    pass


@dataclass(frozen=True)
class StopRecordingEffect:
    pass


@dataclass(frozen=True)
class StartSendEffect:
    pass


@dataclass(frozen=True)
class StopSendEffect:
    pass


SendEffects = Union[
    UpdateUIEffect,
    StartRecordingEffect,
    StopRecordingEffect,
    StartSendEffect,
    StopSendEffect,
]


# --- Pure Transition Function ---


# Helper to determine the button label
def _get_send_label(state: SendButtonState) -> str:
    if state.is_recording:
        return "Stop Rec"
    if state.has_text or state.has_audio:
        return "Send"
    return "Record" if state.audio_supported else "Send"


# Contract: Send and Stop button states are mutually exclusive
# @deal.ensure(lambda state, event, result:
#              not (result[0].is_busy and result[0].is_recording))
# @deal.ensure(lambda state, event, result:
#              not any(isinstance(e, UpdateUIEffect) and e.send_enabled and e.stop_enabled for e in result[1]))
# @deal.ensure(lambda state, event, result:
#              all(not getattr(e, 'send_enabled') or not result[0].is_busy
#                  for e in result[1] if isinstance(e, UpdateUIEffect)))
# @deal.ensure(lambda state, event, result:
#              all(not getattr(e, 'stop_enabled') or result[0].is_busy
#                  for e in result[1] if isinstance(e, UpdateUIEffect)))
def next_state(state: SendButtonState, event: SendEvent) -> FsmTransition[SendButtonState]:
    """Pure state transition for the Send button."""

    effects: List[SendEffects] = []

    if event.kind == SendEventKind.TEXT_UPDATED:
        new_state = SendButtonState(
            is_busy=state.is_busy, is_recording=state.is_recording, has_text=event.data.get("has_text", False), has_audio=state.has_audio, audio_supported=state.audio_supported
        )
        # If currently recording, do not toggle back to Record
        send_enabled = not new_state.is_busy
        stop_enabled = new_state.is_busy
        label = _get_send_label(new_state)
        # We don't overwrite status text during text update, unless we need to?
        # Typically status text is managed by the send process, but we can pass None or an empty string
        # to indicate "don't change". For pure representation, let's omit status text if not changed,
        # or use the current UI logic: status text is usually set to "Ready" or "" by the caller.
        # But for UI consistency, we emit UpdateUIEffect.
        effects.append(
            UpdateUIEffect(
                send_enabled=send_enabled,
                stop_enabled=stop_enabled,
                send_label=label,
                status_text="",  # We'll let the interpreter ignore empty status_text if it wants, or we define it properly
            )
        )
        return FsmTransition(new_state, effects)

    elif event.kind == SendEventKind.RECORD_CLICKED:
        if state.is_busy or state.is_recording or not state.audio_supported:
            return FsmTransition(state, effects)  # Invalid transition

        new_state = SendButtonState(is_busy=False, is_recording=True, has_text=state.has_text, has_audio=state.has_audio, audio_supported=state.audio_supported)
        effects.append(StartRecordingEffect())
        effects.append(
            UpdateUIEffect(
                send_enabled=True,  # Stop Rec button is essentially the "Send" button being clicked again
                stop_enabled=False,
                send_label="Stop Rec",
                status_text="Recording audio...",
            )
        )
        return FsmTransition(new_state, effects)

    elif event.kind == SendEventKind.STOP_REC_CLICKED:
        if not state.is_recording:
            return FsmTransition(state, effects)

        new_state = SendButtonState(
            is_busy=True,
            is_recording=False,
            has_text=state.has_text,
            has_audio=True,  # Transitioning from Stop Rec means we now have audio
            audio_supported=state.audio_supported,
        )
        effects.append(StopRecordingEffect())
        effects.append(UpdateUIEffect(send_enabled=False, stop_enabled=True, send_label="Send", status_text="Starting..."))
        effects.append(StartSendEffect())
        return FsmTransition(new_state, effects)

    elif event.kind == SendEventKind.SEND_CLICKED:
        if state.is_busy or state.is_recording:
            return FsmTransition(state, effects)
        if not state.has_text and not state.has_audio:
            return FsmTransition(state, effects)

        new_state = SendButtonState(is_busy=True, is_recording=False, has_text=state.has_text, has_audio=state.has_audio, audio_supported=state.audio_supported)
        effects.append(
            UpdateUIEffect(
                send_enabled=False,
                stop_enabled=True,
                send_label="Send",  # Label remains Send, but disabled
                status_text="Starting...",
            )
        )
        effects.append(StartSendEffect())
        return FsmTransition(new_state, effects)

    elif event.kind == SendEventKind.STOP_CLICKED:
        if not state.is_busy:
            return FsmTransition(state, effects)

        # We don't change is_busy yet; the StopSendEffect will trigger the stopping logic
        # which will eventually dispatch SendCompletedEvent or ErrorOccurredEvent
        # However, we can update the status text.
        new_state = state
        effects.append(StopSendEffect())
        effects.append(UpdateUIEffect(send_enabled=False, stop_enabled=True, send_label="Send", status_text="Stopping..."))
        return FsmTransition(new_state, effects)

    elif event.kind == SendEventKind.SEND_COMPLETED:
        if not state.is_busy:
            return FsmTransition(state, effects)

        new_state = SendButtonState(
            is_busy=False,
            is_recording=False,
            has_text=False,  # We assume the text is cleared upon send start or completion
            has_audio=False,  # We assume audio is cleared upon send completion
            audio_supported=state.audio_supported,
        )
        label = _get_send_label(new_state)
        effects.append(UpdateUIEffect(send_enabled=True, stop_enabled=False, send_label=label, status_text="Ready"))
        return FsmTransition(new_state, effects)

    elif event.kind == SendEventKind.ERROR_OCCURRED:
        new_state = SendButtonState(
            is_busy=False,
            is_recording=False,
            has_text=state.has_text,  # Keep text on error so user can retry
            has_audio=state.has_audio,
            audio_supported=state.audio_supported,
        )
        label = _get_send_label(new_state)
        effects.append(UpdateUIEffect(send_enabled=True, stop_enabled=False, send_label=label, status_text="Error"))
        return FsmTransition(new_state, effects)

    return FsmTransition(state, effects)
