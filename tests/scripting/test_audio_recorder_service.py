import json
import subprocess
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.audio_recorder_service import (
    is_audio_recording_configured,
    resolve_recording_python,
    stop_recording_process,
    wait_for_recording_ready,
)


def test_reap_kills_the_group_before_dropping_stderr(monkeypatch):
    from plugin.scripting.audio_recorder_service import _reap_recording_process, _recording_stderr_drains

    proc = MagicMock()
    proc.pid = 99
    proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="rec", timeout=1),
        subprocess.TimeoutExpired(cmd="rec", timeout=1),
        None,
    ]
    order: list[str] = []
    proc.terminate.side_effect = lambda: order.append("terminate")
    drain = MagicMock()
    drain.join.side_effect = lambda timeout=None: order.append("join")
    _recording_stderr_drains[id(proc)] = drain

    def _kill(target):
        order.append("kill")
        assert target is proc

    monkeypatch.setattr("plugin.scripting.venv_worker._kill_process_tree", _kill)
    try:
        _reap_recording_process(proc, 0.01)
    finally:
        _recording_stderr_drains.pop(id(proc), None)
    assert order == ["terminate", "kill", "join"]


def test_is_audio_recording_configured_true():
    ctx = MagicMock()
    with (
        patch("plugin.scripting.audio_recorder_service.get_config_str", return_value="/venv"),
        patch("plugin.scripting.audio_recorder_service.resolve_venv_python", return_value="/venv/bin/python"),
    ):
        assert is_audio_recording_configured(ctx) is True


def test_is_audio_recording_configured_false_when_empty():
    ctx = MagicMock()
    with patch("plugin.scripting.audio_recorder_service.get_config_str", return_value=""):
        assert is_audio_recording_configured(ctx) is False


def test_wait_for_recording_ready_accepts_ready_line():
    proc = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.readline.return_value = json.dumps({"status": "ready"}) + "\n"
    wait_for_recording_ready(proc)


def test_stop_recording_process_returns_path():
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.readline.return_value = json.dumps({"status": "ok", "path": "/tmp/x.wav"}) + "\n"
    proc.wait.return_value = 0
    assert stop_recording_process(proc) == "/tmp/x.wav"
    proc.stdin.write.assert_called_once_with(json.dumps({"command": "stop"}) + "\n")


def test_stop_recording_process_uses_json_stop_command():
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = StringIO()
    proc.stdout = StringIO(json.dumps({"status": "ok", "path": "/tmp/x.wav"}) + "\n")
    proc.wait.return_value = 0

    assert stop_recording_process(proc) == "/tmp/x.wav"
    assert proc.stdin.getvalue() == json.dumps({"command": "stop"}) + "\n"


def test_wait_for_recording_ready_eof_raises_runtime_error():
    proc = MagicMock()
    proc.stdout = StringIO("")
    proc.stderr = StringIO("")
    proc.poll.return_value = None

    with pytest.raises(RuntimeError, match="ended before responding"):
        wait_for_recording_ready(proc, timeout_sec=0.01)


def test_resolve_recording_python_requires_venv():
    ctx = MagicMock()
    with patch("plugin.scripting.audio_recorder_service.get_config_str", return_value=""):
        exe, err = resolve_recording_python(ctx)
        assert exe is None
        assert "Settings" in err


def test_stop_gets_ok_path_already_consumed_by_stdout_monitor():
    """Manual Stop Rec must still get the WAV while the silence monitor is running.

    The monitor is the only stdout reader. It used to ignore ``{"status":"ok"}``,
    so a second reader in ``stop_recording_process`` lost the path and timed out.
    Stop now waits on the handoff after that line has already been consumed.
    """
    import time

    from plugin.scripting.audio_recorder_service import RecordingStopHandoff, monitor_recording_stdout

    wav_path = "/tmp/manual-stop.wav"
    stdout = StringIO(
        json.dumps({"status": "silence_progress", "ms": 250}) + "\n"
        + json.dumps({"status": "ok", "path": wav_path}) + "\n"
    )
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = StringIO()
    proc.stdout = stdout
    proc.wait.return_value = 0

    handoff = RecordingStopHandoff()
    progress: list[int] = []
    handle = monitor_recording_stdout(
        proc,
        on_auto_stopped=lambda _path: None,
        on_silence_progress=progress.append,
        handoff=handoff,
    )
    deadline = time.monotonic() + 2
    while handoff.snapshot_path() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    handle.join(timeout=2)

    # The ok line is gone from stdout. A competing read would see EOF.
    assert stdout.read() == ""
    assert progress == [250]
    assert handoff.snapshot_path() == wav_path
    assert stop_recording_process(proc, handoff=handoff, timeout_sec=1) == wav_path
    assert json.loads(proc.stdin.getvalue()) == {"command": "stop"}


def test_audio_record_main_accepts_json_and_legacy_stop_commands():
    from plugin.scripting.venv.audio_record_main import _is_stop_command

    assert _is_stop_command(json.dumps({"command": "stop"}))
    assert _is_stop_command("stop\n")
    assert not _is_stop_command(json.dumps({"command": "continue"}))


def test_native_audio_stt_fallback_stop_does_not_spawn_chat():
    """Stop during fallback STT ends the drain without a no-speech banner."""
    from plugin.scripting.audio_recorder_service import try_native_audio_stt_fallback

    host = MagicMock()
    host.audio_wav_path = "/fake/a.wav"
    host._terminal_status = "Ready"
    host._active_query_text = "hello"
    host._active_client = MagicMock()
    host._turn.alive = True
    host._turn.batcher = None
    host._turn.queue = MagicMock()

    def _stopped(_path, _model):
        host._terminal_status = "Stopped"
        return ""

    host._transcribe_audio.side_effect = _stopped
    with (
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value="chat-model"),
        patch("plugin.framework.config.get_current_endpoint", return_value="https://example"),
        patch("plugin.framework.client.model_fetcher.get_stt_model", return_value="stt-model"),
        patch("plugin.framework.client.model_fetcher.set_native_audio_support"),
        patch("plugin.audio.stt_service.uses_local_stt", return_value=False),
        patch("plugin.scripting.audio_recorder_service.os.remove") as mock_remove,
    ):
        recovered = try_native_audio_stt_fallback(host, "unsupported modality: audio")

    assert recovered is None
    host._spawn_llm_worker.assert_not_called()
    mock_remove.assert_not_called()
    assert host.audio_wav_path == "/fake/a.wav"
    texts = [str(call.args[0]) for call in host._append_response.call_args_list]
    assert any("Falling back to STT" in text for text in texts)
    assert not any("No speech detected" in text for text in texts)


def test_audio_history_replacement():
    from plugin.scripting.audio_recorder_service import _replace_audio_with_text
    from unittest.mock import MagicMock

    class DummyHost:
        def __init__(self):
            self.session = MagicMock()

            # Use list instead of MagicMock for messages so pop() works normally
            # Then we can assert what is inside it.
            self.session.messages = [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "test"}}]}]
            self.session.db = MagicMock()

    host = DummyHost()
    _replace_audio_with_text(host, "hello text")

    host.session.add_user_message.assert_called_once_with("hello text")
    host.session.db.replace_messages.assert_called_once_with(host.session.messages)
    assert len(host.session.messages) == 0  # pop was called

def test_stt_not_registered_in_cancel_scope():
    from plugin.chatbot.send_handlers import SendHandlersMixin
    from unittest.mock import MagicMock, patch

    host = SendHandlersMixin()
    host.audio_wav_path = "test.wav"
    cl = MagicMock()
    host.client = cl
    host._set_status = MagicMock()
    host._append_response = MagicMock()
    host.ctx = MagicMock()

    scope = MagicMock()
    scope.is_cancelled.return_value = False

    # We test the condition inside `_transcribe_audio` directly without running full loop
    with patch("plugin.chatbot.send_handlers.capture_send_stop", return_value=(scope, lambda: False)), \
         patch("plugin.chatbot.send_handlers.get_api_config", return_value={}), \
         patch("plugin.chatbot.send_handlers.LlmClient", return_value=cl), \
         patch("plugin.audio.stt_service.status_for_transcription", return_value="Transcribing..."), \
         patch("plugin.chatbot.send_handlers.run_blocking_in_thread", return_value="test text"), \
         patch("os.remove"):
        host._transcribe_audio("test.wav", "stt_model")

        # It should not register client if transcribing
        scope.register_client.assert_not_called()

def test_stt_put_transcript_in_ask_box_on_stop():
    from plugin.chatbot.panel import SendButtonListener
    from unittest.mock import MagicMock, patch

    with patch("plugin.scripting.audio_recorder_service.is_audio_recording_supported", return_value=False), patch("plugin.scripting.audio_recorder_service.is_audio_recording_configured", return_value=False), patch("plugin.chatbot.panel.ChatSession"):
        listener = SendButtonListener(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), "test")
        listener.query_control = MagicMock()
        listener.query_control.getModel.return_value = MagicMock()
        listener.query_control.getModel().Text = "hello text"

        with patch("plugin.chatbot.dialogs.get_control_text", return_value="hello text"), patch.object(listener, "_restore_query_text") as mock_restore, \
             patch("plugin.audio.stt_service.uses_local_stt", return_value=True), \
             patch("plugin.framework.client.model_fetcher.get_text_model", return_value="model"), \
             patch("plugin.framework.client.model_fetcher.get_stt_model", return_value="stt"), \
             patch("plugin.framework.config.get_current_endpoint", return_value="endpoint"):

            listener._get_document_model = MagicMock()
            listener.cached_doc_type = "writer"
            listener.audio_wav_path = "test.wav"

            # Simulate STT being stopped, returning transcript but _terminal_status="Stopped"
            listener._terminal_status = "Stopped"
            listener._transcribe_audio = MagicMock(return_value="my speech transcript")
            listener._do_send()

            # Should have restored with the COMBINED text!
            assert mock_restore.call_count == 1
            assert mock_restore.call_args_list[0][0][0] == "hello text\nmy speech transcript"
