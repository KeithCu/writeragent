import pytest
from plugin.modules.chatbot.send_state import (
    SendButtonState, SendEventKind, SendEvent,
    UpdateUIEffect, next_state
)

def test_initial_state_to_text_updated():
    state = SendButtonState(False, False, False, False, True)
    new_state, effects = next_state(state, SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}))

    assert new_state.has_text is True
    assert new_state.is_busy is False
    assert len(effects) == 1
    assert isinstance(effects[0], UpdateUIEffect)
    assert effects[0].send_label == "Send"

def test_record_flow():
    state = SendButtonState(False, False, False, False, True)
    new_state, effects = next_state(state, SendEvent(SendEventKind.RECORD_CLICKED))

    assert new_state.is_recording is True
    assert "start_recording" in effects
    ui_effect = next(e for e in effects if isinstance(e, UpdateUIEffect))
    assert ui_effect.send_label == "Stop Rec"

    # Stop recording
    new_state2, effects2 = next_state(new_state, SendEvent(SendEventKind.STOP_REC_CLICKED))
    assert new_state2.is_recording is False
    assert new_state2.has_audio is True
    assert new_state2.is_busy is True
    assert "stop_recording" in effects2
    assert "start_send" in effects2
    ui_effect2 = next(e for e in effects2 if isinstance(e, UpdateUIEffect))
    assert ui_effect2.send_label == "Send"
    assert ui_effect2.send_enabled is False
    assert ui_effect2.stop_enabled is True
    assert ui_effect2.status_text == "Starting..."

def test_send_flow():
    state = SendButtonState(False, False, True, False, True)
    new_state, effects = next_state(state, SendEvent(SendEventKind.SEND_CLICKED))

    assert new_state.is_busy is True
    assert "start_send" in effects
    ui_effect = next(e for e in effects if isinstance(e, UpdateUIEffect))
    assert ui_effect.send_enabled is False
    assert ui_effect.stop_enabled is True

    # Stop during send
    new_state2, effects2 = next_state(new_state, SendEvent(SendEventKind.STOP_CLICKED))
    assert new_state2.is_busy is True  # still busy until explicitly completed
    assert "stop_send" in effects2

    # Complete send
    new_state3, effects3 = next_state(new_state2, SendEvent(SendEventKind.SEND_COMPLETED))
    assert new_state3.is_busy is False
    assert new_state3.has_text is False
    assert new_state3.has_audio is False

def test_error_flow():
    state = SendButtonState(False, False, True, False, True)
    new_state, effects = next_state(state, SendEvent(SendEventKind.SEND_CLICKED))
    assert new_state.is_busy is True

    new_state2, effects2 = next_state(new_state, SendEvent(SendEventKind.ERROR_OCCURRED))
    assert new_state2.is_busy is False
    assert new_state2.has_text is True # keeps text
    ui_effect = next(e for e in effects2 if isinstance(e, UpdateUIEffect))
    assert ui_effect.status_text == "Error"

