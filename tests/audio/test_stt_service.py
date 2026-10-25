# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""STT provider dispatch: endpoint client vs local faster-whisper in the user venv."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plugin.audio import stt_service
from plugin.framework.errors import ConfigError

_REPO = Path(__file__).resolve().parents[2]


def _completed_process(
    args: list[str] | None = None,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args or [], returncode, stdout, stderr)


def test_module_yaml_stt_defaults() -> None:
    import yaml

    data = yaml.safe_load((_REPO / "plugin" / "audio" / "module.yaml").read_text(encoding="utf-8"))
    config = data["config"]
    assert config["stt_provider"]["default"] == "endpoint"
    assert config["stt_local_model"]["default"] == "base"
    values = [opt["value"] for opt in config["stt_provider"]["options"]]
    assert values == ["endpoint", "local"]
    sizes = [opt["value"] for opt in config["stt_local_model"]["options"]]
    assert sizes == ["tiny", "base", "small", "medium"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "endpoint"),
        (None, "endpoint"),
        ("endpoint", "endpoint"),
        ("LLM Endpoint", "endpoint"),
        ("system", "endpoint"),
        ("local", "local"),
        ("whisper", "local"),
        ("faster-whisper", "local"),
        ("faster_whisper", "local"),
        ("Local Whisper (faster-whisper)", "local"),
    ],
)
def test_normalize_stt_provider(raw: object, expected: str) -> None:
    assert stt_service.normalize_stt_provider(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "base"),
        (None, "base"),
        ("BASE", "base"),
        ("small", "small"),
        ("base (~150 MB)", "base"),
        ("tiny (~75 MB)", "tiny"),
        ("medium (~1.5 GB)", "medium"),
        ("Systran/faster-whisper-base", "Systran/faster-whisper-base"),
        ("not a model", "base"),
        ("--wav", "base"),
        ("-h", "base"),
    ],
)
def test_normalize_stt_local_model(raw: object, expected: str) -> None:
    assert stt_service.normalize_stt_local_model(raw) == expected


def test_stt_controls_enabled_matches_provider() -> None:
    assert stt_service.stt_controls_enabled("endpoint") == (True, False)
    assert stt_service.stt_controls_enabled("Local Whisper (faster-whisper)") == (False, True)
    assert stt_service.stt_controls_enabled("") == (True, False)


def test_missing_schema_key_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(key: str) -> str:
        del key
        raise ConfigError("missing")

    monkeypatch.setattr(stt_service, "get_config", boom)
    assert stt_service.get_stt_provider() == "endpoint"
    assert stt_service.get_stt_local_model() == "base"
    assert stt_service.uses_local_stt() is False


def test_status_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stt_service, "uses_local_stt", lambda: False)
    assert stt_service.status_for_transcription() == "Transcribing audio..."
    monkeypatch.setattr(stt_service, "uses_local_stt", lambda: True)
    monkeypatch.setattr(stt_service, "get_stt_local_model", lambda: "base")
    monkeypatch.setattr(stt_service, "_local_whisper_weights_cached", lambda model: True)
    text = stt_service.status_for_transcription()
    assert text == "Transcribing with local Whisper (base)…"
    assert "download" not in text
    monkeypatch.setattr(stt_service, "_local_whisper_weights_cached", lambda model: False)
    text = stt_service.status_for_transcription()
    assert text == "Transcribing with local Whisper (base)… The first run downloads the model."


def test_endpoint_transcribe_calls_client_not_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stt_service, "uses_local_stt", lambda: False)
    called = {"local": False}

    def _local(*_args: object, **_kwargs: object) -> str:
        called["local"] = True
        return "nope"

    monkeypatch.setattr(stt_service, "_transcribe_local", _local)
    client = MagicMock()
    client.transcribe_audio.return_value = "from endpoint"
    assert stt_service.transcribe("/tmp/a.wav", client=client, model="whisper-1") == "from endpoint"
    client.transcribe_audio.assert_called_once_with("/tmp/a.wav", model="whisper-1", status_callback=None)
    assert called["local"] is False

    cb = MagicMock()
    stt_service.transcribe("/tmp/a.wav", client=client, model="whisper-1", on_status=cb)
    client.transcribe_audio.assert_called_with("/tmp/a.wav", model="whisper-1", status_callback=cb)
    cb.assert_not_called()


def test_local_transcribe_does_not_call_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stt_service, "uses_local_stt", lambda: True)
    monkeypatch.setattr(stt_service, "get_stt_local_model", lambda: "small")
    monkeypatch.setattr(stt_service, "_transcribe_local", lambda wav, model, on_status=None, on_spawn=None: f"{model}:{wav}")
    client = MagicMock()
    assert stt_service.transcribe("/tmp/a.wav", client=client, model="whisper-1") == "small:/tmp/a.wav"
    client.transcribe_audio.assert_not_called()


def test_local_transcribe_emits_on_status(monkeypatch: pytest.MonkeyPatch) -> None:
    py_exe = "/opt/venv/bin/python"
    statuses: list[str] = []

    def fake_run(cmd: list[str], timeout: float, **kwargs: object) -> stt_service.SttCommandResult:
        del timeout, kwargs
        return stt_service.SttCommandResult(0, '{"status":"ok","text":"hi"}\n', "")

    monkeypatch.setattr(stt_service, "_run_cmd", fake_run)
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: py_exe)
    monkeypatch.setattr(stt_service, "_local_whisper_weights_cached", lambda model: False)
    assert stt_service._transcribe_local("/tmp/a.wav", "tiny", statuses.append) == "hi"
    assert statuses == ["Transcribing with local Whisper (tiny)… The first run downloads the model."]


def test_local_without_venv_names_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: None)
    with pytest.raises(ConfigError, match='uv pip install faster-whisper "av<14"'):
        stt_service._transcribe_local("/tmp/a.wav", "base")


def test_missing_package_errors_with_install_hint_and_does_not_pip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    py_exe = "/opt/venv/bin/python"
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: float, **kwargs: object) -> stt_service.SttCommandResult:
        del timeout, kwargs
        calls.append(list(cmd))
        return stt_service.SttCommandResult(
            1,
            '{"status":"error","message":"faster-whisper is not installed (No module named faster_whisper)"}\n',
            "",
        )

    monkeypatch.setattr(stt_service, "_run_cmd", fake_run)
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: py_exe)
    with pytest.raises(ConfigError, match='uv pip install faster-whisper "av<14"') as exc_info:
        stt_service._transcribe_local("/tmp/a.wav", "base")
    message = str(exc_info.value)
    assert "Python Test" in message
    assert "optional" in message
    assert len(calls) == 1
    assert calls[0][0] == py_exe
    assert calls[0][1].endswith("whisper_transcribe.py")
    assert all("pip" not in part for cmd in calls for part in cmd)


def test_present_package_transcribes_via_child_script(monkeypatch: pytest.MonkeyPatch) -> None:
    py_exe = "/opt/venv/bin/python"
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: float, **kwargs: object) -> stt_service.SttCommandResult:
        del timeout, kwargs
        calls.append(list(cmd))
        return stt_service.SttCommandResult(0, '{"status":"ok","text":"hello world"}\n', "")

    monkeypatch.setattr(stt_service, "_run_cmd", fake_run)
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: py_exe)
    monkeypatch.setattr(stt_service, "_local_whisper_weights_cached", lambda model: True)
    assert stt_service._transcribe_local("/tmp/a.wav", "small") == "hello world"
    assert calls[0][0] == py_exe
    assert calls[0][1].endswith("whisper_transcribe.py")
    assert calls[0][2:] == ["--wav", "/tmp/a.wav", "--model=small"]
    assert len(calls) == 1
    assert all("pip" not in part for cmd in calls for part in cmd)
    assert stt_service._transcribe_local("/tmp/b.wav", "tiny") == "hello world"
    assert len(calls) == 2
    assert calls[1][2:] == ["--wav", "/tmp/b.wav", "--model=tiny"]


def test_uncached_weights_status_mentions_download(monkeypatch: pytest.MonkeyPatch) -> None:
    py_exe = "/opt/venv/bin/python"

    def fake_run(cmd: list[str], timeout: float, **kwargs: object) -> stt_service.SttCommandResult:
        del timeout, kwargs
        return stt_service.SttCommandResult(0, '{"status":"ok","text":"hi"}\n', "")

    monkeypatch.setattr(stt_service, "_run_cmd", fake_run)
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: py_exe)
    monkeypatch.setattr(stt_service, "_local_whisper_weights_cached", lambda model: False)
    assert stt_service._transcribe_local("/tmp/a.wav", "tiny") == "hi"
    assert "download" in stt_service._local_whisper_status("tiny")


def _write_hub_snapshot(cache: Path, repo_id: str, payload: bytes = b"weights") -> Path:
    snap = cache / ("models--" + repo_id.replace("/", "--")) / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    weights = snap / "model.bin"
    weights.write_bytes(payload)
    return weights


def test_weights_cached_when_snapshot_model_bin_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    assert stt_service._local_whisper_weights_cached("tiny") is False
    _write_hub_snapshot(tmp_path, "Systran/faster-whisper-tiny")
    assert stt_service._local_whisper_weights_cached("tiny") is True
    # A different size does not count, and the size alias shares the HF id's folder.
    assert stt_service._local_whisper_weights_cached("base") is False
    assert stt_service._local_whisper_weights_cached("Systran/faster-whisper-tiny") is True
    assert stt_service._local_whisper_status("tiny") == "Transcribing with local Whisper (tiny)…"
    assert "download" in stt_service._local_whisper_status("base")


def test_empty_or_missing_model_bin_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    weights = _write_hub_snapshot(tmp_path, "Systran/faster-whisper-small", payload=b"")
    assert stt_service._local_whisper_weights_cached("small") is False
    weights.unlink()
    weights.symlink_to(tmp_path / "no-such-blob")
    assert stt_service._local_whisper_weights_cached("small") is False
    # An existing directory is loaded in place; WhisperModel does not download it.
    local_dir = tmp_path / "local-ct2"
    local_dir.mkdir()
    assert stt_service._local_whisper_weights_cached(str(local_dir)) is True


def test_timeout_mentions_download_only_when_weights_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    py_exe = "/opt/venv/bin/python"
    cached = {"yes": True}

    def fake_run(cmd: list[str], timeout: float, **kwargs: object) -> stt_service.SttCommandResult:
        del cmd, timeout, kwargs
        return stt_service.SttCommandResult(None, "", "", timed_out=True)

    monkeypatch.setattr(stt_service, "_run_cmd", fake_run)
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: py_exe)
    monkeypatch.setattr(stt_service, "_local_whisper_weights_cached", lambda model: cached["yes"])
    with pytest.raises(ConfigError, match="timed out") as cached_exc:
        stt_service._transcribe_local("/tmp/a.wav", "base")
    assert "download" not in str(cached_exc.value)
    assert "could not start" not in str(cached_exc.value)
    cached["yes"] = False
    with pytest.raises(ConfigError, match="downloads model weights") as missing_exc:
        stt_service._transcribe_local("/tmp/a.wav", "base")
    assert "first run" in str(missing_exc.value)


def test_hf_home_cache_dir_is_hub_subdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    assert stt_service._hf_hub_cache_dir() == str(tmp_path / "hub")
    _write_hub_snapshot(tmp_path / "hub", "Systran/faster-whisper-medium")
    assert stt_service._local_whisper_weights_cached("medium") is True


def test_parse_whisper_stdout() -> None:
    assert stt_service.parse_whisper_stdout('noise\n{"status":"ok","text":" hello "}\n') == " hello "
    with pytest.raises(ConfigError, match="Local Whisper failed"):
        stt_service.parse_whisper_stdout('{"status":"error","message":"cpu rejected int8"}')
    with pytest.raises(ConfigError, match="did not return a transcript"):
        stt_service.parse_whisper_stdout("")


def test_run_cmd_returns_child_stdout() -> None:
    completed = stt_service._run_cmd([sys.executable, "-c", "print('hello-stt')"], 30)
    assert isinstance(completed, stt_service.SttCommandResult)
    assert completed.returncode == 0
    assert completed.timed_out is False
    assert completed.spawn_failed is False
    assert "hello-stt" in completed.stdout


def test_timeout_is_not_a_missing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    py_exe = "/opt/venv/bin/python"

    def fake_run(cmd: list[str], timeout: float, **kwargs: object) -> stt_service.SttCommandResult:
        del cmd, timeout, kwargs
        return stt_service.SttCommandResult(None, "", "", timed_out=True)

    monkeypatch.setattr(stt_service, "_run_cmd", fake_run)
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: py_exe)
    with pytest.raises(ConfigError) as exc_info:
        stt_service._transcribe_local("/tmp/a.wav", "base")
    message = str(exc_info.value)
    assert "timed out" in message
    assert "could not start" not in message
    assert "uv pip install" not in message


def test_run_cmd_closes_pipes(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 0
    mock_proc.returncode = 0
    mock_proc.wait.return_value = 0
    mock_stdout = MagicMock()
    mock_stdout.read.return_value = ""
    mock_stderr = MagicMock()
    mock_stderr.read.return_value = ""
    mock_proc.stdout = mock_stdout
    mock_proc.stderr = mock_stderr

    monkeypatch.setattr(stt_service.subprocess, "Popen", lambda *args, **kwargs: mock_proc)
    res = stt_service._run_cmd([sys.executable, "-c", "pass"], 5)
    assert isinstance(res, stt_service.SttCommandResult)
    mock_stdout.close.assert_called_once()
    mock_stderr.close.assert_called_once()


def test_spawn_failure_reports_could_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    py_exe = "/opt/venv/bin/python"
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: py_exe)

    def fake_popen(*_args: object, **_kwargs: object):
        raise FileNotFoundError("interpreter not found")

    monkeypatch.setattr(stt_service.subprocess, "Popen", fake_popen)
    with pytest.raises(ConfigError) as exc_info:
        stt_service._transcribe_local("/tmp/a.wav", "base")
    msg = str(exc_info.value)
    assert "could not start" in msg
    assert "timed out" not in msg
    assert "uv pip install" not in msg


def test_killed_child_no_json_reports_exit_code_and_smaller_model(monkeypatch: pytest.MonkeyPatch) -> None:
    py_exe = "/opt/venv/bin/python"
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: py_exe)
    monkeypatch.setattr(
        stt_service,
        "_run_cmd",
        lambda cmd, timeout, **kwargs: stt_service.SttCommandResult(-9, "", "Killed"),
    )
    with pytest.raises(ConfigError) as exc_info:
        stt_service._transcribe_local("/tmp/a.wav", "base")
    msg = str(exc_info.value)
    assert "-9" in msg
    assert "Killed" in msg
    assert "smaller" in msg


def test_json_error_wins_over_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    py_exe = "/opt/venv/bin/python"
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: py_exe)
    monkeypatch.setattr(
        stt_service,
        "_run_cmd",
        lambda cmd, timeout, **kwargs: stt_service.SttCommandResult(
            1, '{"status":"error","message":"cpu rejected int8"}\n', "some stderr"
        ),
    )
    with pytest.raises(ConfigError) as exc_info:
        stt_service._transcribe_local("/tmp/a.wav", "base")
    msg = str(exc_info.value)
    assert "Local Whisper failed: cpu rejected int8" in msg
    assert "exited" not in msg


def test_child_script_missing_package_is_json() -> None:
    """The venv entry reports a missing import as JSON and does not touch the network."""
    import importlib.util

    if importlib.util.find_spec("faster_whisper") is not None:
        pytest.skip("faster-whisper is installed; missing-package path is not this interpreter")
    script = _REPO / "plugin" / "audio" / "whisper_transcribe.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--wav", "/tmp/unused.wav", "--model", "base"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 1
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["status"] == "error"
    assert "faster-whisper" in payload["message"]
