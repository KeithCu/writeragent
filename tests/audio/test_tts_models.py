# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for TTS model downloading and cache resolution."""

import io
import os
import urllib.error
from unittest.mock import patch

from plugin.audio.model_cache_paths import (
    KOKORO_MODEL_FILENAME,
    KOKORO_VOICES_FILENAME,
)
from plugin.audio.tts_models import (
    _KOKORO_RELEASE_BASE,
    _download_to,
    _resolve_kokoro_model_files,
    _resolve_piper_model_file,
    clear_generation_downloads,
    is_download_failed,
    remember_download_failed,
)


def _isolate_home_cache(tmp_path, monkeypatch):
    """Point ``Path.home() / '.cache'`` at a tmp dir and ignore ``XDG_CACHE_HOME``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    return home / ".cache"


def _kokoro_cache(tmp_path, monkeypatch):
    return _isolate_home_cache(tmp_path, monkeypatch) / "kokoro"


def _piper_cache(tmp_path, monkeypatch):
    return _isolate_home_cache(tmp_path, monkeypatch) / "piper"


def test_download_to_cancellable_and_cleans_tmp(tmp_path):
    """Bug 1: _download_to aborts when cancelled and cleans up .tmp file."""
    dest = tmp_path / "model.bin"
    tmp = tmp_path / "model.bin.tmp"

    # Test cancellation mid-download
    call_count = 0

    class SlowStream(io.BytesIO):
        def read(self, n=-1):
            nonlocal call_count
            call_count += 1
            if call_count > 2:
                return b""
            return b"chunk"

    def cancelled_after_first():
        return call_count >= 1

    with patch("urllib.request.urlopen", return_value=SlowStream(b"data")):
        ok = _download_to("https://example.com/model", str(dest), timeout=10.0, cancelled=cancelled_after_first)
        assert not ok
        assert not dest.exists()
        assert not tmp.exists()

    # Test failure on URLError cleans up .tmp
    def fail_open(*args, **kwargs):
        raise urllib.error.URLError("network down")

    with patch("urllib.request.urlopen", side_effect=fail_open):
        ok = _download_to("https://example.com/model", str(dest), timeout=10.0)
        assert not ok
        assert not dest.exists()
        assert not tmp.exists()

    # Test success atomically renames .tmp to dest
    with patch("urllib.request.urlopen", return_value=io.BytesIO(b"full-model-data")):
        ok = _download_to("https://example.com/model", str(dest), timeout=10.0)
        assert ok
        assert dest.exists()
        assert dest.read_bytes() == b"full-model-data"
        assert not tmp.exists()


def test_download_failure_remembered_per_generation():
    """Bug 1: Failed model downloads are remembered per generation and not retried every sentence."""
    clear_generation_downloads(1)
    clear_generation_downloads(2)

    assert not is_download_failed(1, "kokoro")
    remember_download_failed(1, "kokoro")
    assert is_download_failed(1, "kokoro")
    # Different generation should not be affected
    assert not is_download_failed(2, "kokoro")

    # Clear generation 1
    clear_generation_downloads(1)
    assert not is_download_failed(1, "kokoro")


def test_resolve_piper_model_file_ondemand_download(tmp_path, monkeypatch):
    cache_dir = _piper_cache(tmp_path, monkeypatch)
    with patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: io.BytesIO(b"fake-piper-model-bytes")):
        resolved = _resolve_piper_model_file("de_DE-thorsten-medium")
        assert resolved == str(cache_dir / "de_DE-thorsten-medium.onnx")
        assert os.path.exists(resolved)
        assert os.path.exists(str(cache_dir / "de_DE-thorsten-medium.onnx.json"))


def test_resolve_piper_model_file_reports_download_status(tmp_path, monkeypatch):
    messages: list[str] = []
    cache_dir = _piper_cache(tmp_path, monkeypatch)
    with patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: io.BytesIO(b"fake-piper-model-bytes")):
        resolved = _resolve_piper_model_file("de_DE-thorsten-medium", on_status=messages.append)

    assert resolved == str(cache_dir / "de_DE-thorsten-medium.onnx")
    assert os.path.exists(resolved)
    assert messages == ["Downloading Piper voice Thorsten…"]


def test_resolve_piper_model_file_reports_lessac_fallback(tmp_path, monkeypatch):
    messages: list[str] = []
    cache_dir = _piper_cache(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True)
    (cache_dir / "en_US-lessac-medium.onnx").write_bytes(b"lessac")
    (cache_dir / "en_US-lessac-medium.onnx.json").write_bytes(b"{}")

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
        resolved = _resolve_piper_model_file("de_DE-thorsten-medium", on_status=messages.append)

    assert resolved == str(cache_dir / "en_US-lessac-medium.onnx")
    assert messages == [
        "Downloading Piper voice Thorsten…",
        "Couldn't download Thorsten; using Lessac",
    ]


def test_resolve_piper_model_file_reports_os_speech_fallback_and_returns_none(tmp_path, monkeypatch):
    """Bug 4: After failed download, returns None so caller falls back immediately."""
    messages: list[str] = []
    _piper_cache(tmp_path, monkeypatch)

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
        resolved = _resolve_piper_model_file("de_DE-thorsten-medium", on_status=messages.append)

    assert resolved is None
    assert messages[0] == "Downloading Piper voice Thorsten…"
    assert "using Lessac" in messages[1]
    assert messages[-1] == "Couldn't download Thorsten; using OS speech"


def test_corrupt_download_does_not_leave_broken_file(tmp_path, monkeypatch):
    _kokoro_cache(tmp_path, monkeypatch)

    def mock_urlopen_fail(req, timeout=None):
        raise urllib.error.URLError("offline")

    with patch("os.makedirs"), \
         patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}), \
         patch("urllib.request.urlopen", side_effect=mock_urlopen_fail):
        model_path, voices_path = _resolve_kokoro_model_files(on_status=lambda x: None)

    assert not os.path.exists(model_path)
    assert not os.path.exists(voices_path)
    assert model_path.endswith("kokoro-v1.0.onnx")


def test_resolve_kokoro_model_files_reports_download_failure(tmp_path, monkeypatch):
    messages: list[str] = []
    _kokoro_cache(tmp_path, monkeypatch)

    with patch("os.makedirs"), \
         patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}), \
         patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
        model_path, voices_path = _resolve_kokoro_model_files(on_status=messages.append)

    assert model_path.endswith("kokoro-v1.0.onnx")
    assert voices_path.endswith("voices-v1.0.bin")
    assert messages == [
        "Downloading Kokoro voice model…",
        "Couldn't download Kokoro; using OS speech",
    ]


def test_resolve_kokoro_model_files_downloads_multilingual_release(tmp_path, monkeypatch):
    cache_dir = _kokoro_cache(tmp_path, monkeypatch)
    downloaded: list[tuple[str, str]] = []

    def _fake_download_to(url: str, dest: str, **kwargs) -> bool:
        downloaded.append((url, dest))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(b"asset")
        return True

    with patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}, clear=False), \
         patch("plugin.audio.tts_models._download_to", side_effect=_fake_download_to):
        model_path, voices_path = _resolve_kokoro_model_files()

    urls = [url for url, _dest in downloaded]
    assert urls == [
        f"{_KOKORO_RELEASE_BASE}/{KOKORO_VOICES_FILENAME}",
        f"{_KOKORO_RELEASE_BASE}/{KOKORO_MODEL_FILENAME}",
    ]
    assert _KOKORO_RELEASE_BASE.endswith("/model-files-v1.1")
    assert "/model-files/" not in _KOKORO_RELEASE_BASE
    assert "v0_19" not in KOKORO_MODEL_FILENAME
    assert KOKORO_VOICES_FILENAME != "voices.bin"
    assert model_path == str(cache_dir / "kokoro-v1.0.onnx")
    assert voices_path == str(cache_dir / "voices-v1.0.bin")


def test_resolve_kokoro_model_files_supersedes_english_only_cache(tmp_path, monkeypatch):
    cache_dir = _kokoro_cache(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True)
    (cache_dir / "kokoro-v0_19.onnx").write_bytes(b"old-model")
    (cache_dir / "voices.bin").write_bytes(b"old-voices")
    downloaded: list[str] = []

    def _fake_download_to(url: str, dest: str, **kwargs) -> bool:
        downloaded.append(url)
        with open(dest, "wb") as handle:
            handle.write(b"new")
        return True

    with patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}, clear=False), \
         patch("plugin.audio.tts_models._download_to", side_effect=_fake_download_to):
        model_path, voices_path = _resolve_kokoro_model_files()

    assert model_path == str(cache_dir / "kokoro-v1.0.onnx")
    assert voices_path == str(cache_dir / "voices-v1.0.bin")
    assert downloaded == [
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin",
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.onnx",
    ]
    assert (cache_dir / "kokoro-v0_19.onnx").read_bytes() == b"old-model"
    assert (cache_dir / "voices.bin").read_bytes() == b"old-voices"


def test_resolve_kokoro_model_files_reuses_v1_cache(tmp_path, monkeypatch):
    cache_dir = _kokoro_cache(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True)
    (cache_dir / "kokoro-v0_19.onnx").write_bytes(b"old-model")
    (cache_dir / "voices.bin").write_bytes(b"old-voices")
    (cache_dir / "kokoro-v1.0.onnx").write_bytes(b"model")
    (cache_dir / "voices-v1.0.bin").write_bytes(b"voices")

    with patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}, clear=False), \
         patch("plugin.audio.tts_models._download_to") as download_mock:
        model_path, voices_path = _resolve_kokoro_model_files()

    download_mock.assert_not_called()
    assert model_path == str(cache_dir / "kokoro-v1.0.onnx")
    assert voices_path == str(cache_dir / "voices-v1.0.bin")


def test_resolve_kokoro_model_files_honors_env_overrides(tmp_path):
    model = tmp_path / "custom-model.onnx"
    voices = tmp_path / "custom-voices.bin"
    model.write_bytes(b"m")
    voices.write_bytes(b"v")

    with patch.dict(
        "os.environ",
        {"KOKORO_MODEL_PATH": str(model), "KOKORO_VOICES_PATH": str(voices)},
    ), patch("plugin.audio.tts_models._download_to") as download_mock:
        model_path, voices_path = _resolve_kokoro_model_files()

    download_mock.assert_not_called()
    assert model_path == str(model)
    assert voices_path == str(voices)


def test_resolve_kokoro_model_files_reuses_pipecat_without_download(tmp_path, monkeypatch):
    root = _isolate_home_cache(tmp_path, monkeypatch)
    pipecat = root / "pipecat" / "kokoro-onnx"
    pipecat.mkdir(parents=True)
    (pipecat / "kokoro-v1.0.onnx").write_bytes(b"model")
    (pipecat / "voices-v1.0.bin").write_bytes(b"voices")

    with patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}, clear=False), \
         patch("plugin.audio.tts_models._download_to") as download_mock:
        model_path, voices_path = _resolve_kokoro_model_files()

    download_mock.assert_not_called()
    assert model_path == str(pipecat / "kokoro-v1.0.onnx")
    assert voices_path == str(pipecat / "voices-v1.0.bin")
    assert not (root / "kokoro").exists()


def test_resolve_kokoro_model_files_downloads_missing_sibling_beside_probe_hit(tmp_path, monkeypatch):
    root = _isolate_home_cache(tmp_path, monkeypatch)
    pipecat = root / "pipecat" / "kokoro-onnx"
    pipecat.mkdir(parents=True)
    (pipecat / "kokoro-v1.0.onnx").write_bytes(b"model")
    downloaded: list[str] = []

    def _fake_download_to(url: str, dest: str, **kwargs) -> bool:
        downloaded.append(dest)
        with open(dest, "wb") as handle:
            handle.write(b"voices")
        return True

    with patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}, clear=False), \
         patch("plugin.audio.tts_models._download_to", side_effect=_fake_download_to):
        model_path, voices_path = _resolve_kokoro_model_files()

    assert model_path == str(pipecat / "kokoro-v1.0.onnx")
    assert voices_path == str(pipecat / "voices-v1.0.bin")
    assert downloaded == [str(pipecat / "voices-v1.0.bin")]
    assert not (root / "kokoro").exists()


def test_resolve_piper_model_file_reuses_pipecat_without_download(tmp_path, monkeypatch):
    voice_dir = _isolate_home_cache(tmp_path, monkeypatch) / "pipecat" / "piper"
    voice_dir.mkdir(parents=True)
    onnx = voice_dir / "de_DE-thorsten-medium.onnx"
    onnx.write_bytes(b"onnx")
    (voice_dir / "de_DE-thorsten-medium.onnx.json").write_bytes(b"{}")

    with patch("urllib.request.urlopen") as urlopen:
        resolved = _resolve_piper_model_file("de_DE-thorsten-medium")

    urlopen.assert_not_called()
    assert resolved == str(onnx)
