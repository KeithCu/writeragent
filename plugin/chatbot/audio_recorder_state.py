from dataclasses import dataclass
from typing import List, Optional

from plugin.framework.service import BaseState, FsmTransition

from plugin.framework.deal_shim import DEAL_MAX_SOURCE, str_bounded, deal

_VALID_AUDIO_STATUSES = ("idle", "initializing", "recording", "stopping", "error")

# --- State ---


@dataclass(frozen=True)
class AudioRecorderState(BaseState):
    status: str  # 'idle', 'initializing', 'recording', 'stopping', 'error'
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


@deal.pre(
    lambda state, event: state.status in _VALID_AUDIO_STATUSES
    and (
        not isinstance(event, ErrorOccurredEvent)
        or str_bounded(event.error_message, DEAL_MAX_SOURCE)
    )
)
@deal.post(lambda result: result.state.status in _VALID_AUDIO_STATUSES)
@deal.ensure(
    lambda state, event, result: not isinstance(event, ErrorOccurredEvent) or result.state.status == "error"
)
@deal.ensure(
    lambda state, event, result: not isinstance(event, ErrorOccurredEvent)
    or any(isinstance(e, ReportErrorEffect) for e in result.effects)
)
def next_state(state: AudioRecorderState, event: AudioRecorderEvent) -> FsmTransition[AudioRecorderState]:
    """Pure state transition for the audio recorder - NO SIDE EFFECTS"""

    effects: List[AudioRecorderEffect] = []

    match event:
        case ErrorOccurredEvent(error_message=msg):
            effects.append(StopRecordingEffect())
            effects.append(ReportErrorEffect(msg))
            new_state = AudioRecorderState(status="error", error_message=msg)
            return FsmTransition(new_state, effects)

        case StartRequestedEvent():
            if state.status in ("idle", "error"):
                effects.append(InitializeDeviceEffect())
                return FsmTransition(AudioRecorderState(status="initializing"), effects)
            return FsmTransition(state, effects)

        case DeviceReadyEvent():
            if state.status == "initializing":
                effects.append(StartRecordingEffect())
                return FsmTransition(AudioRecorderState(status="recording"), effects)
            return FsmTransition(state, effects)

        case StopRequestedEvent():
            if state.status in ("initializing", "recording"):
                effects.append(StopRecordingEffect())
                return FsmTransition(AudioRecorderState(status="idle"), effects)
            return FsmTransition(state, effects)

    return FsmTransition(state, effects)
