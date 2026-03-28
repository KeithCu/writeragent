from dataclasses import dataclass
from typing import List, Optional

from plugin.framework.state import BaseState, FsmTransition

try:
    import deal  # type: ignore
except ImportError:
    class _DummyDeal:
        @staticmethod
        def pre(func): return lambda f: f
        @staticmethod
        def post(func): return lambda f: f
        @staticmethod
        def ensure(func): return lambda f: f
    deal = _DummyDeal()

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

@deal.post(lambda result: result.state.status in ('idle', 'initializing', 'recording', 'stopping', 'error'))
def next_state(
    state: AudioRecorderState,
    event: AudioRecorderEvent
) -> FsmTransition[AudioRecorderState]:
    """Pure state transition for the audio recorder - NO SIDE EFFECTS"""

    effects: List[AudioRecorderEffect] = []

    match event:
        case ErrorOccurredEvent(error_message=msg):
            effects.append(StopRecordingEffect())
            effects.append(ReportErrorEffect(msg))
            new_state = AudioRecorderState(status='error', error_message=msg)
            return FsmTransition(new_state, effects)

        case StartRequestedEvent():
            if state.status in ('idle', 'error'):
                effects.append(InitializeDeviceEffect())
                return FsmTransition(AudioRecorderState(status='initializing'), effects)
            return FsmTransition(state, effects)

        case DeviceReadyEvent():
            if state.status == 'initializing':
                effects.append(StartRecordingEffect())
                return FsmTransition(AudioRecorderState(status='recording'), effects)
            return FsmTransition(state, effects)

        case StopRequestedEvent():
            if state.status in ('initializing', 'recording'):
                effects.append(StopRecordingEffect())
                return FsmTransition(AudioRecorderState(status='idle'), effects)
            return FsmTransition(state, effects)

    return FsmTransition(state, effects)
