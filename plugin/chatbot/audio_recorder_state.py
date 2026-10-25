from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, assert_never

from plugin.framework.service import BaseState, FsmTransition

from plugin.framework.deal_shim import DEAL_MAX_SOURCE, str_bounded, deal


class AudioStatus(str, Enum):
    IDLE = "idle"
    INITIALIZING = "initializing"
    RECORDING = "recording"
    ERROR = "error"


_VALID_AUDIO_STATUSES = tuple(AudioStatus)

# --- State ---


@dataclass(frozen=True)
class AudioRecorderState(BaseState):
    status: AudioStatus
    error_message: Optional[str] = None


# --- Events ---


class StartRequestedEvent:
    pass


class DeviceReadyEvent:
    pass


class StopRequestedEvent:
    pass


@dataclass(frozen=True)
class ErrorOccurredEvent:
    error_message: str


AudioRecorderEvent = StartRequestedEvent | DeviceReadyEvent | StopRequestedEvent | ErrorOccurredEvent

# --- Effects ---


class InitializeDeviceEffect:
    pass


class StartRecordingEffect:
    pass


class StopRecordingEffect:
    pass


@dataclass(frozen=True)
class ReportErrorEffect:
    error_message: str


AudioRecorderEffect = InitializeDeviceEffect | StartRecordingEffect | StopRecordingEffect | ReportErrorEffect

# --- Pure Transition Function ---


def _audio_transition_invariants(
    state: AudioRecorderState,
    event: AudioRecorderEvent,
    result: FsmTransition[AudioRecorderState],
) -> bool:
    # crosshair: off
    if result.state.status not in _VALID_AUDIO_STATUSES:
        return False
    if isinstance(event, ErrorOccurredEvent):
        if result.state.status != "error":
            return False
        if not any(isinstance(e, ReportErrorEffect) for e in result.effects):
            return False
    return True


@deal.pre(
    lambda state, event: state.status in _VALID_AUDIO_STATUSES
    and (
        not isinstance(event, ErrorOccurredEvent)
        or str_bounded(event.error_message, DEAL_MAX_SOURCE)
    )
)
@deal.ensure(_audio_transition_invariants)
def next_state(state: AudioRecorderState, event: AudioRecorderEvent) -> FsmTransition[AudioRecorderState]:
    """Pure state transition for the audio recorder - NO SIDE EFFECTS"""

    effects: List[AudioRecorderEffect] = []

    match event:
        case ErrorOccurredEvent(error_message=msg):
            effects.append(StopRecordingEffect())
            effects.append(ReportErrorEffect(msg))
            new_state = AudioRecorderState(status=AudioStatus.ERROR, error_message=msg)
            return FsmTransition(new_state, effects)

        case StartRequestedEvent():
            if state.status in ("idle", "error"):
                effects.append(InitializeDeviceEffect())
                return FsmTransition(AudioRecorderState(status=AudioStatus.INITIALIZING), effects)
            return FsmTransition(state, effects)

        case DeviceReadyEvent():
            if state.status == "initializing":
                effects.append(StartRecordingEffect())
                return FsmTransition(AudioRecorderState(status=AudioStatus.RECORDING), effects)
            return FsmTransition(state, effects)

        case StopRequestedEvent():
            if state.status in ("initializing", "recording"):
                effects.append(StopRecordingEffect())
                return FsmTransition(AudioRecorderState(status=AudioStatus.IDLE), effects)
            return FsmTransition(state, effects)

    assert_never(event)
