import io
import json
import subprocess
import sys
import threading
from io import StringIO
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.scripting.audio_recorder_service import (
    is_audio_recording_configured,
    resolve_recording_python,
    stop_recording_process,
    wait_for_recording_ready,
)


def _recording_child(proc: MagicMock, drain: object | None = None) -> ManagedSubprocess:
    return ManagedSubprocess.wrap_existing(proc, label="audio-recorder", stderr_tail=drain)  # type: ignore[arg-type]


def test_reap_kills_the_group_before_dropping_stderr(monkeypatch):
    from plugin.scripting.audio_recorder_service import _reap_recording_process

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
    child = _recording_child(proc, drain)

    def _kill(target):
        order.append("kill")
        assert target is proc

    monkeypatch.setattr("plugin.scripting.audio_recorder_service.kill_process_tree", _kill)
    _reap_recording_process(child, 0.01)
    assert order == ["terminate", "kill", "join"]
    assert child._closed is False


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
    wait_for_recording_ready(_recording_child(proc))


def test_stop_recording_process_returns_path():
    from plugin.scripting.audio_recorder_service import RecordingStopHandoff

    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    # _write_all rejects a write() that does not return a positive byte count.
    # Without this, the known path hides the OSError and the stop line is never accepted.
    proc.stdin.write.side_effect = lambda data: len(data)
    proc.wait.return_value = 0
    handoff = RecordingStopHandoff()
    handoff.note_ok("/tmp/x.wav")

    assert stop_recording_process(_recording_child(proc), handoff=handoff) == "/tmp/x.wav"
    proc.stdin.write.assert_called_once_with(json.dumps({"command": "stop"}) + "\n")


def test_stop_recording_process_uses_json_stop_command():
    from plugin.scripting.audio_recorder_service import RecordingStopHandoff

    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = StringIO()
    proc.wait.return_value = 0
    handoff = RecordingStopHandoff()
    handoff.note_ok("/tmp/x.wav")

    assert stop_recording_process(_recording_child(proc), handoff=handoff) == "/tmp/x.wav"
    assert proc.stdin.getvalue() == json.dumps({"command": "stop"}) + "\n"


def test_monitor_recording_stdout_skips_non_json_lines_before_ok():
    """Stray non-JSON lines (e.g. ALSA warnings or library prints) must be skipped."""
    from plugin.scripting.audio_recorder_service import RecordingStopHandoff, monitor_recording_stdout

    proc = MagicMock()
    proc.poll.side_effect = [None, None, 0]
    proc.stdout = StringIO("ALSA lib pcm.c: unknown PCM\n" + json.dumps({"status": "ok", "path": "/tmp/good.wav"}) + "\n")
    handoff = RecordingStopHandoff()

    handle = monitor_recording_stdout(
        _recording_child(proc),
        on_auto_stopped=lambda _p: None,
        handoff=handoff,
    )
    handle.join(timeout=2.0)
    assert handoff.snapshot_path() == "/tmp/good.wav"


def test_stop_recording_via_handoff_reaps_on_timeout():
    """Handoff timeout must reap the child process via try/finally."""
    from plugin.scripting.audio_recorder_service import (
        RecordingStopHandoff,
        _RECORDING_REAP_TIMEOUT_SEC,
    )

    proc = MagicMock()
    proc.poll.return_value = None
    # StringIO.write returns a length, so the stop line is sent and wait_for_path can time out.
    proc.stdin = StringIO()
    drain = MagicMock()
    child = _recording_child(proc, drain)

    handoff = RecordingStopHandoff()
    # Note nothing so wait_for_path times out
    with pytest.raises(RuntimeError, match="timed out"):
        stop_recording_process(child, handoff=handoff, timeout_sec=0.01)

    proc.wait.assert_called_with(timeout=_RECORDING_REAP_TIMEOUT_SEC)
    drain.join.assert_called()


def test_wait_for_recording_ready_eof_raises_runtime_error():
    proc = MagicMock()
    proc.stdout = StringIO("")
    proc.stderr = StringIO("")
    proc.poll.return_value = None

    with pytest.raises(RuntimeError, match="ended before responding"):
        wait_for_recording_ready(_recording_child(proc), timeout_sec=0.01)


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
    child = _recording_child(proc)

    handoff = RecordingStopHandoff()
    progress: list[int] = []
    handle = monitor_recording_stdout(
        child,
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
    assert stop_recording_process(child, handoff=handoff, timeout_sec=1) == wav_path
    assert json.loads(proc.stdin.getvalue()) == {"command": "stop"}


def test_audio_record_main_accepts_json_and_legacy_stop_commands():
    from plugin.scripting.venv.audio_record_main import _is_stop_command

    assert _is_stop_command(json.dumps({"command": "stop"}))
    assert _is_stop_command("stop\n")
    assert not _is_stop_command(json.dumps({"command": "continue"}))


def test_stdin_stop_reader_sets_event_on_eof(monkeypatch):
    from plugin.scripting.venv.audio_record_main import _stdin_stop_reader

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    stop_event = threading.Event()
    _stdin_stop_reader(stop_event)
    assert stop_event.is_set()


def test_stdin_stop_reader_sets_event_on_stop_and_returns(monkeypatch):
    from plugin.scripting.venv.audio_record_main import _stdin_stop_reader

    monkeypatch.setattr(sys, "stdin", io.StringIO("stop\nNEXT\n"))
    stop_event = threading.Event()
    _stdin_stop_reader(stop_event)
    assert stop_event.is_set()


def test_stdin_stop_reader_sets_event_when_read_raises(monkeypatch):
    from plugin.scripting.venv.audio_record_main import _stdin_stop_reader

    class _RaisingStdin:
        def __iter__(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(sys, "stdin", _RaisingStdin())
    stop_event = threading.Event()
    with pytest.raises(UnicodeDecodeError):
        _stdin_stop_reader(stop_event)
    assert stop_event.is_set()



def test_native_audio_stt_fallback_replaces_db_row():
    """Fallback STT must replace the audio message row with the transcript in the DB."""
    from plugin.scripting.audio_recorder_service import try_native_audio_stt_fallback

    host = MagicMock()
    host.audio_wav_path = "/fake/a.wav"
    host._terminal_status = "Ready"
    host._active_query_text = "typed query"
    host._active_client = MagicMock()
    host._turn.alive = True
    host._turn.batcher = None
    host._turn.queue = MagicMock()
    host._turn.text_model = "chat-model"
    host._turn.endpoint = "https://example"

    # Session has the audio-included message in memory
    audio_msg = {"role": "user", "content": [{"type": "text", "text": "typed query"}, {"type": "input_audio"}]}
    host.session.messages = [audio_msg]

    db_rows = [{"role": "system", "content": "hello"}, {"role": "user", "content": "[Voice message]"}]
    host.session.db = MagicMock()
    host.session.db.get_messages.return_value = db_rows

    def _stopped(_path, _model):
        return "spoken words"

    host._transcribe_audio.side_effect = _stopped
    with (
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value="chat-model"),
        patch("plugin.framework.config.get_current_endpoint", return_value="https://example"),
        patch("plugin.framework.client.model_fetcher.get_stt_model", return_value="stt-model"),
        patch("plugin.framework.client.model_fetcher.set_native_audio_support") as mock_set_support,
        patch("plugin.audio.stt_service.uses_local_stt", return_value=False),
        patch("plugin.scripting.audio_recorder_service.os.remove"),
    ):
        recovered = try_native_audio_stt_fallback(host, "unsupported modality: audio")

    assert recovered is True
    mock_set_support.assert_called_once_with("chat-model", "https://example", supported=False)
    assert len(host.session.messages) == 1
    assert host.session.messages[0]["content"] == "typed query\nspoken words"

    host.session.db.update_last_user_content.assert_called_once_with("typed query\nspoken words")
    host.session.add_user_message.assert_not_called()


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
    host._turn.text_model = "chat-model"
    host._turn.endpoint = "https://example"

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


def test_native_audio_stt_fallback_uses_turn_model_on_mid_turn_change():
    """If model is changed in combobox mid-turn, fallback marks the turn's model unsupported, not the new one."""
    from plugin.scripting.audio_recorder_service import try_native_audio_stt_fallback

    host = MagicMock()
    host.audio_wav_path = "/fake/a.wav"
    host._terminal_status = "Ready"
    host._active_query_text = "typed query"
    host._active_client = MagicMock()
    host._turn.alive = True
    host._turn.batcher = None
    host._turn.queue = MagicMock()
    host._turn.text_model = "turn-captured-model"
    host._turn.endpoint = "https://captured-endpoint"

    audio_msg = {"role": "user", "content": [{"type": "text", "text": "typed query"}, {"type": "input_audio"}]}
    host.session.messages = [audio_msg]
    host.session.db = MagicMock()
    host.session.db.get_messages.return_value = [{"role": "user", "content": "[Voice message]"}]
    host._transcribe_audio.return_value = "spoken words"

    with (
        patch("plugin.framework.client.model_fetcher.get_text_model", return_value="newly-selected-model"),
        patch("plugin.framework.config.get_current_endpoint", return_value="https://new-endpoint"),
        patch("plugin.framework.client.model_fetcher.get_stt_model", return_value="stt-model"),
        patch("plugin.framework.client.model_fetcher.set_native_audio_support") as mock_set_support,
        patch("plugin.audio.stt_service.uses_local_stt", return_value=False),
        patch("plugin.scripting.audio_recorder_service.os.remove"),
    ):
        recovered = try_native_audio_stt_fallback(host, "unsupported modality: audio")

    assert recovered is True
    mock_set_support.assert_called_once_with("turn-captured-model", "https://captured-endpoint", supported=False)


def test_stop_recording_process_requires_handoff():
    proc = MagicMock()
    with pytest.raises(TypeError):
        stop_recording_process(proc)  # type: ignore[call-arg]


def test_error_line_wakes_handoff_without_callback():
    from plugin.scripting.audio_recorder_service import RecordingStopHandoff, monitor_recording_stdout

    proc = MagicMock()
    proc.poll.return_value = 0
    proc.stdout = StringIO(json.dumps({"status": "error", "message": "mic gone"}) + "\n")
    handoff = RecordingStopHandoff()

    handle = monitor_recording_stdout(_recording_child(proc), on_auto_stopped=lambda _p: None, on_error=None, handoff=handoff)
    handle.join(timeout=2.0)
    assert handoff.has_error() is True
    with pytest.raises(RuntimeError, match="mic gone"):
        handoff.wait_for_path(0.01)

    # Error line with no message defaults to "Audio recording failed."
    proc2 = MagicMock()
    proc2.poll.return_value = 0
    proc2.stdout = StringIO(json.dumps({"status": "error"}) + "\n")
    handoff2 = RecordingStopHandoff()

    handle2 = monitor_recording_stdout(_recording_child(proc2), on_auto_stopped=lambda _p: None, on_error=None, handoff=handoff2)
    handle2.join(timeout=2.0)
    assert handoff2.has_error() is True
    with pytest.raises(RuntimeError, match="Audio recording failed."):
        handoff2.wait_for_path(0.01)


def test_callback_exception_still_reads_ok():
    from plugin.scripting.audio_recorder_service import RecordingStopHandoff, monitor_recording_stdout

    proc = MagicMock()
    proc.poll.side_effect = [None, None, 0]
    proc.stdout = StringIO(
        json.dumps({"status": "auto_stopped", "path": "/tmp/a.wav"}) + "\n"
        + json.dumps({"status": "silence_progress", "ms": 100}) + "\n"
        + json.dumps({"status": "ok", "path": "/tmp/b.wav"}) + "\n"
    )
    handoff = RecordingStopHandoff()
    auto_paths: list[str] = []
    progress: list[int] = []

    def _failing_auto_stop(p: str) -> None:
        auto_paths.append(p)
        raise RuntimeError("callback exploded")

    handle = monitor_recording_stdout(
        _recording_child(proc),
        on_auto_stopped=_failing_auto_stop,
        on_silence_progress=progress.append,
        handoff=handoff,
    )
    handle.join(timeout=2.0)

    assert auto_paths == ["/tmp/a.wav"]
    assert progress == [100]
    assert handoff.snapshot_path() == "/tmp/a.wav"
    assert not handle.is_alive()


def test_is_400_input_validation_uses_status():
    from plugin.scripting.audio_recorder_service import _is_400_input_validation

    assert _is_400_input_validation({
        "code": "HTTP_ERROR",
        "message": "HTTP Error 400 from AI Provider: Bad Request",
        "details": {"status": 400},
    }) is True

    assert _is_400_input_validation({
        "code": "HTTP_ERROR",
        "message": "HTTP Error 400 from AI Provider: Bad Request input validation",
        "details": {"status": 500},
    }) is False

    assert _is_400_input_validation("HTTP Error 400 bad request input validation") is False

    assert _is_400_input_validation({
        "code": "HTTP_ERROR",
        "message": "HTTP Error 400 from AI Provider: Bad Request",
        "details": {"status": True},
    }) is False


def test_append_wav_missing_file_returns_false():
    from plugin.scripting.audio_recorder_service import append_wav_as_input_audio

    content_list = []
    assert append_wav_as_input_audio(content_list, "/no/such.wav") is False
    assert content_list == []


def test_spawn_recording_process_uses_managed_text_pipes(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.scripting.audio_silence_detector import SilenceDetectorConfig
    from plugin.scripting.sandbox import wrap_command_for_sandbox
    from plugin.scripting.audio_recorder_service import (
        _AUDIO_RECORD_MAIN,
        _build_recording_env,
        spawn_recording_process,
    )
    import plugin.framework.managed_subprocess as ms

    captured_cmds: list[list[str]] = []
    captured_kwargs: list[dict[str, Any]] = []
    mock_proc = MagicMock()
    mock_proc.pid = 4242
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured_cmds.append(list(cmd))
        captured_kwargs.append(dict(kwargs))
        return mock_proc

    drain_kwargs_list: list[dict[str, Any]] = []

    def fake_drain(stream: Any, **kwargs: Any) -> MagicMock:
        drain_kwargs_list.append(dict(kwargs))
        return MagicMock()

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ms, "start_stderr_drain", fake_drain)

    exe = "/venv/bin/python"
    out_path = "/tmp/take.wav"
    res = spawn_recording_process(exe, out_path, silence_config=SilenceDetectorConfig(silence_stop_ms=250))

    assert isinstance(res, ManagedSubprocess)
    assert res.popen is mock_proc
    assert res not in ms._instances

    kwargs = captured_kwargs[0]
    assert kwargs["stdin"] == subprocess.PIPE
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.PIPE
    assert kwargs["text"] is True
    assert kwargs["bufsize"] == 1
    assert kwargs["cwd"] is None
    assert "encoding" not in kwargs
    assert "errors" not in kwargs
    assert "universal_newlines" not in kwargs
    assert "shell" not in kwargs

    if sys.platform == "win32":
        assert kwargs.get("creationflags") == subprocess.CREATE_NO_WINDOW
        assert "start_new_session" not in kwargs
    else:
        assert kwargs.get("start_new_session") is True
        assert "creationflags" not in kwargs

    assert kwargs["env"] == _build_recording_env()
    expected_cmd = wrap_command_for_sandbox(
        [exe, _AUDIO_RECORD_MAIN, "--output", out_path, "--silence-stop-ms=250"],
        env=kwargs["env"],
        cwd=None,
        watch_bus=True,
    )
    assert captured_cmds[0] == expected_cmd
    assert drain_kwargs_list[0]["name"] == "audio-rec-stderr-4242"

    res2 = spawn_recording_process(exe, out_path)
    assert isinstance(res2, ManagedSubprocess)
    assert "--silence-stop-ms=250" not in captured_cmds[1]
    assert captured_kwargs[1]["text"] is True


def test_spawn_recording_process_popen_oserror_is_not_wrapped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    from plugin.scripting.audio_recorder_service import spawn_recording_process
    import plugin.framework.managed_subprocess as ms

    def fake_popen(*a: Any, **kw: Any) -> Any:
        raise FileNotFoundError(2, "No such file", "/missing/python")

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)

    with caplog.at_level(logging.ERROR, logger="plugin.framework.managed_subprocess"):
        with pytest.raises(FileNotFoundError):
            spawn_recording_process("/missing/python", "/tmp/take.wav")

    assert "Failed to start managed subprocess" not in caplog.text
    assert len(ms._instances) == 0


def test_spawn_recording_process_flatpak_uses_watch_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.scripting.audio_silence_detector import SilenceDetectorConfig
    from plugin.scripting.audio_recorder_service import spawn_recording_process
    import plugin.framework.managed_subprocess as ms

    captured_cmds: list[list[str]] = []
    mock_proc = MagicMock()
    mock_proc.pid = 4242
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured_cmds.append(list(cmd))
        return mock_proc

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ms, "start_stderr_drain", lambda *a, **kw: MagicMock())

    with patch("plugin.scripting.sandbox.detect_sandbox", return_value="flatpak"):
        spawn_recording_process("/venv/bin/python", "/tmp/take.wav", silence_config=SilenceDetectorConfig(silence_stop_ms=250))

    assert captured_cmds[0][:3] == ["flatpak-spawn", "--host", "--watch-bus"]


@pytest.mark.parametrize(
    "msg",
    [
        "I/O operation on closed file.",
        "JSON line exceeds 16777216 bytes",
        'JSON line truncated at EOF: \'{"status"\'',
    ],
)
def test_monitor_breaks_on_fatal_stdout_value_error(msg: str, caplog: pytest.LogCaptureFixture) -> None:
    from plugin.scripting.audio_recorder_service import RecordingStopHandoff, monitor_recording_stdout

    call_count = 0

    def fake_read_json_line(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        raise ValueError(msg)

    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdout = MagicMock()
    handoff = RecordingStopHandoff()

    with (
        patch("plugin.scripting.audio_recorder_service.read_json_line", side_effect=fake_read_json_line),
        caplog.at_level("WARNING", logger="plugin.scripting.audio_recorder_service"),
    ):
        handle = monitor_recording_stdout(
            _recording_child(proc),
            on_auto_stopped=lambda _p: None,
            handoff=handoff,
        )
        handle.join(timeout=1.0)

    assert call_count == 1
    assert handle.is_alive() is False
    assert "Skipping non-JSON line" not in caplog.text


def test_reap_stops_and_joins_monitor_before_closing_stdout() -> None:
    from plugin.framework.worker_pool import run_in_background
    from plugin.scripting.audio_recorder_service import (
        RecordingStopHandoff,
        _reap_recording_process,
    )

    order: list[str] = []
    handoff = RecordingStopHandoff()

    def _monitor_loop() -> None:
        while not handoff.monitor_stop_requested():
            threading.Event().wait(0.01)
        order.append("monitor-exit")

    handle = run_in_background(_monitor_loop, dedicated=True)

    proc = MagicMock()
    proc.wait.return_value = 0
    proc.stdout = MagicMock()
    proc.stdout.fileno.return_value = 7
    proc.stdout.close.side_effect = lambda: order.append("close-stdout")
    proc.stdin = None
    proc.stderr = None
    child = _recording_child(proc)

    _reap_recording_process(child, 0.01, monitor=handle, handoff=handoff)

    assert handoff.monitor_stop_requested() is True
    assert order == ["monitor-exit", "close-stdout"]


def test_reap_skips_monitor_join_on_the_monitor_thread() -> None:
    from plugin.framework.worker_pool import run_in_background
    from plugin.scripting.audio_recorder_service import (
        RecordingStopHandoff,
        _reap_recording_process,
    )

    handoff = RecordingStopHandoff()
    start_event = threading.Event()
    success = False

    proc = MagicMock()
    proc.wait.return_value = 0
    proc.stdout = MagicMock()
    proc.stdout.fileno.return_value = 7
    proc.stdin = None
    proc.stderr = None
    child = _recording_child(proc)

    handle_holder: list[Any] = []

    def _worker() -> None:
        nonlocal success
        start_event.wait()
        _reap_recording_process(child, 0.01, monitor=handle_holder[0], handoff=handoff)
        success = True

    handle = run_in_background(_worker, dedicated=True)
    handle_holder.append(handle)
    start_event.set()
    handle.join(timeout=2.0)

    assert success is True
    assert handoff.monitor_stop_requested() is True



