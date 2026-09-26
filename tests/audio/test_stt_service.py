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
    text = stt_service.status_for_transcription()
    assert "base" in text
    assert text != "Transcribing audio..."


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
    client.transcribe_audio.assert_called_once_with("/tmp/a.wav", model="whisper-1")
    assert called["local"] is False


def test_local_transcribe_does_not_call_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stt_service, "uses_local_stt", lambda: True)
    monkeypatch.setattr(stt_service, "get_stt_local_model", lambda: "small")
    monkeypatch.setattr(stt_service, "_transcribe_local", lambda wav, model, on_status: f"{model}:{wav}")
    client = MagicMock()
    assert stt_service.transcribe("/tmp/a.wav", client=client, model="whisper-1") == "small:/tmp/a.wav"
    client.transcribe_audio.assert_not_called()


def test_local_without_venv_names_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: None)
    with pytest.raises(ConfigError, match="uv pip install faster-whisper"):
        stt_service._transcribe_local("/tmp/a.wav", "base", None)


def test_ensure_installs_into_venv_python(monkeypatch: pytest.MonkeyPatch) -> None:
    stt_service.clear_faster_whisper_probe_cache()
    py_exe = "/opt/venv/bin/python"
    calls: list[list[str]] = []
    probes = {"n": 0}

    class _Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    def fake_run(cmd: list[str], timeout: float) -> _Result:
        del timeout
        calls.append(list(cmd))
        if "import faster_whisper" in cmd:
            probes["n"] += 1
            return _Result(1 if probes["n"] == 1 else 0)
        return _Result(0)

    monkeypatch.setattr(stt_service, "_run_cmd", fake_run)
    monkeypatch.setattr(stt_service.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    statuses: list[str] = []
    assert stt_service.ensure_faster_whisper(py_exe, on_status=statuses.append) is True
    assert calls[0] == [py_exe, "-c", "import faster_whisper"]
    assert calls[1] == ["/usr/bin/uv", "pip", "install", "--python", py_exe, "faster-whisper"]
    assert sys.executable not in calls[1]
    assert any("Installing faster-whisper" in line for line in statuses)
    # Cached success does not probe again.
    assert stt_service.ensure_faster_whisper(py_exe) is True
    assert len(calls) == 3
    stt_service.clear_faster_whisper_probe_cache()


def test_ensure_failure_uses_venv_python(monkeypatch: pytest.MonkeyPatch) -> None:
    stt_service.clear_faster_whisper_probe_cache()
    py_exe = "/opt/venv/bin/python"
    calls: list[list[str]] = []

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "no wheel"

    def fake_run(cmd: list[str], timeout: float) -> _Result:
        del timeout
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr(stt_service, "_run_cmd", fake_run)
    monkeypatch.setattr(stt_service.shutil, "which", lambda name: None)
    assert stt_service.ensure_faster_whisper(py_exe) is False
    assert [py_exe, "-m", "pip", "install", "faster-whisper"] in calls
    assert all(sys.executable not in cmd for cmd in calls)
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: py_exe)
    monkeypatch.setattr(stt_service, "ensure_faster_whisper", lambda py, on_status=None: False)
    with pytest.raises(ConfigError, match="uv pip install faster-whisper"):
        stt_service._transcribe_local("/tmp/a.wav", "base", None)
    stt_service.clear_faster_whisper_probe_cache()


def test_parse_whisper_stdout() -> None:
    assert stt_service.parse_whisper_stdout('noise\n{"status":"ok","text":" hello "}\n') == " hello "
    with pytest.raises(ConfigError, match="Local Whisper failed"):
        stt_service.parse_whisper_stdout('{"status":"error","message":"cpu rejected int8"}')
    with pytest.raises(ConfigError, match="did not return a transcript"):
        stt_service.parse_whisper_stdout("")


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
