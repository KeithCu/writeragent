from plugin.modules.chatbot.send_state import (
    SendButtonState,
    SendEvent,
    SendEventKind,
    StartRecordingEffect,
    StartSendEffect,
    StopSendEffect,
    UpdateUIEffect,
    next_state,
)


def test_initial_state_to_text_updated():
    state = SendButtonState(False, False, False, False, True)
    tr = next_state(state, SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": True}))

    assert tr.state.has_text is True
    assert tr.state.is_busy is False
    assert len(tr.effects) == 1
    assert isinstance(tr.effects[0], UpdateUIEffect)
    assert tr.effects[0].send_label == "Send"


def test_record_flow():
    state = SendButtonState(False, False, False, False, True)
    tr = next_state(state, SendEvent(SendEventKind.RECORD_CLICKED))

    assert tr.state.is_recording is True
    assert any(isinstance(e, StartRecordingEffect) for e in tr.effects)
    ui_effect = next(e for e in tr.effects if isinstance(e, UpdateUIEffect))
    assert ui_effect.send_label == "Stop Rec"

    tr2 = next_state(tr.state, SendEvent(SendEventKind.STOP_REC_CLICKED))
    assert tr2.state.is_recording is False
    assert tr2.state.has_audio is True
    assert tr2.state.is_busy is True
    assert any(isinstance(e, StartSendEffect) for e in tr2.effects)
    ui_effect2 = next(e for e in tr2.effects if isinstance(e, UpdateUIEffect))
    assert ui_effect2.send_label == "Send"
    assert ui_effect2.send_enabled is False
    assert ui_effect2.stop_enabled is True
    assert ui_effect2.status_text == "Starting..."


def test_send_flow():
    state = SendButtonState(False, False, True, False, True)
    tr = next_state(state, SendEvent(SendEventKind.SEND_CLICKED))

    assert tr.state.is_busy is True
    assert any(isinstance(e, StartSendEffect) for e in tr.effects)
    ui_effect = next(e for e in tr.effects if isinstance(e, UpdateUIEffect))
    assert ui_effect.send_enabled is False
    assert ui_effect.stop_enabled is True

    tr2 = next_state(tr.state, SendEvent(SendEventKind.STOP_CLICKED))
    assert tr2.state.is_busy is True
    assert any(isinstance(e, StopSendEffect) for e in tr2.effects)

    tr3 = next_state(tr2.state, SendEvent(SendEventKind.SEND_COMPLETED))
    assert tr3.state.is_busy is False
    assert tr3.state.has_text is False
    assert tr3.state.has_audio is False


def test_error_flow():
    state = SendButtonState(False, False, True, False, True)
    tr = next_state(state, SendEvent(SendEventKind.SEND_CLICKED))
    assert tr.state.is_busy is True

    tr2 = next_state(tr.state, SendEvent(SendEventKind.ERROR_OCCURRED))
    assert tr2.state.is_busy is False
    assert tr2.state.has_text is True
    ui_effect = next(e for e in tr2.effects if isinstance(e, UpdateUIEffect))
    assert ui_effect.status_text == "Error"


def test_invalid_record_does_not_emit_recording_or_send_effects():
    """Busy, already recording, or no audio support: RECORD_CLICKED is a no-op for I/O effects."""
    for bad in (
        SendButtonState(True, False, False, False, True),
        SendButtonState(False, True, False, False, True),
        SendButtonState(False, False, False, False, False),
    ):
        tr = next_state(bad, SendEvent(SendEventKind.RECORD_CLICKED))
        assert tr.state == bad
        assert not any(isinstance(e, StartRecordingEffect) for e in tr.effects)
        assert not any(isinstance(e, StartSendEffect) for e in tr.effects)


def test_send_clicked_with_no_input_emits_no_start_send():
    state = SendButtonState(False, False, False, False, True)
    tr = next_state(state, SendEvent(SendEventKind.SEND_CLICKED))
    assert tr.state == state
    assert not any(isinstance(e, StartSendEffect) for e in tr.effects)


def test_stop_clicked_when_idle_emits_no_stop_send():
    state = SendButtonState(False, False, True, False, True)
    tr = next_state(state, SendEvent(SendEventKind.STOP_CLICKED))
    assert tr.state == state
    assert not any(isinstance(e, StopSendEffect) for e in tr.effects)
