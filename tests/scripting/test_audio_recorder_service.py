import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.audio_recorder_service import (
    is_audio_recording_configured,
    resolve_recording_python,
    stop_recording_process,
    wait_for_recording_ready,
)


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


def test_audio_record_main_accepts_json_and_legacy_stop_commands():
    from plugin.scripting.venv.audio_record_main import _is_stop_command

    assert _is_stop_command(json.dumps({"command": "stop"}))
    assert _is_stop_command("stop\n")
    assert not _is_stop_command(json.dumps({"command": "continue"}))
