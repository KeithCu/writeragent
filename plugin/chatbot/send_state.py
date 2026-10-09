from dataclasses import dataclass
from typing import Any, List, NamedTuple, Union

from enum import Enum, auto

from plugin.framework.service import BaseState, FsmTransition

from plugin.framework.deal_shim import deal

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
    EXTRACTED_SEND = auto()
    STOP_CLICKED = auto()
    CANCEL_REC_CLICKED = auto()
    SEND_COMPLETED = auto()
    ERROR_OCCURRED = auto()


class SendEvent(NamedTuple):
    kind: SendEventKind
    data: dict[str, Any] | None = None


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
class CancelRecordingEffect:
    """Stop capture and delete the take. Nothing is sent."""


@dataclass(frozen=True)
class StartSendEffect:
    pass


@dataclass(frozen=True)
class StopSendEffect:
    pass


SendEffects = Union[UpdateUIEffect, StartRecordingEffect, StopRecordingEffect, CancelRecordingEffect, StartSendEffect, StopSendEffect]


# --- Pure Transition Function ---


# Helper to determine the button label
def _get_send_label(state: SendButtonState) -> str:
    if state.is_recording:
        return "Stop Rec"
    if state.has_text or state.has_audio:
        return "Send"
    return "Record" if state.audio_supported else "Send"


# Contract: while a send is in flight only Stop is enabled, and while idle only
# Send is. A take is the one state with both: Stop Rec (the Send button) sends
# the take, and Stop leaves hands-free or cancels the take.
# Stop stays enabled while recording. That is the step-down exit: with
# Stop off during every take, a hands-free loop has no exit between turns
# (Stop Rec sends and keeps the lock). The contracts below allow exactly
# that state and nothing else.
# Pre rejects the illegal pair so CrossHair cannot start from (busy and recording).
@deal.pre(lambda state, event: not (state.is_busy and state.is_recording))
@deal.ensure(lambda state, event, result: not (result.state.is_busy and result.state.is_recording))
@deal.ensure(
    lambda state, event, result: result.state.is_recording
    or not any(isinstance(e, UpdateUIEffect) and e.send_enabled and e.stop_enabled for e in result.effects)
)
@deal.ensure(
    lambda state, event, result: all(
        (not e.send_enabled) or (not result.state.is_busy) for e in result.effects if isinstance(e, UpdateUIEffect)
    )
)
@deal.ensure(
    lambda state, event, result: all(
        (not e.stop_enabled) or result.state.is_busy or result.state.is_recording
        for e in result.effects
        if isinstance(e, UpdateUIEffect)
    )
)
def next_state(state: SendButtonState, event: SendEvent) -> FsmTransition[SendButtonState]:
    """Pure state transition for the Send button."""
    # event.data is an unbounded dict; Hypothesis covers transitions.
    # crosshair: off
    event_data = event.data or {}

    effects: List[SendEffects] = []

    if event.kind == SendEventKind.TEXT_UPDATED:
        new_state = SendButtonState(is_busy=state.is_busy, is_recording=state.is_recording, has_text=event_data.get("has_text", False), has_audio=state.has_audio, audio_supported=state.audio_supported)
        # Update has_text for when the turn finishes, and emit no UI effect
        # while busy. Typing during web-search approval or in-flight
        # generation would otherwise emit UpdateUIEffect, relabel Send, and
        # grey it out over overlay states such as Accept.
        if state.is_busy:
            return FsmTransition(new_state, effects)
        # If currently recording, do not toggle back to Record
        send_enabled = not new_state.is_busy
        # Typing during a take must not grey out Stop (the take's exit).
        stop_enabled = new_state.is_busy or new_state.is_recording
        label = _get_send_label(new_state)
        effects.append(
            UpdateUIEffect(
                send_enabled=send_enabled,
                stop_enabled=stop_enabled,
                send_label=label,
                status_text="",
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
                stop_enabled=True,  # Stop leaves hands-free, then cancels the take
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

    elif event.kind == SendEventKind.EXTRACTED_SEND:
        # Peer inject: set busy without StartSendEffect (that posts _do_send / Ask-box).
        if state.is_busy or state.is_recording:
            return FsmTransition(state, effects)
        new_state = SendButtonState(is_busy=True, is_recording=False, has_text=state.has_text, has_audio=state.has_audio, audio_supported=state.audio_supported)
        effects.append(
            UpdateUIEffect(
                send_enabled=False,
                stop_enabled=True,
                send_label="Send",
                status_text="Starting...",
            )
        )
        return FsmTransition(new_state, effects)

    elif event.kind == SendEventKind.STOP_CLICKED:
        if not state.is_busy:
            return FsmTransition(state, effects)

        # Keep is_busy until SendCompleted/Error. Clear recording so an illegal
        # (busy and recording) start cannot be echoed (deal_shim is a no-op in LO).
        new_state = SendButtonState(
            is_busy=True,
            is_recording=False,
            has_text=state.has_text,
            has_audio=state.has_audio,
            audio_supported=state.audio_supported,
        )
        effects.append(StopSendEffect())
        effects.append(UpdateUIEffect(send_enabled=False, stop_enabled=True, send_label="Send", status_text="Stopping..."))
        return FsmTransition(new_state, effects)

    elif event.kind == SendEventKind.CANCEL_REC_CLICKED:
        # Stop on a one-shot take (the second Stop on a locked one). ERROR_OCCURRED
        # also drops the take, but it shows "Error" for a click the user meant.
        if not state.is_recording:
            return FsmTransition(state, effects)
        new_state = SendButtonState(
            is_busy=False,
            is_recording=False,
            has_text=state.has_text,
            has_audio=False,
            audio_supported=state.audio_supported,
        )
        effects.append(CancelRecordingEffect())
        effects.append(
            UpdateUIEffect(send_enabled=True, stop_enabled=False, send_label=_get_send_label(new_state), status_text="Recording cancelled")
        )
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
        # Keep typed text so the user can retry. Always drop has_audio: Stop Rec
        # auto-send and _transcribe_audio already consume/delete the WAV. Leaving
        # has_audio True after STT/chat errors (G13/G28) shows a dead Send button.
        new_state = SendButtonState(
            is_busy=False,
            is_recording=False,
            has_text=state.has_text,  # Keep text on error so user can retry
            has_audio=False,
            audio_supported=state.audio_supported,
        )
        label = _get_send_label(new_state)
        effects.append(UpdateUIEffect(send_enabled=True, stop_enabled=False, send_label=label, status_text="Error"))
        return FsmTransition(new_state, effects)

    return FsmTransition(state, effects)
