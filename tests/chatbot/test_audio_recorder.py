import os
import threading
import wave
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.xdist_group("audio_recorder_control")

from plugin.chatbot.audio_recorder import (
    AudioRecorder,
    clear_stub_recorder_control,
    write_stub_recorder_control,
)


@pytest.fixture
def ctx():
    return MagicMock()


@pytest.fixture
def recording_mocks(tmp_path):
    wav_path = str(tmp_path / "test.wav")
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.poll.return_value = None  # simulate running subprocess; stop path calls stop_recording_process
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", return_value=wav_path),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", return_value=proc) as spawn,
        patch("plugin.chatbot.audio_recorder.wait_for_recording_ready") as wait_ready,
        patch("plugin.chatbot.audio_recorder.stop_recording_process", return_value=wav_path) as stop_proc,
    ):
        yield {
            "wav_path": wav_path,
            "proc": proc,
            "spawn": spawn,
            "wait_ready": wait_ready,
            "stop_proc": stop_proc,
        }


def test_audio_recorder_spawns_venv_subprocess(ctx, recording_mocks):
    recorder = AudioRecorder(ctx)
    try:
        recorder.start_recording()
        recording_mocks["spawn"].assert_called_once()
        recording_mocks["wait_ready"].assert_called_once()
        assert recorder.state.status == "recording"
        assert recorder.temp_filename == recording_mocks["wav_path"]

        returned = recorder.stop_recording()
        assert returned == recording_mocks["wav_path"]
        recording_mocks["stop_proc"].assert_called_once()
        assert recorder.state.status == "idle"
    finally:
        if os.path.exists(recording_mocks["wav_path"]):
            os.remove(recording_mocks["wav_path"])


def test_audio_recorder_multiple_sessions(ctx, recording_mocks):
    paths = [str(recording_mocks["wav_path"]), str(recording_mocks["wav_path"]) + "2"]

    with patch("plugin.chatbot.audio_recorder.make_temp_wav_path", side_effect=paths):
        recorder = AudioRecorder(ctx)
        recorder.start_recording()
        first = recorder.temp_filename
        recorder.stop_recording()

        recorder.start_recording()
        second = recorder.temp_filename
        recorder.stop_recording()

        assert first == paths[0]
        assert second == paths[1]
        assert first != second


def test_audio_recorder_skip_spawn_from_control_file(ctx, tmp_path):
    fixture = tmp_path / "inject.wav"
    fixture.write_bytes(b"RIFF....WAVEfmt ")
    write_stub_recorder_control(wav=str(fixture), skip=True)
    recorder = AudioRecorder(ctx)
    try:
        recorder.start_recording()
        assert recorder.state.status == "recording"
        path = recorder.stop_recording()
        assert path and os.path.isfile(path)
    finally:
        clear_stub_recorder_control()
        if recorder.temp_filename and os.path.isfile(recorder.temp_filename):
            os.remove(recorder.temp_filename)


def test_audio_recorder_skip_spawn_injects_wav(ctx, tmp_path):
    fixture = tmp_path / "inject.wav"
    fixture.write_bytes(b"RIFF....WAVEfmt ")
    recorder = AudioRecorder(ctx)
    recorder._test_skip_spawn = True
    recorder._test_inject_wav = str(fixture)
    recorder.start_recording()
    assert recorder.state.status == "recording"
    assert recorder._stub_start_count == 1
    path = recorder.stop_recording()
    assert path and os.path.isfile(path)
    with open(path, "rb") as handle:
        assert handle.read() == fixture.read_bytes()
    os.remove(path)


def test_audio_recorder_skip_spawn_hang_ready(ctx):
    recorder = AudioRecorder(ctx)
    recorder._test_skip_spawn = True
    write_stub_recorder_control(skip=True, hang_ready=True)
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            recorder.start_recording()
        assert recorder.state.status == "error"
    finally:
        clear_stub_recorder_control()


def test_audio_recorder_control_file_clears_hang_ready(ctx, tmp_path):
    """G21 hang_ready must not stick on the live recorder for later Packet G cases."""
    fixture = tmp_path / "inject.wav"
    fixture.write_bytes(b"RIFF....WAVEfmt ")
    recorder = AudioRecorder(ctx)
    try:
        write_stub_recorder_control(skip=True, hang_ready=True)
        with pytest.raises(RuntimeError, match="timed out"):
            recorder.start_recording()
        write_stub_recorder_control(skip=True, hang_ready=False, fail_start=None, wav=str(fixture))
        recorder.start_recording()
        assert recorder._test_hang_ready is False
        assert recorder.state.status == "recording"
        path = recorder.stop_recording()
        assert path and os.path.isfile(path)
    finally:
        clear_stub_recorder_control()
        if recorder.temp_filename and os.path.isfile(recorder.temp_filename):
            os.remove(recorder.temp_filename)


def test_audio_recorder_skip_spawn_fail_start(ctx):
    recorder = AudioRecorder(ctx)
    recorder._test_skip_spawn = True
    recorder._test_fail_start = "stub crash"
    with pytest.raises(RuntimeError, match="stub crash"):
        recorder.start_recording()
    assert recorder.state.status == "error"


def test_audio_recorder_control_file_clears_fail_start(ctx, tmp_path):
    """Same live recorder: JSON fail_start=null must not keep G12's crash flag."""
    fixture = tmp_path / "inject.wav"
    fixture.write_bytes(b"RIFF....WAVEfmt ")
    recorder = AudioRecorder(ctx)
    try:
        write_stub_recorder_control(skip=True, fail_start="stub crash")
        with pytest.raises(RuntimeError, match="stub crash"):
            recorder.start_recording()
        write_stub_recorder_control(skip=True, fail_start=None, missing_wav=False, wav=str(fixture))
        recorder.start_recording()
        assert recorder._test_fail_start is None
        assert recorder.state.status == "recording"
        path = recorder.stop_recording()
        assert path and os.path.isfile(path)
    finally:
        clear_stub_recorder_control()
        if recorder.temp_filename and os.path.isfile(recorder.temp_filename):
            os.remove(recorder.temp_filename)


def test_manual_stop_returns_path_while_silence_monitor_runs(ctx, tmp_path):
    """Stop Rec goes through the real monitor and the real stop handshake.

    ``ready`` is read before the monitor starts. The monitor then consumes
    ``silence_progress`` and ``ok``. Stop must return that path anyway.
    """
    import json
    import time
    from io import StringIO

    wav_path = str(tmp_path / "voice.wav")
    stdout = StringIO(
        json.dumps({"status": "ready"}) + "\n"
        + json.dumps({"status": "silence_progress", "ms": 100}) + "\n"
        + json.dumps({"status": "ok", "path": wav_path}) + "\n"
    )
    proc = MagicMock()
    proc.stdout = stdout
    proc.stdin = StringIO()
    proc.poll.return_value = None
    proc.wait.return_value = 0
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", return_value=wav_path),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", return_value=proc),
    ):
        recorder = AudioRecorder(ctx)
        recorder.start_recording()
        assert recorder.state.status == "recording"
        assert recorder._stop_handoff is not None
        deadline = time.monotonic() + 2
        while recorder._stop_handoff.snapshot_path() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert recorder._stop_handoff.snapshot_path() == wav_path
        monitor = recorder._stdout_monitor
        returned = recorder.stop_recording()
        if monitor is not None:
            monitor.join(timeout=2)
    assert returned == wav_path
    assert json.loads(proc.stdin.getvalue()) == {"command": "stop"}


def test_stop_keeps_nonempty_wav_when_handshake_fails(ctx, tmp_path):
    wav_path = tmp_path / "keep.wav"
    wav_path.write_bytes(b"RIFF")
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", return_value=str(wav_path)),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", return_value=proc),
        patch("plugin.chatbot.audio_recorder.wait_for_recording_ready"),
        patch("plugin.chatbot.audio_recorder.monitor_recording_stdout", return_value=MagicMock()),
        patch(
            "plugin.chatbot.audio_recorder.stop_recording_process",
            side_effect=RuntimeError("timed out"),
        ),
    ):
        recorder = AudioRecorder(ctx)
        recorder.start_recording()
        returned = recorder.stop_recording()
    assert returned == str(wav_path)
    assert wav_path.is_file()
    assert wav_path.read_bytes() == b"RIFF"


def test_stop_deletes_empty_wav_when_handshake_fails(ctx, tmp_path):
    wav_path = tmp_path / "empty.wav"
    wav_path.write_bytes(b"")
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", return_value=str(wav_path)),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", return_value=proc),
        patch("plugin.chatbot.audio_recorder.wait_for_recording_ready"),
        patch("plugin.chatbot.audio_recorder.monitor_recording_stdout", return_value=MagicMock()),
        patch(
            "plugin.chatbot.audio_recorder.stop_recording_process",
            side_effect=RuntimeError("timed out"),
        ),
    ):
        recorder = AudioRecorder(ctx)
        recorder.start_recording()
        returned = recorder.stop_recording()
    assert returned is None
    assert not wav_path.exists()


def test_stdout_error_after_ready_keeps_nonempty_wav(ctx, tmp_path):
    from plugin.chatbot.audio_recorder_state import AudioRecorderState

    wav_path = tmp_path / "partial.wav"
    wav_path.write_bytes(b"RIFF")
    recorder = AudioRecorder(ctx)
    recorder.state = AudioRecorderState(status="recording")
    recorder.temp_filename = str(wav_path)
    recorder._proc = MagicMock()

    recorder._notify_recording_error("child died")

    assert recorder.state.status == "error"
    assert recorder.temp_filename == str(wav_path)
    assert wav_path.read_bytes() == b"RIFF"


def test_stdout_error_callback_does_not_apply_on_monitor(ctx, tmp_path):
    from plugin.chatbot.audio_recorder_state import AudioRecorderState

    wav_path = tmp_path / "partial.wav"
    wav_path.write_bytes(b"RIFF")
    recorder = AudioRecorder(ctx)
    recorder.state = AudioRecorderState(status="recording")
    recorder.temp_filename = str(wav_path)
    seen: list[str] = []
    recorder.set_auto_stop_callbacks(on_error=seen.append)

    recorder._notify_recording_error("child died")

    assert seen == ["child died"]
    assert recorder.state.status == "recording"
    assert wav_path.is_file()


def test_audio_recorder_missing_venv(ctx):
    with (
        patch(
            "plugin.chatbot.audio_recorder.resolve_recording_python",
            return_value=(None, "Configure Settings → Python"),
        ),
        patch.dict("sys.modules", {"sounddevice": None}),
    ):
        recorder = AudioRecorder(ctx)
        with pytest.raises(RuntimeError, match="Configure Settings|Please configure a Python venv"):
            recorder.start_recording()


def test_venv_record_to_wav_writes_file(tmp_path):
    import plugin.scripting.venv.audio_recorder as var

    stop_event = threading.Event()
    output_path = str(tmp_path / "out.wav")

    mock_sd = MagicMock()
    mock_stream = MagicMock()

    def fake_raw_input_stream(*args, **kwargs):
        callback = kwargs.get("callback")

        def start():
            if callback is not None:
                callback(b"\x00\x01", 1, None, None)
            stop_event.set()

        mock_stream.start.side_effect = start
        return mock_stream

    mock_sd.RawInputStream.side_effect = fake_raw_input_stream

    with patch.object(var, "_import_sounddevice", return_value=mock_sd):
        var.record_to_wav(output_path, stop_event, on_stream_started=lambda: None)

    assert os.path.exists(output_path)
    with wave.open(output_path, "rb") as wf:
        assert wf.getnchannels() == var.CHANNELS
        assert wf.getframerate() == var.SAMPLE_RATE


def test_audio_record_main_protocol(tmp_path, monkeypatch):
    from plugin.scripting.venv import audio_record_main as main_mod

    output_path = str(tmp_path / "child.wav")
    emitted: list[dict] = []

    def fake_emit(payload):
        emitted.append(payload)

    threading.Event()

    def fake_record(output, event, *, on_stream_started=None, silence_config=None, on_ipc_emit=None):
        if on_stream_started is not None:
            on_stream_started()
        event.set()
        return False  # not auto-stopped

    monkeypatch.setattr(main_mod, "_emit", fake_emit)
    monkeypatch.setattr(main_mod, "record_to_wav", fake_record)
    class _NoOpThread:
        def __init__(self, target, args, daemon):
            pass

        def start(self):
            return None

    monkeypatch.setattr(main_mod.threading, "Thread", _NoOpThread)

    code = main_mod.main(["--output", output_path])
    assert code == 0
    assert emitted[0] == {"status": "ready"}
    assert emitted[-1] == {"status": "ok", "path": os.path.abspath(output_path), "auto_stopped": False}


def test_cleanup_failure_closes_wav(ctx):
    """A failed Stop used to leave the host WAV open."""
    from plugin.chatbot.audio_recorder_state import AudioRecorderState

    recorder = AudioRecorder(ctx)
    wav = MagicMock()
    stream = MagicMock()
    recorder.wav_file = wav
    recorder.stream = stream
    recorder._silence_detector = MagicMock()
    recorder.state = AudioRecorderState(status="recording")
    recorder._proc = MagicMock()
    with (
        patch.object(recorder, "_apply_event", side_effect=RuntimeError("stop failed")),
        patch("plugin.chatbot.audio_recorder.terminate_recording_process") as term,
    ):
        recorder.cleanup()
    term.assert_called_once()
    stream.stop.assert_called_once()
    stream.close.assert_called_once()
    assert recorder.stream is None
    wav.close.assert_called_once()
    assert recorder.wav_file is None
    assert recorder._silence_detector is None


def test_close_host_wav_waits_until_callback_drops_the_file(ctx):
    recorder = AudioRecorder(ctx)
    wav = MagicMock()
    recorder.wav_file = wav
    in_callback = threading.Event()
    allow_finish = threading.Event()
    close_finished = threading.Event()

    def callback_holds_lock() -> None:
        with recorder._wav_lock:
            in_callback.set()
            assert allow_finish.wait(2)

    worker = threading.Thread(target=callback_holds_lock)
    worker.start()
    assert in_callback.wait(2)

    def do_close() -> None:
        recorder._close_host_wav()
        close_finished.set()

    closer = threading.Thread(target=do_close)
    closer.start()
    assert not close_finished.wait(0.05)
    assert wav.close.call_count == 0
    allow_finish.set()
    assert close_finished.wait(2)
    wav.close.assert_called_once()
    assert recorder.wav_file is None
    worker.join(2)
    closer.join(2)


def test_host_callback_writeframes_error_is_swallowed(ctx, tmp_path, monkeypatch):
    import sys
    import types

    captured: dict = {}

    class _Stream:
        def __init__(self, **kwargs):
            captured["callback"] = kwargs["callback"]

        def start(self) -> None:
            return None

    sounddevice = types.ModuleType("sounddevice")
    sounddevice.RawInputStream = _Stream
    monkeypatch.setitem(sys.modules, "sounddevice", sounddevice)
    wav_path = str(tmp_path / "host.wav")
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("", "no venv")),
        patch("plugin.chatbot.audio_recorder.ensure_downloaded_audio_on_path"),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", return_value=wav_path),
    ):
        recorder = AudioRecorder(ctx)
        recorder.start_recording()
    assert recorder.state.status == "recording"
    real_wav = recorder.wav_file
    boom = MagicMock()
    boom.writeframes.side_effect = ValueError("closed")
    recorder.wav_file = boom
    real_wav.close()
    captured["callback"](b"\x00\x00", 1, None, None)
    boom.writeframes.assert_called_once()
