import json
import os
import threading
import wave
from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.audio_recorder import AudioRecorder


@pytest.fixture
def ctx():
    return MagicMock()


@pytest.fixture
def recording_mocks(tmp_path):
    wav_path = str(tmp_path / "test.wav")
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
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


def test_audio_recorder_missing_venv(ctx):
    with patch(
        "plugin.chatbot.audio_recorder.resolve_recording_python",
        return_value=(None, "Configure Settings → Python"),
    ):
        recorder = AudioRecorder(ctx)
        with pytest.raises(RuntimeError, match="Configure Settings"):
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

    stop_event = threading.Event()

    def fake_record(output, event, *, on_stream_started=None):
        if on_stream_started is not None:
            on_stream_started()
        event.set()

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
    assert emitted[-1] == {"status": "ok", "path": os.path.abspath(output_path)}
