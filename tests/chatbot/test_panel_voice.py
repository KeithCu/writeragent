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
        self._close_turn_ui = MagicMock()

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


def test_stop_tts_handles_exceptions():
    with patch("plugin.audio.tts_service.stop_speech", side_effect=RuntimeError("tts error")) as mock_stop:
        _stop_tts()
    mock_stop.assert_called_once()


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
    with patch("plugin.audio.stt_service.uses_local_stt", return_value=False), patch(
        "plugin.framework.client.model_fetcher.get_stt_model", return_value=None
    ):
        controller.on_capture_stopped("/tmp/test.wav")
        assert panel._wait_for_take is False
        panel._interpret_effect.assert_not_called()
        panel._close_turn_ui.assert_called_once_with(speak=False)


def test_on_capture_stopped_when_path_is_none():
    panel = DummyPanel()
    panel._wait_for_take = True
    controller = PanelVoiceController(panel)
    controller.on_capture_stopped(None)
    assert panel._wait_for_take is False
    panel._interpret_effect.assert_not_called()
    panel._close_turn_ui.assert_called_once_with(speak=False)


def test_on_capture_stopped_when_panel_gone_deletes_wav(tmp_path):
    wav = tmp_path / "abandoned.wav"
    wav.write_bytes(b"data")
    panel = DummyPanel()
    panel.ctx = None  # panel gone
    controller = PanelVoiceController(panel)
    controller.on_capture_stopped(str(wav))
    assert not wav.exists()


def test_async_stt_stop_in_flight_restores_ask_and_skips_send(tmp_path):
    import threading
    from plugin.framework.send_cancellation import SendCancellation

    wav = tmp_path / "take.wav"
    wav.write_bytes(b"RIFF")

    panel = DummyPanel()
    panel.audio_wav_path = str(wav)
    panel._stt_inflight = False
    panel._stt_defer_close = False
    panel._stt_pending_close = None
    panel._orphan_send_outcome = None
    panel._turn = None
    panel.query_control = None
    panel._append_response = MagicMock()
    panel._restore_query_text = MagicMock()
    panel.sync_has_text_from_query = MagicMock()
    scope = SendCancellation()
    panel._send_cancellation = scope

    posted: list[Any] = []
    panel.queue_executor._async_callback_service = object()
    panel.queue_executor.post = lambda fn, **_kw: posted.append(fn)

    started = threading.Event()
    release = threading.Event()
    resolved: list[str] = []
    closed: list[Any] = []

    def fake_transcribe(*_args, **_kwargs) -> str:
        started.set()
        assert release.wait(timeout=2.0)
        return "Fake STT transcript."

    ctrl = PanelVoiceController(panel)
    with (
        patch("plugin.audio.stt_service.uses_local_stt", return_value=True),
        patch("plugin.audio.stt_service.transcribe", side_effect=fake_transcribe),
        patch("plugin.audio.stt_service.status_for_transcription", return_value="Transcribing audio..."),
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value="chat"),
        patch("plugin.framework.client.model_fetcher.get_stt_model", return_value="whisper-1"),
        patch("plugin.framework.config.get_current_endpoint", return_value="https://example"),
    ):
        assert ctrl.try_start_async_stt("", on_resolved=resolved.append) is True
        assert panel._stt_defer_close is True
        panel._stt_pending_close = lambda err=None: closed.append(err)
        assert started.wait(timeout=2.0)
        scope.cancel()
        release.set()

        # Wait for worker to post continuation to queue_executor
        import time
        deadline = time.monotonic() + 2.0
        while not posted and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(posted) == 1
        posted[0]()

    assert resolved == []
    assert closed == [None]
    assert panel._orphan_send_outcome == "stopped"
    panel._restore_query_text.assert_called_once_with("Fake STT transcript.")
    assert not wav.exists()
