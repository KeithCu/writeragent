import os
import threading
import wave
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.xdist_group("audio_recorder_control")

from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.chatbot.audio_recorder import (
    AudioRecorder,
    clear_stub_recorder_control,
    write_stub_recorder_control,
)


def _recording_child(proc: MagicMock, drain: object | None = None) -> ManagedSubprocess:
    return ManagedSubprocess.wrap_existing(proc, label="audio-recorder", stderr_tail=drain)  # type: ignore[arg-type]


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
    child = _recording_child(proc)
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", return_value=wav_path),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", return_value=child) as spawn,
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


def test_audio_recorder_skip_spawn_from_control_file(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
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


def test_audio_recorder_stub_auto_stop_fires_after_start_returns(ctx, tmp_path, monkeypatch):
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
    recorder = AudioRecorder(ctx)
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
        from plugin.chatbot.audio_recorder import read_stub_recorder_control

        assert read_stub_recorder_control().get("auto_stop") is False
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


def test_audio_recorder_skip_spawn_hang_ready(ctx, monkeypatch):
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    recorder = AudioRecorder(ctx)
    recorder._test_skip_spawn = True
    write_stub_recorder_control(skip=True, hang_ready=True)
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            recorder.start_recording()
        assert recorder.state.status == "error"
    finally:
        clear_stub_recorder_control()


def test_audio_recorder_control_file_clears_hang_ready(ctx, tmp_path, monkeypatch):
    """G21 hang_ready must not stick on the live recorder for later Packet G cases."""
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
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


def test_audio_recorder_control_file_clears_fail_start(ctx, tmp_path, monkeypatch):
    """Same live recorder: JSON fail_start=null must not keep G12's crash flag."""
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
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
    child = _recording_child(proc)
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", return_value=wav_path),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", return_value=child),
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


# More than the 44-byte WAV header: a header-only file has no audio.
_WAV_WITH_AUDIO = b"RIFF" + b"\x00" * 60


def test_stop_keeps_nonempty_wav_when_handshake_fails(ctx, tmp_path):
    wav_path = tmp_path / "keep.wav"
    wav_path.write_bytes(_WAV_WITH_AUDIO)
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    child = _recording_child(proc)
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", return_value=str(wav_path)),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", return_value=child),
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
    assert wav_path.read_bytes() == _WAV_WITH_AUDIO


def test_stop_deletes_empty_wav_when_handshake_fails(ctx, tmp_path):
    wav_path = tmp_path / "empty.wav"
    wav_path.write_bytes(b"")
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    child = _recording_child(proc)
    with (
        patch("plugin.chatbot.audio_recorder.resolve_recording_python", return_value=("/usr/bin/python", "")),
        patch("plugin.chatbot.audio_recorder.make_temp_wav_path", return_value=str(wav_path)),
        patch("plugin.chatbot.audio_recorder.spawn_recording_process", return_value=child),
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
    wav_path.write_bytes(_WAV_WITH_AUDIO)
    recorder = AudioRecorder(ctx)
    recorder.state = AudioRecorderState(status="recording")
    recorder.temp_filename = str(wav_path)
    recorder._proc = MagicMock()

    recorder._notify_recording_error("child died")

    assert recorder.state.status == "error"
    assert recorder.temp_filename == str(wav_path)
    assert wav_path.read_bytes() == _WAV_WITH_AUDIO


def test_stdout_error_callback_does_not_apply_on_monitor(ctx, tmp_path):
    from plugin.chatbot.audio_recorder_state import AudioRecorderState

    wav_path = tmp_path / "partial.wav"
    wav_path.write_bytes(_WAV_WITH_AUDIO)
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


def test_record_to_wav_holds_silence_progress_until_ready(tmp_path):
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

    original_wait = stop_event.wait
    stop_event.wait = fake_wait

    try:
        with patch.object(var, "_import_sounddevice", return_value=mock_sd):
            var.record_to_wav(
                output_path,
                stop_event,
                on_stream_started=lambda: recorded.append("ready"),
                silence_config=config,
                on_ipc_emit=lambda payload: recorded.append(str(payload["status"])),
            )
    finally:
        stop_event.wait = original_wait

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
    proc = MagicMock()
    recorder._proc = proc
    monitor = MagicMock()
    handoff = MagicMock()
    recorder._stdout_monitor = monitor
    recorder._stop_handoff = handoff
    with (
        patch.object(recorder, "_apply_event", side_effect=RuntimeError("stop failed")),
        patch("plugin.chatbot.audio_recorder.terminate_recording_process") as term,
    ):
        recorder.cleanup()
    term.assert_called_once_with(proc, monitor=monitor, handoff=handoff)
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
        patch("plugin.chatbot.audio_recorder.ensure_native_binaries_on_path"),
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


def test_wav_file_has_bytes_treats_header_only_as_empty(tmp_path):
    from plugin.chatbot.audio_recorder import _wav_file_has_bytes

    header_only = tmp_path / "header.wav"
    header_only.write_bytes(b"\x00" * 44)
    with_audio = tmp_path / "audio.wav"
    with_audio.write_bytes(_WAV_WITH_AUDIO)
    assert _wav_file_has_bytes(str(header_only)) is False
    assert _wav_file_has_bytes(str(with_audio)) is True


def test_stub_control_file_ignored_without_testing_env(ctx, recording_mocks, monkeypatch, tmp_path):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    fixture = tmp_path / "inject.wav"
    fixture.write_bytes(b"RIFF....WAVEfmt ")
    write_stub_recorder_control(wav=str(fixture), skip=True)
    recorder = AudioRecorder(ctx)
    try:
        recorder.start_recording()
        assert recorder._test_skip_spawn is False
        recording_mocks["spawn"].assert_called_once()
        recorder.stop_recording()
    finally:
        clear_stub_recorder_control()


def test_notify_auto_stop_once_per_take(ctx, monkeypatch):
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    recorder = AudioRecorder(ctx)
    counts = 0

    def _on_auto_stop() -> None:
        nonlocal counts
        counts += 1

    recorder.set_auto_stop_callbacks(on_auto_stop=_on_auto_stop)
    recorder._notify_auto_stop("/tmp/a.wav")
    recorder._notify_auto_stop("/tmp/a.wav")
    assert counts == 1
    assert recorder._auto_stopped_path == "/tmp/a.wav"

    recorder._test_skip_spawn = True
    recorder.start_recording()
    assert recorder._auto_stop_notified is False

    recorder._notify_auto_stop("/tmp/b.wav")
    assert counts == 2
    assert recorder._auto_stopped_path == "/tmp/b.wav"
    recorder.stop_recording()


def test_stop_forwards_stdout_monitor(ctx, recording_mocks):
    recorder = AudioRecorder(ctx)
    try:
        recorder.start_recording()
        monitor = recorder._stdout_monitor
        handoff = recorder._stop_handoff
        assert monitor is not None
        assert handoff is not None
        recorder.stop_recording()
        call_kwargs = recording_mocks["stop_proc"].call_args.kwargs
        assert call_kwargs["monitor"] is monitor
        assert call_kwargs["handoff"] is handoff
    finally:
        if os.path.exists(recording_mocks["wav_path"]):
            os.remove(recording_mocks["wav_path"])


def test_error_after_ready_reaps_with_the_stdout_monitor(ctx, tmp_path):
    from plugin.chatbot.audio_recorder_state import AudioRecorderState
    from plugin.scripting.audio_recorder_service import RecordingStopHandoff

    wav_path = tmp_path / "partial.wav"
    wav_path.write_bytes(_WAV_WITH_AUDIO)
    recorder = AudioRecorder(ctx)
    recorder.state = AudioRecorderState(status="recording")
    recorder.temp_filename = str(wav_path)
    proc = MagicMock()
    recorder._proc = proc
    monitor = MagicMock()
    monitor.is_current_thread.return_value = False
    recorder._stdout_monitor = monitor
    handoff = RecordingStopHandoff()
    recorder._stop_handoff = handoff

    with patch("plugin.chatbot.audio_recorder.terminate_recording_process") as term:
        recorder._notify_recording_error("child died")

    term.assert_called_once_with(proc, monitor=monitor, handoff=handoff)
    assert wav_path.is_file()


def test_next_recording_deletes_wav_kept_after_stdout_error(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    from plugin.chatbot.audio_recorder_state import AudioRecorderState

    old_wav = tmp_path / "old.wav"
    old_wav.write_bytes(_WAV_WITH_AUDIO)
    recorder = AudioRecorder(ctx)
    recorder.state = AudioRecorderState(status="recording")
    recorder.temp_filename = str(old_wav)
    recorder._proc = MagicMock()

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

