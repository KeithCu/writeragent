import pytest

import deal
from plugin.chatbot.send_state import (
    ButtonAction,
    CancelRecordingEffect,
    SendButtonState,
    SendEvent,
    SendEventKind,
    StartCaptureEffect,
    StartSendEffect,
    StopSendEffect,
    UpdateUIEffect,
    next_state,
)


def test_send_event_fields():
    a = SendEvent(SendEventKind.SEND_CLICKED)
    assert a.has_text is None
    b = SendEvent(SendEventKind.TEXT_UPDATED, has_text=True)
    assert b.has_text is True


def test_send_event_post_init_assertions():
    with pytest.raises(AssertionError):
        SendEvent(SendEventKind.TEXT_UPDATED)
    with pytest.raises(AssertionError):
        SendEvent(SendEventKind.TEXT_UPDATED, has_text=None)
    with pytest.raises(AssertionError):
        SendEvent(SendEventKind.SEND_CLICKED, has_text=True)


def test_initial_state_to_text_updated():
    state = SendButtonState(False, False, False, False, True)
    tr = next_state(state, SendEvent(SendEventKind.TEXT_UPDATED, has_text=True))

    assert tr.state.has_text is True
    assert tr.state.is_busy is False
    assert len(tr.effects) == 1
    assert isinstance(tr.effects[0], UpdateUIEffect)
    assert tr.effects[0].send_label == "Send"
    assert tr.effects[0].action == ButtonAction.SEND


def test_record_flow():
    state = SendButtonState(False, False, False, False, True)
    tr = next_state(state, SendEvent(SendEventKind.RECORD_CLICKED))

    assert tr.state.is_recording is True
    assert any(isinstance(e, StartCaptureEffect) for e in tr.effects)
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
    assert tr2.state.has_audio is False
    assert not any(isinstance(e, CancelRecordingEffect) for e in tr2.effects)
    ui_effect = next(e for e in tr2.effects if isinstance(e, UpdateUIEffect))
    assert ui_effect.status_text == "Error"


def test_error_after_stop_rec_clears_has_audio():
    """G28: STT/chat errors dispatch ERROR_OCCURRED while has_audio is still True.

    The WAV is already gone (_transcribe_audio finally / stream error cleanup).
    Keeping has_audio left a dead Send button; SEND_COMPLETED already clears it.
    """
    state = SendButtonState(False, False, False, False, True)
    tr = next_state(state, SendEvent(SendEventKind.RECORD_CLICKED))
    tr2 = next_state(tr.state, SendEvent(SendEventKind.STOP_REC_CLICKED))
    assert tr2.state.has_audio is True
    assert tr2.state.is_busy is True

    tr3 = next_state(tr2.state, SendEvent(SendEventKind.ERROR_OCCURRED))
    assert tr3.state.is_busy is False
    assert tr3.state.is_recording is False
    assert tr3.state.has_audio is False
    assert not any(isinstance(e, CancelRecordingEffect) for e in tr3.effects)
    ui_effect = next(e for e in tr3.effects if isinstance(e, UpdateUIEffect))
    assert ui_effect.send_label == "Record"
    assert ui_effect.send_enabled is True


def test_error_while_idle_is_noop():
    state1 = SendButtonState(False, False, True, False, True)
    tr1 = next_state(state1, SendEvent(SendEventKind.ERROR_OCCURRED))
    assert tr1.state == state1
    assert not tr1.effects

    state2 = SendButtonState(False, False, False, True, True)
    tr2 = next_state(state2, SendEvent(SendEventKind.ERROR_OCCURRED))
    assert tr2.state == state2
    assert not tr2.effects


def test_error_while_recording_drops_the_take():
    state = SendButtonState(False, True, True, False, True)
    tr = next_state(state, SendEvent(SendEventKind.ERROR_OCCURRED))
    assert tr.state.is_recording is False
    assert tr.state.is_busy is False
    assert tr.state.has_text is True
    assert tr.state.has_audio is False
    assert len(tr.effects) == 2
    assert isinstance(tr.effects[0], CancelRecordingEffect)
    ui_effect = tr.effects[1]
    assert isinstance(ui_effect, UpdateUIEffect)
    assert ui_effect.status_text == "Error"
    assert ui_effect.send_enabled is True
    assert ui_effect.stop_enabled is False
    assert ui_effect.send_label == "Send"


def test_record_clicked_clears_stale_has_audio():
    tr1 = next_state(
        SendButtonState(False, False, False, True, True),
        SendEvent(SendEventKind.RECORD_CLICKED),
    )
    assert tr1.state.is_recording is True
    assert tr1.state.has_audio is False
    assert tr1.state.is_busy is False
    assert tr1.state.has_text is False
    tr1_cancel = next_state(tr1.state, SendEvent(SendEventKind.CANCEL_REC_CLICKED))
    ui1 = next(e for e in tr1_cancel.effects if isinstance(e, UpdateUIEffect))
    assert ui1.send_label == "Record"

    tr2 = next_state(
        SendButtonState(False, False, True, True, True),
        SendEvent(SendEventKind.RECORD_CLICKED),
    )
    assert tr2.state.has_audio is False
    assert tr2.state.has_text is True
    ui2 = next(e for e in tr2.effects if isinstance(e, UpdateUIEffect))
    assert ui2.send_label == "Stop Rec"

    for bad in (
        SendButtonState(True, False, False, True, True),
        SendButtonState(False, False, False, True, False),
    ):
        tr_bad = next_state(bad, SendEvent(SendEventKind.RECORD_CLICKED))
        assert tr_bad.state == bad
        assert not any(isinstance(e, StartCaptureEffect) for e in tr_bad.effects)


def test_text_updated_without_has_text_raises():
    state = SendButtonState(False, False, True, False, True)
    with pytest.raises(AssertionError):
        next_state(state, SendEvent(SendEventKind.TEXT_UPDATED))


def test_invalid_record_does_not_emit_recording_or_send_effects():
    """Busy, already recording, or no audio support: RECORD_CLICKED is a no-op for I/O effects."""
    for bad in (
        SendButtonState(True, False, False, False, True),
        SendButtonState(False, True, False, False, True),
        SendButtonState(False, False, False, False, False),
    ):
        tr = next_state(bad, SendEvent(SendEventKind.RECORD_CLICKED))
        assert tr.state == bad
        assert not any(isinstance(e, StartCaptureEffect) for e in tr.effects)
        assert not any(isinstance(e, StartSendEffect) for e in tr.effects)


def test_send_clicked_with_no_input_emits_no_start_send():
    state = SendButtonState(False, False, False, False, True)
    tr = next_state(state, SendEvent(SendEventKind.SEND_CLICKED))
    assert tr.state == state
    assert not any(isinstance(e, StartSendEffect) for e in tr.effects)


def test_extracted_send_sets_busy_without_start_send():
    state = SendButtonState(False, False, False, False, True)
    tr = next_state(state, SendEvent(SendEventKind.EXTRACTED_SEND))
    assert tr.state.is_busy is True
    assert not any(isinstance(e, StartSendEffect) for e in tr.effects)
    ui_effect = next(e for e in tr.effects if isinstance(e, UpdateUIEffect))
    assert ui_effect.send_enabled is False
    assert ui_effect.stop_enabled is True


def test_extracted_send_while_busy_is_noop():
    state = SendButtonState(True, False, True, False, True)
    tr = next_state(state, SendEvent(SendEventKind.EXTRACTED_SEND))
    assert tr.state == state
    assert not tr.effects


def test_send_clicked_with_audio_only_starts_send():
    """Cold send with recorded audio and no text (not via Stop Rec auto-send)."""
    state = SendButtonState(False, False, False, True, True)
    tr = next_state(state, SendEvent(SendEventKind.SEND_CLICKED))
    assert tr.state.is_busy is True
    assert tr.state.has_audio is True
    assert any(isinstance(e, StartSendEffect) for e in tr.effects)

    tr_unsupported = next_state(
        SendButtonState(False, False, False, True, False),
        SendEvent(SendEventKind.SEND_CLICKED),
    )
    assert tr_unsupported.state.is_busy is True
    assert tr_unsupported.state.has_audio is True
    assert any(isinstance(e, StartSendEffect) for e in tr_unsupported.effects)


def test_next_state_rejects_busy_and_recording_pre():
    """CrossHair found STOP_CLICKED on (busy, recording) violating the ensure."""
    from tests.harness.strip_bundle import deal_pre_present

    if not deal_pre_present(next_state):
        pytest.skip("@deal.pre stripped in release bundle")
    illegal = SendButtonState(True, True, False, False, False)
    with pytest.raises(deal.PreContractError):
        next_state(illegal, SendEvent(SendEventKind.STOP_CLICKED))


def test_stop_clicked_when_idle_emits_no_stop_send():
    state = SendButtonState(False, False, True, False, True)
    tr = next_state(state, SendEvent(SendEventKind.STOP_CLICKED))
    assert tr.state == state
    assert not any(isinstance(e, StopSendEffect) for e in tr.effects)


def test_stop_clicked_twice_while_busy_stays_busy():
    """Packet B3: second Stop while winding down must not leave the FSM."""
    state = SendButtonState(True, False, True, False, True)
    tr = next_state(state, SendEvent(SendEventKind.STOP_CLICKED))
    tr2 = next_state(tr.state, SendEvent(SendEventKind.STOP_CLICKED))
    assert tr2.state.is_busy is True
    assert tr2.state.is_recording is False
    assert any(isinstance(e, StopSendEffect) for e in tr2.effects)


def test_record_enables_stop_for_the_take():
    """Stop is the take's exit (leave hands-free, then cancel); Stop Rec still sends."""
    state = SendButtonState(False, False, False, False, True)
    tr = next_state(state, SendEvent(SendEventKind.RECORD_CLICKED))
    ui_effect = next(e for e in tr.effects if isinstance(e, UpdateUIEffect))
    assert ui_effect.send_enabled is True
    assert ui_effect.stop_enabled is True


def test_text_updated_during_take_keeps_stop_enabled():
    state = SendButtonState(False, True, False, False, True)
    tr = next_state(state, SendEvent(SendEventKind.TEXT_UPDATED, has_text=True))
    ui_effect = next(e for e in tr.effects if isinstance(e, UpdateUIEffect))
    assert ui_effect.send_label == "Stop Rec"
    assert ui_effect.stop_enabled is True


def test_cancel_rec_drops_the_take_without_send_or_error():
    state = SendButtonState(False, True, False, False, True)
    tr = next_state(state, SendEvent(SendEventKind.CANCEL_REC_CLICKED))
    assert tr.state.is_recording is False
    assert tr.state.is_busy is False
    assert tr.state.has_audio is False
    assert any(isinstance(e, CancelRecordingEffect) for e in tr.effects)
    assert not any(isinstance(e, StartSendEffect) for e in tr.effects)
    ui_effect = next(e for e in tr.effects if isinstance(e, UpdateUIEffect))
    assert ui_effect.status_text == "Recording cancelled"
    assert ui_effect.send_label == "Record"
    assert ui_effect.send_enabled is True
    assert ui_effect.stop_enabled is False


def test_cancel_rec_keeps_typed_text():
    state = SendButtonState(False, True, True, False, True)
    tr = next_state(state, SendEvent(SendEventKind.CANCEL_REC_CLICKED))
    assert tr.state.has_text is True
    ui_effect = next(e for e in tr.effects if isinstance(e, UpdateUIEffect))
    assert ui_effect.send_label == "Send"


def test_cancel_rec_when_not_recording_is_noop():
    for state in (
        SendButtonState(False, False, True, False, True),
        SendButtonState(True, False, True, True, True),
    ):
        tr = next_state(state, SendEvent(SendEventKind.CANCEL_REC_CLICKED))
        assert tr.state == state
        assert not tr.effects


def test_text_updated_while_busy_emits_no_ui_effects():
    """Typing while busy (in flight or awaiting approval) records text but avoids UI effect clobber."""
    state = SendButtonState(True, False, False, False, True)
    tr = next_state(state, SendEvent(SendEventKind.TEXT_UPDATED, has_text=True))
    assert tr.state.is_busy is True
    assert tr.state.has_text is True
    assert not tr.effects


def test_stop_clicked_label_uses_get_send_label():
    state1 = SendButtonState(True, False, True, False, True)
    tr1 = next_state(state1, SendEvent(SendEventKind.STOP_CLICKED))
    assert any(isinstance(e, StopSendEffect) for e in tr1.effects)
    ui_effect1 = next(e for e in tr1.effects if isinstance(e, UpdateUIEffect))
    assert ui_effect1.send_label == "Send"
    assert ui_effect1.send_enabled is False
    assert ui_effect1.stop_enabled is True
    assert ui_effect1.status_text == "Stopping..."

    state2 = SendButtonState(True, False, False, False, True)
    tr2 = next_state(state2, SendEvent(SendEventKind.STOP_CLICKED))
    ui_effect2 = next(e for e in tr2.effects if isinstance(e, UpdateUIEffect))
    assert ui_effect2.send_label == "Record"

    state3 = SendButtonState(True, False, False, False, False)
    tr3 = next_state(state3, SendEvent(SendEventKind.STOP_CLICKED))
    ui_effect3 = next(e for e in tr3.effects if isinstance(e, UpdateUIEffect))
    assert ui_effect3.send_label == "Send"

