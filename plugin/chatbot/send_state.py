from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, assert_never

from plugin.framework.service import BaseState, FsmTransition
from plugin.framework.deal_shim import deal


# --- Button Actions ---


class ButtonAction(Enum):
    SEND = auto()
    RECORD = auto()
    STOP_REC = auto()
    ACCEPT = auto()
    CHANGE = auto()
    REJECT = auto()
    IGNORE = auto()


# --- State ---


@dataclass(frozen=True)
class SendButtonState(BaseState):
    is_busy: bool  # True when AI is generating (or transcribing)
    is_recording: bool  # True when audio is actively being recorded
    has_text: bool  # True when the query text area is non-empty
    has_audio: bool  # Stop Rec committed a take to send. Not proof a WAV exists; the panel's audio_wav_path is the file. A missing file ends the send as ERROR_OCCURRED, which clears this flag.
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


@dataclass(frozen=True)
class SendEvent:
    kind: SendEventKind
    has_text: bool | None = None

    def __post_init__(self) -> None:
        if self.kind == SendEventKind.TEXT_UPDATED:
            assert isinstance(self.has_text, bool)
        else:
            assert self.has_text is None


# --- Effects ---


RECORDING_STATUS = "Recording audio..."


# Panel capture effects. Not audio_recorder_state.StartRecordingEffect / StopRecordingEffect.
@dataclass(frozen=True)
class UpdateUIEffect:
    send_enabled: bool
    stop_enabled: bool
    send_label: str
    status_text: str
    recording_status: bool = False
    action: ButtonAction = ButtonAction.RECORD


BUSY_STARTING_EFFECT = UpdateUIEffect(
    send_enabled=False,
    stop_enabled=True,
    send_label="Send",
    status_text="Starting...",
    action=ButtonAction.IGNORE,
)


@dataclass(frozen=True)
class StartCaptureEffect:
    pass


@dataclass(frozen=True)
class StopCaptureEffect:
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


SendEffects = UpdateUIEffect | StartCaptureEffect | StopCaptureEffect | CancelRecordingEffect | StartSendEffect | StopSendEffect


# --- Pure Transition Function ---


def _replace_state(state: SendButtonState, **changes: Any) -> SendButtonState:
    return dataclasses.replace(state, **changes)


def _noop(state: SendButtonState) -> FsmTransition[SendButtonState]:
    return FsmTransition(state, [])


# Helper to determine the button label
def _get_send_label(state: SendButtonState) -> str:
    if state.is_recording:
        return "Stop Rec"
    if state.has_text or state.has_audio:
        return "Send"
    return "Record" if state.audio_supported else "Send"


def _return_idle(
    state: SendButtonState,
    status_text: str,
    *,
    clear_text: bool = False,
    release_capture: bool = False,
) -> FsmTransition[SendButtonState]:
    """The three idle exits share Send-on / Stop-off / label-from-cleared-flags;

    status text, clearing typed text, and deleting the take stay arguments.
    """
    changes: dict[str, Any] = {
        "is_busy": False,
        "is_recording": False,
        "has_audio": False,
    }
    if clear_text:
        changes["has_text"] = False
    new_state = _replace_state(state, **changes)

    effects: list[SendEffects] = []
    if release_capture:
        effects.append(CancelRecordingEffect())
    action = ButtonAction.SEND if (new_state.has_text or new_state.has_audio or not new_state.audio_supported) else ButtonAction.RECORD
    effects.append(
        UpdateUIEffect(
            send_enabled=True,
            stop_enabled=False,
            send_label=_get_send_label(new_state),
            status_text=status_text,
            action=action,
        )
    )
    return FsmTransition(new_state, effects)


def _send_ui_invariants(
    state: SendButtonState,
    event: SendEvent,
    result: FsmTransition[SendButtonState],
) -> bool:
    """Send and stop button invariants.

    One predicate, still enforced by a single ``@deal.ensure``. The
    Hypothesis oracles call this function so the contract and the tests
    cannot drift.
    """
    # crosshair: off
    if result.state.is_busy and result.state.is_recording:
        return False
    for effect in result.effects:
        if isinstance(effect, UpdateUIEffect):
            if effect.send_enabled and effect.stop_enabled and not result.state.is_recording:
                return False
            if effect.send_enabled and result.state.is_busy:
                return False
            if effect.stop_enabled and not (result.state.is_busy or result.state.is_recording):
                return False
    return True


# Contract: while a send is in flight only Stop is enabled, and while idle only
# Send is. A take is the one state with both: Stop Rec (the Send button) sends
# the take, and Stop leaves hands-free or cancels the take.
# Stop stays enabled while recording. That is the step-down exit: with
# Stop off during every take, a hands-free loop has no exit between turns
# (Stop Rec sends and keeps the lock). The contracts below allow exactly
# that state and nothing else.
# Pre rejects the illegal pair so CrossHair cannot start from (busy and recording).
@deal.pre(lambda state, event: not (state.is_busy and state.is_recording))
@deal.ensure(_send_ui_invariants)
def next_state(state: SendButtonState, event: SendEvent) -> FsmTransition[SendButtonState]:
    """Pure state transition for the Send button."""
    # crosshair: off
    match event.kind:
        case SendEventKind.TEXT_UPDATED:
            assert event.has_text is not None
            new_state = _replace_state(state, has_text=event.has_text)
            # Update has_text for when the turn finishes, and emit no UI effect
            # while busy. Typing during web-search approval or in-flight
            # generation would otherwise emit UpdateUIEffect, relabel Send, and
            # grey it out over overlay states such as Accept.
            if state.is_busy:
                return _noop(new_state)
            # Typing during a take must not grey out Stop (the take's exit).
            label = _get_send_label(new_state)
            action = ButtonAction.SEND if (new_state.has_text or new_state.has_audio or not new_state.audio_supported) else ButtonAction.RECORD
            effect = UpdateUIEffect(
                send_enabled=True,
                stop_enabled=new_state.is_recording,
                send_label=label,
                status_text="",
                action=action,
            )
            return FsmTransition(new_state, [effect])

        case SendEventKind.RECORD_CLICKED:
            if state.is_busy or state.is_recording or not state.audio_supported:
                return _noop(state)

            # Idle + has_audio is a legal cold-send state; leaving the flag set on
            # a new take made a later cancel/error treat an empty box as a committed
            # take (_get_send_label returns Send). A new capture is not that commit.
            new_state = _replace_state(state, is_busy=False, is_recording=True, has_audio=False)
            effects: list[SendEffects] = [
                StartCaptureEffect(),
                UpdateUIEffect(
                    send_enabled=True,
                    stop_enabled=True,
                    send_label="Stop Rec",
                    status_text=RECORDING_STATUS,
                    recording_status=True,
                ),
            ]
            return FsmTransition(new_state, effects)

        case SendEventKind.STOP_REC_CLICKED:
            if not state.is_recording:
                return _noop(state)

            new_state = _replace_state(
                state,
                is_busy=True,
                is_recording=False,
                has_audio=True,  # Commit the take. The WAV is not known in this function.
            )
            effects = [
                StopCaptureEffect(),
                BUSY_STARTING_EFFECT,
                StartSendEffect(),
            ]
            return FsmTransition(new_state, effects)

        case SendEventKind.SEND_CLICKED:
            if state.is_busy or state.is_recording:
                return _noop(state)
            if not state.has_text and not state.has_audio:
                return _noop(state)

            new_state = _replace_state(state, is_busy=True, is_recording=False)
            effects = [
                BUSY_STARTING_EFFECT,
                StartSendEffect(),
            ]
            return FsmTransition(new_state, effects)

        case SendEventKind.EXTRACTED_SEND:
            # Peer inject: set busy without StartSendEffect (that posts _do_send / Ask-box).
            if state.is_busy or state.is_recording:
                return _noop(state)
            new_state = _replace_state(state, is_busy=True, is_recording=False)
            effects = [
                BUSY_STARTING_EFFECT,
            ]
            return FsmTransition(new_state, effects)

        case SendEventKind.STOP_CLICKED:
            if not state.is_busy:
                return _noop(state)

            # Keep is_busy until SEND_COMPLETED or ERROR_OCCURRED. Force is_recording
            # False so a Stop Rec label cannot be echoed. @deal.pre rejects (busy and
            # recording) when deal is installed; release OXTs strip it and LibreOffice's
            # deal_shim is a no-op, so this assignment is the runtime copy of that invariant.
            new_state = _replace_state(
                state,
                is_busy=True,
                is_recording=False,
            )
            effects = [
                StopSendEffect(),
                UpdateUIEffect(
                    send_enabled=False,
                    stop_enabled=True,
                    send_label=_get_send_label(new_state),
                    status_text="Stopping...",
                    action=ButtonAction.IGNORE,
                ),
            ]
            return FsmTransition(new_state, effects)

        case SendEventKind.CANCEL_REC_CLICKED:
            # Stop on a one-shot take (the second Stop on a locked one). ERROR_OCCURRED
            # also drops the take, but it shows "Error" for a click the user meant.
            if not state.is_recording:
                return _noop(state)
            return _return_idle(state, "Recording cancelled", release_capture=True)

        case SendEventKind.SEND_COMPLETED:
            if not state.is_busy:
                return _noop(state)

            # Completion clears typed text and has_audio; the panel's
            # _sync_has_text_from_query puts back text typed during the turn.
            return _return_idle(state, "Ready", clear_text=True)

        case SendEventKind.ERROR_OCCURRED:
            if not state.is_busy and not state.is_recording:
                return _noop(state)

            # Keep typed text so the user can retry. Always drop has_audio: Stop Rec
            # auto-send and _transcribe_audio already consume/delete the WAV. Leaving
            # has_audio True after STT/chat errors (G13/G28) shows a dead Send button.
            # Clearing is_recording with only UpdateUIEffect left the FSM idle while
            # capture could still be running if the caller had not stopped the
            # recorder; release_capture=state.is_recording emits the same
            # CancelRecordingEffect as cancel, and a busy send error does not,
            # because the drain owns that file.
            return _return_idle(state, "Error", release_capture=state.is_recording)

        case _:
            assert_never(event.kind)
