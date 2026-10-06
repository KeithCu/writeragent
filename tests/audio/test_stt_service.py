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
    with pytest.raises(ConfigError, match='uv pip install faster-whisper "av<14"'):
        stt_service._transcribe_local("/tmp/a.wav", "base", None)


def test_probe_caches_present_package_without_pip(monkeypatch: pytest.MonkeyPatch) -> None:
    stt_service.clear_faster_whisper_probe_cache()
    py_exe = "/opt/venv/bin/python"
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd: list[str], timeout: float) -> _Result:
        del timeout
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr(stt_service, "_run_cmd", fake_run)
    assert stt_service.ensure_faster_whisper(py_exe) is True
    assert calls == [[py_exe, "-c", "import faster_whisper"]]
    # Cached success does not probe again and never pip-installs.
    assert stt_service.ensure_faster_whisper(py_exe) is True
    assert calls == [[py_exe, "-c", "import faster_whisper"]]
    stt_service.clear_faster_whisper_probe_cache()


def test_missing_package_errors_with_install_hint_and_does_not_pip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stt_service.clear_faster_whisper_probe_cache()
    py_exe = "/opt/venv/bin/python"
    calls: list[list[str]] = []

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "ModuleNotFoundError: faster_whisper"

    def fake_run(cmd: list[str], timeout: float) -> _Result:
        del timeout
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr(stt_service, "_run_cmd", fake_run)
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: py_exe)
    with pytest.raises(ConfigError, match='uv pip install faster-whisper "av<14"') as exc_info:
        stt_service._transcribe_local("/tmp/a.wav", "base", None)
    message = str(exc_info.value)
    assert "Python Test" in message
    assert "optional" in message
    assert calls == [[py_exe, "-c", "import faster_whisper"]]
    assert all("pip" not in part for cmd in calls for part in cmd)
    stt_service.clear_faster_whisper_probe_cache()


def test_present_package_transcribes_via_child_script(monkeypatch: pytest.MonkeyPatch) -> None:
    stt_service.clear_faster_whisper_probe_cache()
    py_exe = "/opt/venv/bin/python"
    calls: list[list[str]] = []
    statuses: list[str] = []

    class _Result:
        def __init__(self, returncode: int, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd: list[str], timeout: float) -> _Result:
        del timeout
        calls.append(list(cmd))
        if "import faster_whisper" in cmd:
            return _Result(0)
        return _Result(0, '{"status":"ok","text":"hello world"}\n')

    monkeypatch.setattr(stt_service, "_run_cmd", fake_run)
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: py_exe)
    monkeypatch.setattr(stt_service, "_local_whisper_weights_cached", lambda model: True)
    assert stt_service._transcribe_local("/tmp/a.wav", "small", statuses.append) == "hello world"
    assert calls[0] == [py_exe, "-c", "import faster_whisper"]
    assert calls[1][0] == py_exe
    assert calls[1][1].endswith("whisper_transcribe.py")
    assert calls[1][2:] == ["--wav", "/tmp/a.wav", "--model", "small"]
    assert len(calls) == 2
    assert all("pip" not in part for cmd in calls for part in cmd)
    assert statuses == ["Transcribing with local Whisper (small)…"]
    assert all("download" not in line for line in statuses)
    # Cached import probe: the next recording only runs the child script.
    assert stt_service._transcribe_local("/tmp/b.wav", "tiny", None) == "hello world"
    assert len(calls) == 3
    assert calls[2][2:] == ["--wav", "/tmp/b.wav", "--model", "tiny"]
    stt_service.clear_faster_whisper_probe_cache()


def test_uncached_weights_status_mentions_download(monkeypatch: pytest.MonkeyPatch) -> None:
    stt_service.clear_faster_whisper_probe_cache()
    py_exe = "/opt/venv/bin/python"
    statuses: list[str] = []

    class _Result:
        def __init__(self, returncode: int, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd: list[str], timeout: float) -> _Result:
        del timeout
        if "import faster_whisper" in cmd:
            return _Result(0)
        return _Result(0, '{"status":"ok","text":"hi"}\n')

    monkeypatch.setattr(stt_service, "_run_cmd", fake_run)
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: py_exe)
    monkeypatch.setattr(stt_service, "_local_whisper_weights_cached", lambda model: False)
    assert stt_service._transcribe_local("/tmp/a.wav", "tiny", statuses.append) == "hi"
    assert statuses == ["Transcribing with local Whisper (tiny)… The first run downloads the model."]
    stt_service.clear_faster_whisper_probe_cache()


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
    stt_service.clear_faster_whisper_probe_cache()
    py_exe = "/opt/venv/bin/python"
    cached = {"yes": True}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd: list[str], timeout: float) -> _Result | None:
        del timeout
        if "import faster_whisper" in cmd:
            return _Result()
        return None

    monkeypatch.setattr(stt_service, "_run_cmd", fake_run)
    monkeypatch.setattr(stt_service, "resolve_stt_python", lambda: py_exe)
    monkeypatch.setattr(stt_service, "_local_whisper_weights_cached", lambda model: cached["yes"])
    with pytest.raises(ConfigError, match="could not start") as cached_exc:
        stt_service._transcribe_local("/tmp/a.wav", "base", None)
    assert "download" not in str(cached_exc.value)
    cached["yes"] = False
    with pytest.raises(ConfigError, match="downloads model weights") as missing_exc:
        stt_service._transcribe_local("/tmp/a.wav", "base", None)
    assert "first run" in str(missing_exc.value)
    stt_service.clear_faster_whisper_probe_cache()


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
    assert completed is not None
    assert completed.returncode == 0
    assert "hello-stt" in completed.stdout


def test_run_cmd_stop_already_clicked_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Stop was already clicked before spawn, _run_cmd raises SttStopped without spawning."""
    spawned = False

    def _fail_popen(*_args: object, **_kwargs: object) -> subprocess.Popen[str]:
        nonlocal spawned
        spawned = True
        raise AssertionError("Process must not be spawned when Stop was already clicked")

    monkeypatch.setattr(stt_service.subprocess, "Popen", _fail_popen)
    with pytest.raises(stt_service.SttStopped):
        stt_service._run_cmd([sys.executable, "-c", "pass"], 10, stop_checker=lambda: True)
    assert not spawned


def test_transcribe_stop_already_clicked_raises_stt_stopped() -> None:
    """transcribe raises SttStopped when Stop was already clicked."""
    with pytest.raises(stt_service.SttStopped):
        stt_service.transcribe("/tmp/test.wav", stop_checker=lambda: True)


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
