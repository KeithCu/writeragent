import pytest

from plugin.modules.chatbot.audio_recorder_state import (
    AudioRecorderState,
    StartRequestedEvent,
    DeviceReadyEvent,
    StopRequestedEvent,
    ErrorOccurredEvent,
    InitializeDeviceEffect,
    StartRecordingEffect,
    StopRecordingEffect,
    ReportErrorEffect,
    next_state,
)

def test_happy_path_transitions():
    """Test the normal idle -> initializing -> recording -> stopping sequence."""

    # 1. idle -> initializing
    state = AudioRecorderState(status='idle')
    event = StartRequestedEvent()
    step = next_state(state, event)

    assert step.state.status == 'initializing'
    assert len(step.effects) == 1
    assert isinstance(step.effects[0], InitializeDeviceEffect)

    # 2. initializing -> recording
    state = step.state
    event = DeviceReadyEvent()
    step = next_state(state, event)

    assert step.state.status == 'recording'
    assert len(step.effects) == 1
    assert isinstance(step.effects[0], StartRecordingEffect)

    # 3. recording -> stopping -> idle
    state = step.state
    event = StopRequestedEvent()
    step = next_state(state, event)

    assert step.state.status == 'idle'
    assert len(step.effects) == 1
    assert isinstance(step.effects[0], StopRecordingEffect)


def test_start_while_initializing_or_recording_ignored():
    """Test that start requests are ignored when not in idle/error."""

    # In initializing
    state = AudioRecorderState(status='initializing')
    step = next_state(state, StartRequestedEvent())
    assert step.state.status == 'initializing'
    assert len(step.effects) == 0

    # In recording
    state = AudioRecorderState(status='recording')
    step = next_state(state, StartRequestedEvent())
    assert step.state.status == 'recording'
    assert len(step.effects) == 0


def test_stop_while_idle_ignored():
    """Test that stop requests are ignored when already idle."""
    state = AudioRecorderState(status='idle')
    step = next_state(state, StopRequestedEvent())
    assert step.state.status == 'idle'
    assert len(step.effects) == 0


def test_error_transition_from_any_state():
    """Test that an error event from any state transitions to error and emits cleanup."""

    states_to_test = [
        AudioRecorderState(status='idle'),
        AudioRecorderState(status='initializing'),
        AudioRecorderState(status='recording'),
    ]

    error_msg = "test error"

    for initial_state in states_to_test:
        step = next_state(initial_state, ErrorOccurredEvent(error_msg))

        assert step.state.status == 'error'
        assert step.state.error_message == error_msg

        assert len(step.effects) == 2
        assert isinstance(step.effects[0], StopRecordingEffect)
        assert isinstance(step.effects[1], ReportErrorEffect)
        assert step.effects[1].error_message == error_msg


def test_recovery_from_error():
    """Test starting a new recording from an error state clears the error."""

    state = AudioRecorderState(status='error', error_message='previous error')
    step = next_state(state, StartRequestedEvent())

    assert step.state.status == 'initializing'
    assert step.state.error_message is None
    assert len(step.effects) == 1
    assert isinstance(step.effects[0], InitializeDeviceEffect)
