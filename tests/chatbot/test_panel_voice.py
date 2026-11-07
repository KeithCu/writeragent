from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch


from plugin.chatbot.panel_voice import PanelVoiceController, _stop_tts
from plugin.chatbot.record_gesture import RecordGesture
from plugin.chatbot.send_state import SendButtonState, SendMode
from plugin.chatbot.sidebar_state import SidebarCompositeState


class DummyPanel:
    def __init__(self) -> None:
        self.audio_wav_path: str | None = None
        self._stt_kill: Any = None
        self.audio_recorder: Any = None
        send_state = SendButtonState(
            mode=SendMode.IDLE,
            has_text=False,
            has_audio=False,
            audio_supported=True,
        )
        self.sidebar_state = SidebarCompositeState(
            send=send_state,
            tool_loop=None,
        )
        self._record_gesture = RecordGesture()
        self._sticky_restart_pending = False
        self._empty_take_count = 0
        self._panel_teardown = False
        self._wait_for_take = False
        self.stop_requested = False
        self.ctx: Any = MagicMock()
        self.queue_executor = MagicMock()
        self.dispatch = MagicMock()
        self._set_status = MagicMock()
        self._start_local_error_turn = MagicMock()
        self._interpret_effect = MagicMock()

    def exit_hands_free_record(self) -> None:
        self._record_gesture = RecordGesture(sticky=False)

    def _apply_record_gesture(self, step: Any) -> None:
        pass

    def _begin_sticky_rerecord(self) -> None:
        pass

    def _spawn_sticky_tts_wait(self, gen: int) -> None:
        pass

    def sync_audio_slice(self) -> None:
        pass


def test_panel_voice_controller_init():
    panel = DummyPanel()
    controller = PanelVoiceController(panel)
    assert controller._panel is panel


def test_stop_tts_handles_exceptions():
    with patch("plugin.audio.tts_service.stop_speech", side_effect=RuntimeError("tts error")):
        # Must not raise
        _stop_tts()


def test_clear_pending_audio_wav():
    from plugin.scripting.audio_recorder_service import clear_pending_audio_wav

    panel = DummyPanel()
    panel.audio_wav_path = "/tmp/test_nonexistent_audio.wav"
    with patch("os.remove") as mock_remove:
        clear_pending_audio_wav(panel)
        mock_remove.assert_called_once_with("/tmp/test_nonexistent_audio.wav")
    assert panel.audio_wav_path is None


def test_kill_inflight_stt():
    panel = DummyPanel()
    mock_kill = MagicMock()
    panel._stt_kill = mock_kill
    controller = PanelVoiceController(panel)
    controller._kill_inflight_stt()
    mock_kill.assert_called_once()


def test_exit_hands_free_record():
    panel = DummyPanel()
    panel._record_gesture = RecordGesture(sticky=True)
    controller = PanelVoiceController(panel)
    controller._sticky_restart_pending = True
    controller.exit_hands_free_record()
    assert not panel._record_gesture.sticky
    assert not controller._sticky_restart_pending


def test_on_capture_stopped_when_panel_gone():
    panel = DummyPanel()
    panel._panel_teardown = True
    controller = PanelVoiceController(panel)
    controller.on_capture_stopped("/tmp/test.wav")
    assert panel.audio_wav_path is None
    panel._interpret_effect.assert_not_called()


def test_on_capture_stopped_normal_flow():
    from plugin.chatbot.send_state import StartSendEffect

    panel = DummyPanel()
    panel._wait_for_take = True
    controller = PanelVoiceController(panel)
    controller.on_capture_stopped("/tmp/test.wav")
    assert panel._wait_for_take is False
    assert panel.audio_wav_path == "/tmp/test.wav"
    assert panel._interpret_effect.call_count == 1
    assert isinstance(panel._interpret_effect.call_args[0][0], StartSendEffect)


def test_on_capture_stopped_when_stop_requested():
    panel = DummyPanel()
    panel._wait_for_take = True
    panel.stop_requested = True
    controller = PanelVoiceController(panel)
    with patch.object(controller, "resolve_voice_query") as mock_resolve:
        controller.on_capture_stopped("/tmp/test.wav")
        assert panel._wait_for_take is False
        assert panel.audio_wav_path == "/tmp/test.wav"
        panel._interpret_effect.assert_not_called()
        mock_resolve.assert_called_once_with("")
