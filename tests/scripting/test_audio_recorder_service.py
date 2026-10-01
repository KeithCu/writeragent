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
