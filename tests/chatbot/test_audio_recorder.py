import io
import os
import threading
import wave
from unittest.mock import MagicMock, patch
from typing import Any

import pytest

pytestmark = pytest.mark.xdist_group("audio_recorder_control")

from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.chatbot.audio_recorder import AudioRecorder
from plugin.chatbot.audio_recorder_state import AudioStatus
from plugin.scripting.audio_recorder_service import Recording
try:
    # Release builds delete sidebar_test_hooks; the stub-recorder tests skip there.
    from plugin.chatbot.sidebar_test_hooks import (
        clear_stub_recorder_control,
        write_stub_recorder_control,
    )
except ImportError:
    _HAS_TEST_HOOKS = False
else:
    _HAS_TEST_HOOKS = True

needs_test_hooks = pytest.mark.skipif(not _HAS_TEST_HOOKS, reason="sidebar_test_hooks is omitted from release builds")


def _recording_child(proc: MagicMock, drain: Any = None) -> Recording:
    return Recording(ManagedSubprocess.wrap_existing(proc, label="audio-recorder", stderr_tail=drain))


def _recording_after_ready(proc: MagicMock, drain: Any = None) -> Recording:
    rec = _recording_child(proc, drain)
    rec.monitor = MagicMock()
    rec.monitor.is_current_thread.return_value = False
    return rec


class _ClosingStringIO(io.StringIO):
    """StringIO that preserves getvalue() after close()."""

    def __init__(self, initial_value: str = "") -> None:
        super().__init__(initial_value)
        self._saved_value = ""

    def close(self) -> None:
        if not self.closed:
            self._saved_value = self.getvalue()
        super().close()

    def getvalue(self) -> str:
        if self.closed:
            return self._saved_value
        return super().getvalue()


def _settle(recorder: AudioRecorder, timeout: float = 5.0) -> None:
    start_job = getattr(recorder, "_start_job", None)
    if start_job is not None:
        start_job.join(timeout=timeout)
    stop_job = getattr(recorder, "_stop_job", None)
    if stop_job is not None:
        stop_job.join(timeout=timeout)


def _stale_start_stop_during_init(
    recorder: AudioRecorder, ready_event: threading.Event | None = None
) -> None:
    recorder.start_recording()
    assert recorder.state.status == "initializing"
    if ready_event is not None:
        assert ready_event.wait(timeout=5.0)
    returned = recorder.stop_recording()
    assert returned is None
    assert recorder.stop_deferred is True


def _release_stale_start_gate_and_assert_none(
    recorder: AudioRecorder,
    *,
    gate: threading.Event,
    on_capture_stopped: MagicMock,
    on_error: MagicMock,
) -> None:
    gate.set()
    _settle(recorder)
    on_capture_stopped.assert_called_once_with(None)
    on_error.assert_not_called()


@pytest.fixture
def recording_mocks(tmp_path):
    wav_path = str(tmp_path / "test.wav")
    spawned_children: list[Recording] = []

    def make_child(*args: Any, **kwargs: Any) -> Recording:
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline.return_value = ""
        proc.poll.return_value = None  # simulate running subprocess; stop path calls finish
        child = _recording_child(proc)
        spawned_children.append(child)
        return child

    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", return_value=wav_path),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", side_effect=make_child) as spawn,
        patch.object(Recording, "wait_ready") as wait_ready,
        patch.object(Recording, "finish", return_value=wav_path) as stop_proc,
    ):
        yield {
            "wav_path": wav_path,
            "spawned_children": spawned_children,
            "spawn": spawn,
            "wait_ready": wait_ready,
            "stop_proc": stop_proc,
        }


def test_audio_recorder_spawns_venv_subprocess(recording_mocks):
    recorder = AudioRecorder()
    on_capture_stopped = MagicMock()
    recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped)
    try:
        recorder.start_recording()
        _settle(recorder)
        recording_mocks["spawn"].assert_called_once()
        recording_mocks["wait_ready"].assert_called_once()
        assert recorder.state.status == "recording"
        assert recorder.temp_filename == recording_mocks["wav_path"]

        returned = recorder.stop_recording()
        _settle(recorder)
        assert returned is None
        on_capture_stopped.assert_called_once_with(recording_mocks["wav_path"])
        recording_mocks["stop_proc"].assert_called_once()
        assert recorder.state.status == "idle"
    finally:
        if os.path.exists(recording_mocks["wav_path"]):
            os.remove(recording_mocks["wav_path"])


def test_audio_recorder_multiple_sessions(recording_mocks):
    paths = [str(recording_mocks["wav_path"]), str(recording_mocks["wav_path"]) + "2"]

    with patch("plugin.chatbot.audio_recorder.make_temp_wav_path", side_effect=paths):
        recorder = AudioRecorder()
        recorder.start_recording()
        _settle(recorder)
        first = recorder.temp_filename
        recorder.stop_recording()
        _settle(recorder)

        recorder.start_recording()
        _settle(recorder)
        second = recorder.temp_filename
        recorder.stop_recording()
        _settle(recorder)

        assert first == paths[0]
        assert second == paths[1]


@needs_test_hooks
def test_audio_recorder_stub_auto_stop_fires_after_start_returns(tmp_path, monkeypatch):
    """G4 stub auto-stop is reported from a tagged worker, like the stdout monitor.

    Firing it inline on the caller re-entered Record's transition
    (STOP_REC_CLICKED plus a whole send drain nested in RECORD_CLICKED) and
    wedged soffice in CI. From a tagged worker the panel's post() enqueues
    instead of running inline, so STOP_REC lands on a later VCL tick.
    """
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    fixture = tmp_path / "inject.wav"
    fixture.write_bytes(b"RIFF....WAVEfmt ")
    write_stub_recorder_control(wav=str(fixture), skip=True, auto_stop=True)
    recorder = AudioRecorder()
    fired = threading.Event()
    seen: dict[str, object] = {}

    def _on_auto_stop() -> None:
        from plugin.framework.thread_guard import get_background_task_name

        seen["thread"] = threading.current_thread()
        seen["bg_task"] = get_background_task_name()
        fired.set()

    recorder.set_auto_stop_callbacks(on_auto_stop=_on_auto_stop)
    try:
        recorder.start_recording()
        assert recorder.state.status == "recording"
        assert fired.wait(5.0), "stub auto-stop never fired"
        assert seen["thread"] is not threading.current_thread()
        assert seen["bg_task"] == "audio-rec-stub-auto-stop"
        assert recorder._auto_stopped_path == recorder.temp_filename
        # One-shot: later Packet G cases must not inherit auto_stop.
        from plugin.chatbot.sidebar_test_hooks import read_stub_recorder_control

        assert read_stub_recorder_control().get("auto_stop") is False
        path = recorder.stop_recording()
        assert path and os.path.isfile(path)
    finally:
        clear_stub_recorder_control()
        if recorder.temp_filename and os.path.isfile(recorder.temp_filename):
            os.remove(recorder.temp_filename)


def test_manual_stop_returns_path_while_silence_monitor_runs(tmp_path):
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
    proc.stdin = _ClosingStringIO()
    proc.poll.return_value = None
    proc.wait.return_value = 0
    child = _recording_child(proc)
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", return_value=wav_path),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", return_value=child),
    ):
        recorder = AudioRecorder()
        recorder.start_recording()
        _settle(recorder)
        assert recorder.state.status == "recording"
        assert recorder._recording is not None
        deadline = time.monotonic() + 2
        while recorder._recording.handoff.snapshot_path() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert recorder._recording.handoff.snapshot_path() == wav_path
        monitor = recorder._recording.monitor
        on_capture_stopped = MagicMock()
        recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped)
        returned = recorder.stop_recording()
        assert json.loads(proc.stdin.getvalue()) == {"command": "stop"}
        _settle(recorder)
        if monitor is not None:
            monitor.join(timeout=2)
    assert returned is None
    on_capture_stopped.assert_called_once_with(wav_path)


def _make_zero_frame_wav(pcm_bytes: bytes) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
    header = buf.getvalue()[:44]
    return header + pcm_bytes


_WAV_WITH_AUDIO = _make_zero_frame_wav(b"\x00" * 32)


def test_stop_keeps_nonempty_wav_when_handshake_fails(tmp_path):
    wav_path = tmp_path / "keep.wav"
    wav_path.write_bytes(_WAV_WITH_AUDIO)
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    proc.stdout = io.StringIO("")
    child = _recording_child(proc)
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", return_value=str(wav_path)),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", return_value=child),
        patch.object(child, "wait_ready"),
        patch.object(
            child,
            "finish",
            side_effect=RuntimeError("timed out"),
        ),
    ):
        recorder = AudioRecorder()
        on_capture_stopped = MagicMock()
        recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped)
        recorder.start_recording()
        _settle(recorder)
        returned = recorder.stop_recording()
        _settle(recorder)
    assert returned is None
    on_capture_stopped.assert_called_once_with(str(wav_path))
    assert wav_path.is_file()
    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnframes() == 16


def test_stop_deletes_empty_wav_when_handshake_fails(tmp_path):
    wav_path = tmp_path / "empty.wav"
    wav_path.write_bytes(b"")
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    proc.stdout = io.StringIO("")
    child = _recording_child(proc)
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", return_value=str(wav_path)),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", return_value=child),
        patch.object(child, "wait_ready"),
        patch.object(
            child,
            "finish",
            side_effect=RuntimeError("timed out"),
        ),
    ):
        recorder = AudioRecorder()
        on_capture_stopped = MagicMock()
        recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped)
        recorder.start_recording()
        _settle(recorder)
        returned = recorder.stop_recording()
        _settle(recorder)
    assert returned is None
    on_capture_stopped.assert_called_once_with(None)
    assert not wav_path.exists()


def test_stdout_error_after_ready_keeps_nonempty_wav(tmp_path):
    from plugin.chatbot.audio_recorder_state import AudioRecorderState

    wav_path = tmp_path / "partial.wav"
    wav_path.write_bytes(_WAV_WITH_AUDIO)
    recorder = AudioRecorder()
    recorder.state = AudioRecorderState(status=AudioStatus.RECORDING)
    recorder.temp_filename = str(wav_path)
    recorder._recording = MagicMock()

    recorder._notify_recording_error("child died")

    assert recorder.state.status == "error"
    assert recorder.temp_filename == str(wav_path)
    assert wav_path.read_bytes() == _WAV_WITH_AUDIO


def test_stdout_error_callback_does_not_apply_on_monitor(tmp_path):
    from plugin.chatbot.audio_recorder_state import AudioRecorderState

    wav_path = tmp_path / "partial.wav"
    wav_path.write_bytes(_WAV_WITH_AUDIO)
    recorder = AudioRecorder()
    recorder.state = AudioRecorderState(status=AudioStatus.RECORDING)
    recorder.temp_filename = str(wav_path)
    seen: list[str] = []
    recorder.set_auto_stop_callbacks(on_error=seen.append)

    recorder._notify_recording_error("child died")

    assert seen == ["child died"]
    assert recorder.state.status == "recording"
    assert wav_path.is_file()


def test_audio_recorder_missing_venv():
    with (
        patch(
            "plugin.chatbot.audio_recorder.resolve_recording_python",
            return_value=(None, "Configure Settings → Python"),
        ),
        patch.dict("sys.modules", {"sounddevice": None}),
    ):
        recorder = AudioRecorder()
        recorder.start_recording()
        _settle(recorder)
        assert recorder.state.status == "error"


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


def test_record_to_wav_holds_silence_progress_until_ready(tmp_path, monkeypatch):
    from plugin.scripting.audio_silence_detector import SilenceDetectorConfig
    import plugin.scripting.venv.audio_recorder as var

    stop_event = threading.Event()
    output_path = str(tmp_path / "out.wav")
    config = SilenceDetectorConfig(silence_stop_ms=100)
    pcm = b"\x00" * 3200
    recorded: list[str] = []

    mock_sd = MagicMock()
    mock_stream = MagicMock()
    stored_callback = None

    def fake_raw_input_stream(*args, **kwargs):
        nonlocal stored_callback
        stored_callback = kwargs.get("callback")

        def start():
            if stored_callback is not None:
                stored_callback(pcm, 1600, None, None)

        mock_stream.start.side_effect = start
        mock_stream.stop.side_effect = lambda: None
        mock_stream.close.side_effect = lambda: None
        return mock_stream

    mock_sd.RawInputStream.side_effect = fake_raw_input_stream

    def fake_wait(timeout=None):
        if stored_callback is not None:
            stored_callback(pcm, 1600, None, None)
        stop_event.set()
        return True

    monkeypatch.setattr(stop_event, "wait", fake_wait)

    with patch.object(var, "_import_sounddevice", return_value=mock_sd):
        var.record_to_wav(
            output_path,
            stop_event,
            on_stream_started=lambda: recorded.append("ready"),
            silence_config=config,
            on_ipc_emit=lambda payload: recorded.append(str(payload["status"])),
        )

    assert recorded == ["ready", "silence_progress"]
    with wave.open(output_path, "rb") as wf:
        assert wf.getnframes() == 3200



def test_venv_record_write_error_stops_instead_of_waiting(tmp_path):
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

        mock_stream.start.side_effect = start
        return mock_stream

    mock_sd.RawInputStream.side_effect = fake_raw_input_stream

    mock_wave_file = MagicMock()
    mock_wave_file.writeframes.side_effect = OSError("disk full")

    def fake_wave_open(*args, **kwargs):
        del args, kwargs
        return mock_wave_file

    wait_called = False

    def fake_wait(timeout=None):
        del timeout
        nonlocal wait_called
        wait_called = True
        if not stop_event.is_set():
            raise AssertionError("stop_event was not set before wait completed")

    with (
        patch.object(var, "_import_sounddevice", return_value=mock_sd),
        patch("wave.open", side_effect=fake_wave_open),
        patch.object(stop_event, "wait", side_effect=fake_wait),
    ):
        with pytest.raises(RuntimeError, match=r"Failed to write audio WAV file.*disk full"):
            var.record_to_wav(output_path, stop_event)

    assert wait_called is True
    mock_wave_file.close.assert_called_once()


def test_venv_record_vad_error_stops_capture(tmp_path):
    from plugin.scripting.audio_silence_detector import SilenceDetectorConfig
    import plugin.scripting.venv.audio_recorder as var

    stop_event = threading.Event()
    output_path = str(tmp_path / "out.wav")
    config = SilenceDetectorConfig(silence_stop_ms=100)

    mock_sd = MagicMock()
    mock_stream = MagicMock()

    def fake_raw_input_stream(*args, **kwargs):
        callback = kwargs.get("callback")

        def start():
            if callback is not None:
                callback(b"\x00\x01", 1, None, None)

        mock_stream.start.side_effect = start
        return mock_stream

    mock_sd.RawInputStream.side_effect = fake_raw_input_stream

    def fake_wait(timeout=None):
        del timeout
        if not stop_event.is_set():
            raise AssertionError("stop_event was not set before wait")

    mock_vad = MagicMock()
    mock_vad.process_chunk.side_effect = ValueError("bad pcm")

    with (
        patch.object(var, "_import_sounddevice", return_value=mock_sd),
        patch.object(var, "SilenceDetector", return_value=mock_vad),
        patch.object(stop_event, "wait", side_effect=fake_wait),
    ):
        with pytest.raises(RuntimeError, match=r"Audio capture failed.*bad pcm"):
            var.record_to_wav(output_path, stop_event, silence_config=config)


def test_venv_record_ipc_emit_error_stops_capture(tmp_path):
    from plugin.scripting.audio_silence_detector import SilenceDetectorConfig
    import plugin.scripting.venv.audio_recorder as var

    stop_event = threading.Event()
    output_path = str(tmp_path / "out.wav")
    config = SilenceDetectorConfig(silence_stop_ms=100)
    pcm = b"\x00" * 3200

    mock_sd = MagicMock()
    mock_stream = MagicMock()

    def fake_raw_input_stream(*args, **kwargs):
        callback = kwargs.get("callback")

        def start():
            if callback is not None:
                callback(pcm, 1600, None, None)

        mock_stream.start.side_effect = start
        return mock_stream

    mock_sd.RawInputStream.side_effect = fake_raw_input_stream

    def fake_wait(timeout=None):
        del timeout
        if not stop_event.is_set():
            raise AssertionError("stop_event was not set before wait")

    def broken_ipc_emit(payload):
        del payload
        raise OSError("ipc gone")

    with (
        patch.object(var, "_import_sounddevice", return_value=mock_sd),
        patch.object(stop_event, "wait", side_effect=fake_wait),
    ):
        with pytest.raises(RuntimeError, match=r"Audio capture failed.*ipc gone"):
            var.record_to_wav(output_path, stop_event, silence_config=config, on_ipc_emit=broken_ipc_emit)


def test_venv_record_stream_finished_early_stops_capture(tmp_path):
    import plugin.scripting.venv.audio_recorder as var

    stop_event = threading.Event()
    output_path = str(tmp_path / "out.wav")

    mock_sd = MagicMock()
    mock_stream = MagicMock()
    stored_finished_cb = None

    def fake_raw_input_stream(*args, **kwargs):
        nonlocal stored_finished_cb
        stored_finished_cb = kwargs.get("finished_callback")
        callback = kwargs.get("callback")

        def start():
            if callback is not None:
                callback(b"\x00\x01", 1, None, None)

        mock_stream.start.side_effect = start
        return mock_stream

    mock_sd.RawInputStream.side_effect = fake_raw_input_stream

    def fake_wait(timeout=None):
        del timeout
        assert stored_finished_cb is not None
        stored_finished_cb()
        assert stop_event.is_set()
        return True

    with (
        patch.object(var, "_import_sounddevice", return_value=mock_sd),
        patch.object(stop_event, "wait", side_effect=fake_wait),
    ):
        with pytest.raises(RuntimeError, match="stopped unexpectedly"):
            var.record_to_wav(output_path, stop_event)


def test_venv_record_max_duration_is_auto_stop(tmp_path):
    import plugin.scripting.venv.audio_recorder as var

    stop_event = threading.Event()
    output_path = str(tmp_path / "out.wav")
    pcm = b"\x00" * 3200  # 1600 samples (16-bit mono)
    collected_statuses: list[str] = []
    frames_at_emit: list[int] = []

    mock_sd = MagicMock()
    mock_stream = MagicMock()

    def fake_raw_input_stream(*args, **kwargs):
        callback = kwargs.get("callback")

        def start():
            if callback is not None:
                callback(pcm, 1600, None, None)

        mock_stream.start.side_effect = start
        return mock_stream

    mock_sd.RawInputStream.side_effect = fake_raw_input_stream

    def fake_wait(timeout=None):
        del timeout
        return False

    def emit_handler(payload):
        collected_statuses.append(str(payload.get("status")))
        with wave.open(output_path, "rb") as wf:
            frames_at_emit.append(wf.getnframes())

    with (
        patch.object(var, "_import_sounddevice", return_value=mock_sd),
        patch.object(stop_event, "wait", side_effect=fake_wait),
    ):
        result = var.record_to_wav(output_path, stop_event, on_ipc_emit=emit_handler)

    assert result is True
    assert collected_statuses == ["auto_stopped"]
    assert frames_at_emit == [1600]
    with wave.open(output_path, "rb") as wf:
        assert wf.getnframes() == 1600


def test_venv_record_start_error_keeps_original_reason(tmp_path):
    import plugin.scripting.venv.audio_recorder as var

    stop_event = threading.Event()
    output_path = str(tmp_path / "out.wav")

    mock_sd = MagicMock()
    mock_stream = MagicMock()
    mock_stream.start.side_effect = OSError("Device unavailable")
    mock_sd.RawInputStream.return_value = mock_stream

    with patch.object(var, "_import_sounddevice", return_value=mock_sd):
        with pytest.raises(RuntimeError, match=r"Failed to start audio capture.*Device unavailable") as excinfo:
            var.record_to_wav(output_path, stop_event)

    assert "libportaudio2" not in str(excinfo.value)


def test_venv_record_wav_init_failure_closes_file(tmp_path):
    import plugin.scripting.venv.audio_recorder as var

    stop_event = threading.Event()
    output_path = str(tmp_path / "out.wav")

    mock_sd = MagicMock()
    mock_wave_file = MagicMock()
    mock_wave_file.setnchannels.side_effect = OSError("invalid params")

    with (
        patch.object(var, "_import_sounddevice", return_value=mock_sd),
        patch("wave.open", return_value=mock_wave_file),
    ):
        with pytest.raises(RuntimeError, match=r"Failed to create audio WAV file"):
            var.record_to_wav(output_path, stop_event)

    mock_wave_file.close.assert_called_once()


def test_audio_record_main_protocol(tmp_path, monkeypatch):
    from plugin.scripting.venv import audio_record_main as main_mod

    output_path = str(tmp_path / "child.wav")
    emitted: list[dict] = []

    def fake_emit(payload):
        emitted.append(payload)

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


def test_cleanup_failure_closes_wav():
    """A failed Stop closes the host WAV file."""
    from plugin.chatbot.audio_recorder_state import AudioRecorderState

    recorder = AudioRecorder()
    wav = MagicMock()
    stream = MagicMock()
    recorder.wav_file = wav
    recorder.stream = stream
    recorder._silence_detector = MagicMock()
    recorder.state = AudioRecorderState(status=AudioStatus.RECORDING)
    recording = MagicMock()
    recorder._recording = recording
    with patch.object(recorder, "_apply_event", side_effect=RuntimeError("stop failed")):
        recorder.cleanup()
    recording.terminate.assert_called_once_with()
    stream.stop.assert_called_once()
    stream.close.assert_called_once()
    assert recorder.stream is None
    wav.close.assert_called_once()
    assert recorder.wav_file is None
    assert recorder._silence_detector is None


def test_close_host_wav_waits_until_callback_drops_the_file():
    recorder = AudioRecorder()
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


def test_host_callback_writeframes_error_is_swallowed(tmp_path, monkeypatch):
    import sys
    import types

    captured: dict = {}

    class _Stream:
        def __init__(self, **kwargs):
            captured["callback"] = kwargs["callback"]

        def start(self) -> None:
            return None

    sounddevice = types.ModuleType("sounddevice")
    setattr(sounddevice, "RawInputStream", _Stream)
    monkeypatch.setitem(sys.modules, "sounddevice", sounddevice)
    wav_path = str(tmp_path / "host.wav")
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("", "no venv")),
        patch("plugin.chatbot.audio_recorder.ensure_native_binaries_on_path"),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", return_value=wav_path),
    ):
        recorder = AudioRecorder()
        recorder.start_recording()
        _settle(recorder)
    assert recorder.state.status == "recording"
    real_wav = recorder.wav_file
    boom = MagicMock()
    boom.writeframes.side_effect = ValueError("closed")
    recorder.wav_file = boom
    real_wav.close()
    captured["callback"](b"\x00\x00", 1, None, None)
    boom.writeframes.assert_called_once()


def test_wav_file_has_bytes_treats_header_only_as_empty(tmp_path):
    from plugin.chatbot.audio_recorder import _wav_file_has_bytes

    header_only = tmp_path / "header.wav"
    header_only.write_bytes(b"\x00" * 44)
    with_audio = tmp_path / "audio.wav"
    with_audio.write_bytes(_WAV_WITH_AUDIO)
    assert _wav_file_has_bytes(str(header_only)) is False
    assert _wav_file_has_bytes(str(with_audio)) is True


@needs_test_hooks
def test_stub_control_file_ignored_without_testing_env(recording_mocks, monkeypatch, tmp_path):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    fixture = tmp_path / "inject.wav"
    fixture.write_bytes(b"RIFF....WAVEfmt ")
    write_stub_recorder_control(wav=str(fixture), skip=True)
    recorder = AudioRecorder()
    try:
        recorder.start_recording()
        _settle(recorder)
        recording_mocks["spawn"].assert_called_once()
        recorder.stop_recording()
        _settle(recorder)
    finally:
        clear_stub_recorder_control()


@needs_test_hooks
def test_unset_testing_env_never_touches_stub_control_file(recording_mocks, monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    from plugin.chatbot.sidebar_test_hooks import stub_recorder_control_path

    path = stub_recorder_control_path()
    if os.path.exists(path):
        os.remove(path)
    recorder = AudioRecorder()
    recorder.start_recording()
    _settle(recorder)
    recorder.stop_recording()
    _settle(recorder)
    assert not os.path.exists(path)


@needs_test_hooks
def test_notify_auto_stop_once_per_take(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    recorder = AudioRecorder()
    counts = 0

    def _on_auto_stop() -> None:
        nonlocal counts
        counts += 1

    recorder.set_auto_stop_callbacks(on_auto_stop=_on_auto_stop)
    recorder._notify_auto_stop("/tmp/a.wav")
    recorder._notify_auto_stop("/tmp/a.wav")
    assert counts == 1
    assert recorder._auto_stopped_path == "/tmp/a.wav"

    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    write_stub_recorder_control(skip=True)
    try:
        recorder.start_recording()
        assert recorder._auto_stop_notified is False

        recorder._notify_auto_stop("/tmp/b.wav")
        assert counts == 2
        assert recorder._auto_stopped_path == "/tmp/b.wav"
        recorder.stop_recording()
    finally:
        clear_stub_recorder_control()


def test_stop_finishes_the_published_recording(recording_mocks):
    recorder = AudioRecorder()
    try:
        recorder.start_recording()
        _settle(recorder)
        child = recording_mocks["spawned_children"][0]
        rec = recorder._recording
        assert rec is child and rec.monitor_started
        recorder.stop_recording()
        _settle(recorder)
        recording_mocks["stop_proc"].assert_called_once_with()
        assert recorder._recording is None
    finally:
        if os.path.exists(recording_mocks["wav_path"]):
            os.remove(recording_mocks["wav_path"])


def test_error_after_ready_reaps_with_the_stdout_monitor(tmp_path):
    from plugin.chatbot.audio_recorder_state import AudioRecorderState

    wav_path = tmp_path / "partial.wav"
    wav_path.write_bytes(_WAV_WITH_AUDIO)
    recorder = AudioRecorder()
    recorder.state = AudioRecorderState(status=AudioStatus.RECORDING)
    recorder.temp_filename = str(wav_path)
    proc = MagicMock()
    recording = _recording_after_ready(proc)
    recorder._recording = recording

    with patch.object(recording, "terminate") as term:
        recorder._notify_recording_error("child died")
        if recorder._stop_job is not None:
            recorder._stop_job.join(timeout=5)

    term.assert_called_once_with()
    assert wav_path.is_file()


@needs_test_hooks
def test_next_recording_deletes_wav_kept_after_stdout_error(tmp_path, monkeypatch):
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    from plugin.chatbot.audio_recorder_state import AudioRecorderState

    old_wav = tmp_path / "old.wav"
    old_wav.write_bytes(_WAV_WITH_AUDIO)
    recorder = AudioRecorder()
    recorder.state = AudioRecorderState(status=AudioStatus.RECORDING)
    recorder.temp_filename = str(old_wav)
    recorder._recording = MagicMock()

    try:
        recorder._notify_recording_error("child died")
        assert old_wav.is_file()
        assert recorder.temp_filename == str(old_wav)

        write_stub_recorder_control(skip=True)
        recorder.start_recording()

        assert not old_wav.exists()
        assert recorder.temp_filename is not None
        assert recorder.temp_filename != str(old_wav)
        assert os.path.isfile(recorder.temp_filename)
    finally:
        clear_stub_recorder_control()
        if recorder.temp_filename and os.path.isfile(recorder.temp_filename):
            try:
                os.remove(recorder.temp_filename)
            except OSError:
                pass


def test_finalize_wav_header_success(tmp_path):
    from plugin.chatbot.audio_recorder import finalize_wav_header

    wav_file = tmp_path / "valid.wav"
    raw = _make_zero_frame_wav(b"\x00" * 40)
    wav_file.write_bytes(raw)
    assert finalize_wav_header(str(wav_file)) is True
    with wave.open(str(wav_file), "rb") as wf:
        assert wf.getnframes() == 20


def test_finalize_wav_header_wrong_magic(tmp_path):
    from plugin.chatbot.audio_recorder import finalize_wav_header

    wav_file = tmp_path / "wrong_magic.wav"
    raw = b"NOPE" + b"\x00" * 40 + b"\x00" * 20
    wav_file.write_bytes(raw)
    assert finalize_wav_header(str(wav_file)) is False


def test_finalize_wav_header_truncated(tmp_path):
    from plugin.chatbot.audio_recorder import finalize_wav_header

    wav_file = tmp_path / "short.wav"
    wav_file.write_bytes(b"RIFF....WAVE")
    assert finalize_wav_header(str(wav_file)) is False


def test_finalize_wav_header_odd_pcm_length(tmp_path):
    from plugin.chatbot.audio_recorder import finalize_wav_header

    wav_file = tmp_path / "odd.wav"
    raw = _make_zero_frame_wav(b"\x00" * 15)
    wav_file.write_bytes(raw)
    assert finalize_wav_header(str(wav_file)) is False


def test_host_callback_max_duration(monkeypatch):
    import time
    from unittest.mock import MagicMock, patch
    from plugin.chatbot.audio_recorder import MAX_RECORDING_DURATION_SEC

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    captured_callback = None

    class FakeInputStream:
        def __init__(self, *args, **kwargs):
            nonlocal captured_callback
            captured_callback = kwargs.get("callback")

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    fake_sd = MagicMock()
    fake_sd.RawInputStream = FakeInputStream

    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=(None, "")),
        patch("plugin.chatbot.audio_recorder.ensure_native_binaries_on_path"),
        patch.dict("sys.modules", {"sounddevice": fake_sd}),
    ):
        recorder = AudioRecorder()
        auto_stops = []
        errors = []
        recorder.set_auto_stop_callbacks(
            on_auto_stop=lambda: auto_stops.append(True),
            on_error=lambda msg: errors.append(msg),
        )
        recorder.start_recording()
        _settle(recorder)
        assert captured_callback is not None

        # Clock under cap: does not stop
        recorder._host_stream_start_time = time.monotonic() - 10.0
        captured_callback(b"\x00" * 32, 16, None, None)
        assert len(auto_stops) == 0
        assert len(errors) == 0

        # Clock past cap: stops take and notifies error
        recorder._host_stream_start_time = time.monotonic() - (MAX_RECORDING_DURATION_SEC + 10.0)
        captured_callback(b"\x00" * 32, 16, None, None)
        assert len(auto_stops) == 1
        assert len(errors) == 1
        assert "recording too long" in errors[0]

        recorder.stop_recording()
        _settle(recorder)


def test_start_recording_returns_before_ready_timeout(monkeypatch):
    proc = MagicMock()
    r, w = os.pipe()
    proc.stdout = os.fdopen(r, "r")
    child = _recording_child(proc)

    gate = threading.Event()

    def gated_ready(*, timeout_sec=None):
        gate.wait(timeout=5.0)
        raise TimeoutError("timed out")

    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", return_value=child),
        patch.object(child, "wait_ready", side_effect=gated_ready),
    ):
        recorder = AudioRecorder()
        recorder.start_recording()
        # Recorder returns immediately while status is initializing; ready wait is blocked on gate
        assert recorder.state.status == "initializing"
        assert not gate.is_set()
        gate.set()
        _settle(recorder)
        assert recorder.state.status == "error"
        assert "timed out" in (recorder.state.error_message or "")
    os.close(w)


def test_stop_recording_returns_before_reap(monkeypatch, tmp_path):
    import json
    from plugin.chatbot.audio_recorder_state import AudioRecorderState
    from plugin.framework.thread_guard import get_background_task_name

    wav_path = str(tmp_path / "test.wav")
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = _ClosingStringIO()

    recorder = AudioRecorder()
    recorder.state = AudioRecorderState(status=AudioStatus.RECORDING)
    recorder.temp_filename = wav_path
    rec = _recording_after_ready(proc)
    rec.handoff.note_ok(wav_path)
    recorder._recording = rec

    gate = threading.Event()
    reap_records = []
    orig_reap = rec._reap

    def fake_reap(*args, **kwargs):
        gate.wait(timeout=5.0)
        reap_records.append({
            "thread": threading.current_thread(),
            "task_name": get_background_task_name(),
        })
        return orig_reap(*args, **kwargs)

    monkeypatch.setattr(rec, "_reap", fake_reap)

    returned = recorder.stop_recording()

    assert returned is None
    assert json.loads(proc.stdin.getvalue()) == {"command": "stop"}
    assert len(reap_records) == 0
    assert not gate.is_set()

    gate.set()
    _settle(recorder)
    assert len(reap_records) == 1
    assert reap_records[0]["thread"] is not threading.current_thread()
    assert reap_records[0]["task_name"] == "audio-rec-stop"


def test_cleanup_does_not_wait_for_reap(monkeypatch, tmp_path):
    from plugin.chatbot.audio_recorder_state import AudioRecorderState
    from plugin.framework.thread_guard import get_background_task_name

    wav_path = str(tmp_path / "test.wav")
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = _ClosingStringIO()

    recorder = AudioRecorder()
    recorder.state = AudioRecorderState(status=AudioStatus.RECORDING)
    recorder.temp_filename = wav_path
    rec = _recording_after_ready(proc)
    rec.handoff.note_ok(wav_path)
    recorder._recording = rec

    on_capture_stopped = MagicMock()
    recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped)

    gate = threading.Event()
    reap_records = []
    orig_reap = rec._reap

    def fake_reap(*args, **kwargs):
        gate.wait(timeout=5.0)
        reap_records.append({
            "thread": threading.current_thread(),
            "task_name": get_background_task_name(),
        })
        return orig_reap(*args, **kwargs)

    monkeypatch.setattr(rec, "_reap", fake_reap)

    recorder.cleanup()
    assert len(reap_records) == 0
    assert not gate.is_set()

    gate.set()
    _settle(recorder)
    assert len(reap_records) == 1
    assert reap_records[0]["task_name"] == "audio-rec-stop"
    on_capture_stopped.assert_not_called()


def test_stale_start_exit_after_spawn_delivers_none_and_cleans_up():
    """Stop Rec during venv spawn: delivers None via on_capture_stopped, deletes temp WAV, no error."""
    spawn_gate = threading.Event()
    spawn_started = threading.Event()
    proc = MagicMock()
    proc.poll.return_value = None
    child = _recording_child(proc)

    def gated_spawn(*args, **kwargs):
        spawn_started.set()
        spawn_gate.wait(timeout=5.0)
        return child

    on_capture_stopped = MagicMock()
    on_error = MagicMock()
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", side_effect=gated_spawn),
        patch.object(child, "terminate") as term_proc,
    ):
        recorder = AudioRecorder()
        recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped, on_error=on_error)
        _stale_start_stop_during_init(recorder, ready_event=spawn_started)
        assert recorder.state.status == "idle"

        _release_stale_start_gate_and_assert_none(
            recorder, gate=spawn_gate, on_capture_stopped=on_capture_stopped, on_error=on_error
        )
        term_proc.assert_called()


def test_stale_start_exit_after_ready_delivers_none_and_cleans_up():
    """Stop Rec after ready wait: delivers None via on_capture_stopped, terminates proc, no error."""
    ready_gate = threading.Event()
    ready_started = threading.Event()
    proc = MagicMock()
    proc.poll.return_value = None
    child = _recording_child(proc)

    def gated_ready(*args, **kwargs):
        ready_started.set()
        ready_gate.wait(timeout=5.0)

    on_capture_stopped = MagicMock()
    on_error = MagicMock()
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", return_value=child),
        patch.object(child, "wait_ready", side_effect=gated_ready),
        patch.object(child, "terminate") as term_proc,
    ):
        recorder = AudioRecorder()
        recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped, on_error=on_error)
        _stale_start_stop_during_init(recorder, ready_event=ready_started)

        _release_stale_start_gate_and_assert_none(
            recorder, gate=ready_gate, on_capture_stopped=on_capture_stopped, on_error=on_error
        )
        term_proc.assert_called()


def test_stale_start_exit_in_venv_exception_delivers_none_and_skips_error():
    """Stop Rec during venv start when child exits early: delivers None, skips error callback."""
    fail_gate = threading.Event()
    fail_started = threading.Event()
    proc = MagicMock()
    proc.poll.return_value = None
    child = _recording_child(proc)

    def failing_ready(*args, **kwargs):
        fail_started.set()
        fail_gate.wait(timeout=5.0)
        raise RuntimeError("child ended prematurely")

    on_capture_stopped = MagicMock()
    on_error = MagicMock()
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", return_value=child),
        patch.object(child, "wait_ready", side_effect=failing_ready),
        patch.object(child, "terminate") as term_proc,
    ):
        recorder = AudioRecorder()
        recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped, on_error=on_error)
        _stale_start_stop_during_init(recorder, ready_event=fail_started)

        _release_stale_start_gate_and_assert_none(
            recorder, gate=fail_gate, on_capture_stopped=on_capture_stopped, on_error=on_error
        )
        term_proc.assert_called()


def test_stale_start_exit_host_stream_delivers_none():
    """Stop Rec during host RawInputStream: delivers None via on_capture_stopped, no error."""
    stream_gate = threading.Event()
    stream_started = threading.Event()
    mock_sd = MagicMock()
    mock_stream = MagicMock()

    def gated_stream(*args, **kwargs):
        stream_started.set()
        stream_gate.wait(timeout=5.0)
        return mock_stream

    mock_sd.RawInputStream.side_effect = gated_stream
    on_capture_stopped = MagicMock()
    on_error = MagicMock()
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=(None, "")),
        patch("plugin.chatbot.audio_recorder.ensure_native_binaries_on_path"),
        patch.dict("sys.modules", {"sounddevice": mock_sd}),
    ):
        recorder = AudioRecorder()
        recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped, on_error=on_error)
        _stale_start_stop_during_init(recorder, ready_event=stream_started)

        _release_stale_start_gate_and_assert_none(
            recorder, gate=stream_gate, on_capture_stopped=on_capture_stopped, on_error=on_error
        )
        mock_stream.close.assert_called()


def test_stale_start_exit_host_exception_delivers_none_and_skips_error():
    """Stop Rec during host start exception: delivers None, skips error callback."""
    fail_gate = threading.Event()
    fail_started = threading.Event()
    mock_sd = MagicMock()

    def failing_stream(*args, **kwargs):
        fail_started.set()
        fail_gate.wait(timeout=5.0)
        raise RuntimeError("PortAudio init failed")

    mock_sd.RawInputStream.side_effect = failing_stream
    on_capture_stopped = MagicMock()
    on_error = MagicMock()
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=(None, "")),
        patch("plugin.chatbot.audio_recorder.ensure_native_binaries_on_path"),
        patch.dict("sys.modules", {"sounddevice": mock_sd}),
    ):
        recorder = AudioRecorder()
        recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped, on_error=on_error)
        _stale_start_stop_during_init(recorder, ready_event=fail_started)

        _release_stale_start_gate_and_assert_none(
            recorder, gate=fail_gate, on_capture_stopped=on_capture_stopped, on_error=on_error
        )


def test_cancel_during_start_transitions_fsm_to_idle():
    """Cancel during initializing transitions state to idle and discards take."""
    gate = threading.Event()
    ready_started = threading.Event()
    proc = MagicMock()
    proc.poll.return_value = None
    child = _recording_child(proc)

    def gated_ready(*args, **kwargs):
        ready_started.set()
        gate.wait(timeout=5.0)

    on_capture_stopped = MagicMock()
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", return_value=child),
        patch.object(child, "wait_ready", side_effect=gated_ready),
        patch.object(child, "terminate") as term_proc,
    ):
        recorder = AudioRecorder()
        recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped)
        recorder.start_recording()
        assert ready_started.wait(timeout=5.0)
        assert recorder.state.status == "initializing"

        recorder.cleanup()
        # Cleanup transitions out of initializing to idle.
        assert recorder.state.status == "idle"

        gate.set()
        _settle(recorder)

        # Take was discarded, so on_capture_stopped must not be called.
        on_capture_stopped.assert_not_called()
        term_proc.assert_called()


def test_dead_child_stop_goes_through_finish_stop_and_preserves_wav(tmp_path, monkeypatch):
    """Dead child with audio data preserves WAV via _finish_stop header finalization."""
    monkeypatch.setattr("plugin.scripting.audio_recorder_service._RECORDING_MONITOR_JOIN_SEC", 0.01)
    wav_path = tmp_path / "dead_child.wav"
    wav_path.write_bytes(_WAV_WITH_AUDIO)

    proc = MagicMock()
    proc.poll.return_value = 0  # child already dead

    from plugin.chatbot.audio_recorder_state import AudioRecorderState

    recorder = AudioRecorder()
    recorder.state = AudioRecorderState(status=AudioStatus.RECORDING)
    recorder.temp_filename = str(wav_path)
    recorder._recording = _recording_after_ready(proc)
    monkeypatch.setattr(recorder._recording.handoff, "wait_for_path", lambda _timeout: None)

    on_capture_stopped = MagicMock()
    recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped)

    returned = recorder.stop_recording()
    assert returned is None
    assert recorder.stop_deferred is True

    _settle(recorder)
    on_capture_stopped.assert_called_once_with(str(wav_path))
    assert wav_path.is_file()


def test_stale_start_after_cancel_does_not_clobber_next_take(tmp_path):
    """Old start job exiting after cancel does not clear next take's proc, path, or file."""
    gate1 = threading.Event()
    take1_in_make_temp = threading.Event()
    take2_spawned = threading.Event()

    call_count = 0
    created_paths: list[str] = []

    def gated_make_temp_wav_path() -> str:
        nonlocal call_count
        call_count += 1
        path = str(tmp_path / f"take_{call_count}.wav")
        with open(path, "wb") as f:
            f.write(b"RIFF4\x00\x00\x00WAVEfmt ")
        created_paths.append(path)
        if call_count == 1:
            take1_in_make_temp.set()
            gate1.wait(timeout=5.0)
        return path

    proc1 = MagicMock()
    proc1.poll.return_value = None
    child1 = _recording_child(proc1)

    proc2 = MagicMock()
    proc2.poll.return_value = None
    child2 = _recording_child(proc2)

    def fake_spawn(exe, temp_path, silence_config=None):
        if temp_path == created_paths[0]:
            return child1
        take2_spawned.set()
        return child2

    on_capture_stopped = MagicMock()
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", side_effect=gated_make_temp_wav_path),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", side_effect=fake_spawn),
        patch.object(child1, "wait_ready"),
        patch.object(child2, "wait_ready"),
        patch.object(child1, "terminate"),
        patch.object(child2, "terminate"),
    ):
        recorder = AudioRecorder()
        recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped)

        job1 = None
        try:
            recorder.start_recording()
            job1 = recorder._start_job
            assert take1_in_make_temp.wait(timeout=5.0)

            recorder.cleanup()
            recorder.start_recording()
            assert take2_spawned.wait(timeout=5.0)

            # Allow take 2's start job to finish publishing proc and status
            _settle(recorder)

            # Release the first job; its epoch is stale so it must not clobber take 2.
            gate1.set()
            if job1 is not None:
                job1.join(timeout=5.0)
            _settle(recorder)

            assert os.path.exists(created_paths[1])
            assert not os.path.exists(created_paths[0])
            on_capture_stopped.assert_not_called()
        finally:
            gate1.set()
            if job1 is not None:
                job1.join(timeout=5.0)
            _settle(recorder)


def test_stale_start_does_not_consume_newer_stop_commit(tmp_path):
    """Old start job does not consume stop commit token belonging to a newer take."""
    gate1 = threading.Event()
    gate2 = threading.Event()
    take1_in_make_temp = threading.Event()
    take2_in_wait_ready = threading.Event()

    call_count = 0
    created_paths: list[str] = []

    def gated_make_temp_wav_path() -> str:
        nonlocal call_count
        call_count += 1
        path = str(tmp_path / f"take_{call_count}.wav")
        with open(path, "wb") as f:
            f.write(b"RIFF4\x00\x00\x00WAVEfmt ")
        created_paths.append(path)
        if call_count == 1:
            take1_in_make_temp.set()
            gate1.wait(timeout=5.0)
        return path

    proc1 = MagicMock()
    proc1.poll.return_value = None
    child1 = _recording_child(proc1)

    proc2 = MagicMock()
    proc2.poll.return_value = None
    child2 = _recording_child(proc2)

    def fake_spawn(exe, temp_path, silence_config=None):
        if temp_path == created_paths[0]:
            return child1
        return child2

    def gated_wait_ready(*args, **kwargs):
        take2_in_wait_ready.set()
        gate2.wait(timeout=5.0)

    on_capture_stopped = MagicMock()
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", side_effect=gated_make_temp_wav_path),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", side_effect=fake_spawn),
        patch.object(child1, "wait_ready"),
        patch.object(child2, "wait_ready", side_effect=gated_wait_ready),
        patch.object(child1, "terminate"),
        patch.object(child2, "terminate"),
    ):
        recorder = AudioRecorder()
        recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped)

        job1 = None
        job2 = None
        try:
            recorder.start_recording()
            job1 = recorder._start_job
            assert take1_in_make_temp.wait(timeout=5.0)

            recorder.cleanup()
            recorder.start_recording()
            job2 = recorder._start_job
            assert take2_in_wait_ready.wait(timeout=5.0)

            returned = recorder.stop_recording()
            assert returned is None
            assert recorder.stop_deferred is True

            # Release job 1. Since its epoch does not match the stop commit token,
            # it must not consume the stop commit token or invoke on_capture_stopped.
            gate1.set()
            if job1 is not None:
                job1.join(timeout=5.0)

            on_capture_stopped.assert_not_called()
            assert os.path.exists(created_paths[1])

            # Release job 2. Its epoch matches the commit token, so it delivers None once.
            gate2.set()
            if job2 is not None:
                job2.join(timeout=5.0)
            _settle(recorder)

            on_capture_stopped.assert_called_once_with(None)
        finally:
            gate1.set()
            gate2.set()
            if job1 is not None:
                job1.join(timeout=5.0)
            if job2 is not None:
                job2.join(timeout=5.0)
            _settle(recorder)


def test_stale_start_after_spawn_preserves_next_take_process(tmp_path):
    """Old start job exiting after spawn does not clear next take's recording identity."""
    gate1 = threading.Event()
    take1_in_ready = threading.Event()
    take2_spawned = threading.Event()

    call_count = 0
    created_paths: list[str] = []

    def fake_make_temp() -> str:
        nonlocal call_count
        call_count += 1
        path = str(tmp_path / f"take_{call_count}.wav")
        with open(path, "wb") as f:
            f.write(b"RIFF4\x00\x00\x00WAVEfmt ")
        created_paths.append(path)
        return path

    proc1 = MagicMock()
    proc1.poll.return_value = None
    child1 = _recording_child(proc1)

    proc2 = MagicMock()
    proc2.poll.return_value = None
    child2 = _recording_child(proc2)

    def fake_spawn(exe, temp_path, silence_config=None):
        if temp_path == created_paths[0]:
            return child1
        take2_spawned.set()
        return child2

    def gated_ready(*args, **kwargs):
        take1_in_ready.set()
        gate1.wait(timeout=5.0)

    on_capture_stopped = MagicMock()
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", side_effect=fake_make_temp),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", side_effect=fake_spawn),
        patch.object(child1, "wait_ready", side_effect=gated_ready),
        patch.object(child2, "wait_ready"),
        patch.object(child2, "finish", side_effect=lambda *a, **kw: created_paths[1]),
        patch.object(child1, "terminate") as term1,
        patch.object(child2, "terminate") as term2,
    ):
        recorder = AudioRecorder()
        recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped)

        job1 = None
        try:
            recorder.start_recording()
            job1 = recorder._start_job
            assert take1_in_ready.wait(timeout=5.0)

            # Take 1 has spawned child1 and published _recording = child1, and is waiting in ready.
            recorder.cleanup()
            recorder.start_recording()
            assert take2_spawned.wait(timeout=5.0)
            _settle(recorder)

            # Release take 1. Epoch is stale; its exit must terminate child1 only
            # and must not clear take 2's _recording.
            gate1.set()
            if job1 is not None:
                job1.join(timeout=5.0)
            _settle(recorder)

            # Normal stop on take 2 succeeds through finish and on_capture_stopped.
            returned = recorder.stop_recording()
            assert returned is None
            assert recorder.stop_deferred is True
            _settle(recorder)

            on_capture_stopped.assert_called_once_with(created_paths[1])
            term1.assert_called_once_with()
            term2.assert_not_called()
        finally:
            gate1.set()
            if job1 is not None:
                job1.join(timeout=5.0)
            _settle(recorder)


def test_stale_start_host_stream_preserves_next_take_wav(tmp_path):
    """Old host start job does not clear or close next take's wav_file."""
    gate1 = threading.Event()
    take1_in_stream = threading.Event()
    take2_in_stream = threading.Event()

    call_count = 0
    created_paths: list[str] = []

    def fake_make_temp() -> str:
        nonlocal call_count
        call_count += 1
        path = str(tmp_path / f"host_{call_count}.wav")
        created_paths.append(path)
        return path

    stream_calls = 0
    take2_callback = None

    class FakeInputStream:
        def __init__(self, *args, **kwargs):
            nonlocal stream_calls, take2_callback
            stream_calls += 1
            if stream_calls == 1:
                take1_in_stream.set()
                gate1.wait(timeout=5.0)
            else:
                take2_callback = kwargs.get("callback")
                take2_in_stream.set()

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    mock_sd = MagicMock()
    mock_sd.RawInputStream = FakeInputStream

    on_capture_stopped = MagicMock()
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=(None, "")),
        patch("plugin.chatbot.audio_recorder.ensure_native_binaries_on_path"),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", side_effect=fake_make_temp),
        patch.dict("sys.modules", {"sounddevice": mock_sd}),
    ):
        recorder = AudioRecorder()
        recorder.set_auto_stop_callbacks(on_capture_stopped=on_capture_stopped)

        job1 = None
        try:
            recorder.start_recording()
            job1 = recorder._start_job
            assert take1_in_stream.wait(timeout=5.0)

            recorder.cleanup()
            recorder.start_recording()
            assert take2_in_stream.wait(timeout=5.0)
            _settle(recorder)

            # Release take 1. Epoch is stale; its exit must close wav1 only
            # and must not clear or close take 2's wav_file.
            gate1.set()
            if job1 is not None:
                job1.join(timeout=5.0)
            _settle(recorder)

            # Take 2 is recording; audio frames are delivered through its callback.
            assert take2_callback is not None
            take2_callback(b"\x00" * 32, 16, None, None)

            # Stop take 2 normally; it returns the path on the host path.
            returned = recorder.stop_recording()
            assert returned == created_paths[1]
            assert os.path.exists(created_paths[1])
            assert os.path.getsize(created_paths[1]) > 44
        finally:
            gate1.set()
            if job1 is not None:
                job1.join(timeout=5.0)
            _settle(recorder)


