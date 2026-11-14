# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for TTS model downloading and cache resolution."""

import email.message
import io
import os
import socket
import threading
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from plugin.audio.model_cache_paths import (
    KOKORO_MODEL_FILENAME,
    KOKORO_VOICES_FILENAME,
)
from plugin.audio.tts_models import (
    _KOKORO_RELEASE_BASE,
    clear_tts_status,
    _download_piper_pair,
    _download_to,
    DownloadOutcome,
    notify_tts_status,
    resolve_kokoro_model_files,
    resolve_piper_model_file,
)
from plugin.audio.tts_service import Utterance
from plugin.framework.send_cancellation import SendCancellation, is_cancelled


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """Skip retry backoff sleeps while preserving cancellation checks."""
    monkeypatch.setattr(
        "plugin.audio.tts_models._sleep_cancellable",
        lambda unused_duration, cancel: not is_cancelled(cancel),
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

    call_count = 0
    cancel = SendCancellation()

    class SlowStream(io.BytesIO):
        def read(self, n=-1):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                cancel.cancel()
            if call_count > 2:
                return b""
            return b"chunk"

    with patch("urllib.request.urlopen", return_value=SlowStream(b"data")):
        outcome = _download_to("https://example.com/model", str(dest), timeout=10.0, cancel=cancel)
        assert outcome is DownloadOutcome.CANCELLED
        assert not dest.exists()
        assert list(tmp_path.glob("*.tmp")) == []
        assert list(tmp_path.glob("writeragent_tts_*")) == []

    def fail_open(*args, **kwargs):
        raise urllib.error.URLError("network down")

    with patch("urllib.request.urlopen", side_effect=fail_open):
        outcome = _download_to("https://example.com/model", str(dest), timeout=10.0)
        assert outcome is DownloadOutcome.FAILED
        assert not dest.exists()
        assert list(tmp_path.glob("*.tmp")) == []
        assert list(tmp_path.glob("writeragent_tts_*")) == []

    with patch("urllib.request.urlopen", return_value=io.BytesIO(b"full-model-data")):
        outcome = _download_to("https://example.com/model", str(dest), timeout=10.0)
        assert outcome is DownloadOutcome.OK
        assert dest.exists()
        assert dest.read_bytes() == b"full-model-data"
        assert list(tmp_path.glob("*.tmp")) == []
        assert list(tmp_path.glob("writeragent_tts_*")) == []


def test_overlapping_downloads_waiter_shares_result(tmp_path):
    dest = tmp_path / "model.bin"
    owner_in_read = threading.Event()
    waiter_polled = threading.Event()
    allow_owner_finish = threading.Event()
    urlopen_calls = 0

    class BlockingStream(io.BytesIO):
        def read(self, n=-1):
            owner_in_read.set()
            assert allow_owner_finish.wait(timeout=5.0)
            return super().read(n)

    def fake_urlopen(req, timeout=30.0):
        nonlocal urlopen_calls
        urlopen_calls += 1
        return BlockingStream(b"model-content")

    class PollingCancellation(SendCancellation):
        def is_cancelled(self) -> bool:
            waiter_polled.set()
            return super().is_cancelled()

    waiter_cancel = PollingCancellation()

    outcome_owner = None
    outcome_waiter = None

    def run_owner():
        nonlocal outcome_owner
        outcome_owner = _download_to("https://example.com/model", str(dest))

    def run_waiter():
        nonlocal outcome_waiter
        outcome_waiter = _download_to("https://example.com/model", str(dest), cancel=waiter_cancel)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        t_owner = threading.Thread(target=run_owner)
        t_owner.start()
        assert owner_in_read.wait(timeout=5.0)

        t_waiter = threading.Thread(target=run_waiter)
        t_waiter.start()

        assert waiter_polled.wait(timeout=5.0)
        allow_owner_finish.set()

        t_owner.join(timeout=5.0)
        t_waiter.join(timeout=5.0)

    assert not t_owner.is_alive()
    assert not t_waiter.is_alive()
    assert outcome_owner is DownloadOutcome.OK
    assert outcome_waiter is DownloadOutcome.OK
    assert urlopen_calls == 1
    assert dest.read_bytes() == b"model-content"


def test_overlapping_downloads_cancel_waiter_only(tmp_path):
    dest = tmp_path / "model.bin"
    owner_in_read = threading.Event()
    waiter_cancel = SendCancellation()
    allow_owner_finish = threading.Event()
    urlopen_calls = 0

    class BlockingStream(io.BytesIO):
        def read(self, n=-1):
            owner_in_read.set()
            assert allow_owner_finish.wait(timeout=5.0)
            return super().read(n)

    def fake_urlopen(req, timeout=30.0):
        nonlocal urlopen_calls
        urlopen_calls += 1
        return BlockingStream(b"owner-content")

    outcome_owner = None
    outcome_waiter = None

    def run_owner():
        nonlocal outcome_owner
        outcome_owner = _download_to("https://example.com/model", str(dest))

    def run_waiter():
        nonlocal outcome_waiter
        outcome_waiter = _download_to("https://example.com/model", str(dest), cancel=waiter_cancel)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        t_owner = threading.Thread(target=run_owner)
        t_owner.start()
        assert owner_in_read.wait(timeout=5.0)

        t_waiter = threading.Thread(target=run_waiter)
        t_waiter.start()

        waiter_cancel.cancel()
        t_waiter.join(timeout=5.0)
        assert not t_waiter.is_alive()
        assert outcome_waiter is DownloadOutcome.CANCELLED

        allow_owner_finish.set()
        t_owner.join(timeout=5.0)
        assert not t_owner.is_alive()
        assert outcome_owner is DownloadOutcome.OK

    assert urlopen_calls == 1
    assert dest.read_bytes() == b"owner-content"


def test_overlapping_downloads_cancel_owner_waiter_becomes_owner(tmp_path):
    dest = tmp_path / "model.bin"
    owner_in_read = threading.Event()
    cancel_owner = SendCancellation()
    urlopen_calls = 0

    class StreamOwner:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, n=-1):
            owner_in_read.set()
            while not cancel_owner.is_cancelled():
                time.sleep(0.05)
            raise InterruptedError("owner cancelled")

    def fake_urlopen(req, timeout=30.0):
        nonlocal urlopen_calls
        urlopen_calls += 1
        if urlopen_calls == 1:
            return StreamOwner()
        return io.BytesIO(b"waiter-content")

    outcome_owner = None
    outcome_waiter = None

    def run_owner():
        nonlocal outcome_owner
        outcome_owner = _download_to(
            "https://example.com/model", str(dest), cancel=cancel_owner
        )

    def run_waiter():
        nonlocal outcome_waiter
        outcome_waiter = _download_to("https://example.com/model", str(dest))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        t_owner = threading.Thread(target=run_owner)
        t_owner.start()
        assert owner_in_read.wait(timeout=5.0)

        t_waiter = threading.Thread(target=run_waiter)
        t_waiter.start()

        cancel_owner.cancel()
        t_owner.join(timeout=5.0)
        assert not t_owner.is_alive()
        assert outcome_owner is DownloadOutcome.CANCELLED

        t_waiter.join(timeout=5.0)
        assert not t_waiter.is_alive()
        assert outcome_waiter is DownloadOutcome.OK

    assert urlopen_calls == 2
    assert dest.read_bytes() == b"waiter-content"
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob("writeragent_tts_*")) == []


def test_different_destinations_download_concurrently(tmp_path):
    dest1 = tmp_path / "model1.bin"
    dest2 = tmp_path / "model2.bin"
    urlopen_calls = 0

    def fake_urlopen(req, timeout=30.0):
        nonlocal urlopen_calls
        urlopen_calls += 1
        return io.BytesIO(b"data")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        res1 = _download_to("https://example.com/1", str(dest1))
        res2 = _download_to("https://example.com/2", str(dest2))

    assert res1 is DownloadOutcome.OK
    assert res2 is DownloadOutcome.OK
    assert urlopen_calls == 2


def test_download_failure_remembered_per_generation():
    """Bug 1: Failed model downloads are remembered per generation and not retried every sentence."""
    import plugin.audio.tts_service as tts

    utt1 = Utterance(SendCancellation())
    utt2 = Utterance(SendCancellation())
    tts._current = utt1
    try:
        assert not utt1.download_failed("kokoro")
        utt1.note_download_failed("kokoro")
        assert utt1.download_failed("kokoro")
        assert not utt2.download_failed("kokoro")
    finally:
        tts._current = None


def test_remember_download_failed_ignores_stale_generation():
    import plugin.audio.tts_service as tts

    utt = tts._begin_utterance()
    try:
        utt.note_download_failed("kokoro")
        assert utt.download_failed("kokoro")
        tts.stop_speech()
        utt.note_download_failed("piper")
        assert not utt.download_failed("piper")
    finally:
        tts.stop_speech()


def test_notify_tts_status_progress_prefers_progress_method():
    calls: list[str] = []

    class Sink:
        def progress(self, text: str) -> None:
            calls.append(f"progress:{text}")

        def __call__(self, text: str) -> None:
            calls.append(f"call:{text}")

        def clear(self) -> None:
            calls.append("clear")

    sink = Sink()
    notify_tts_status("Downloading…", sink, progress=True)
    assert calls == ["progress:Downloading…"]

    notify_tts_status("Speech request failed.", sink)
    assert calls == ["progress:Downloading…", "call:Speech request failed."]

    plain_calls: list[str] = []
    notify_tts_status("Downloading…", plain_calls.append, progress=True)
    assert plain_calls == ["Downloading…"]

    clear_tts_status(plain_calls.append)
    assert plain_calls == ["Downloading…"]

    clear_tts_status(sink)
    assert calls == ["progress:Downloading…", "call:Speech request failed.", "clear"]


def test_resolve_piper_model_file_ondemand_download(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.audio.tts_models.PIPER_ONNX_MIN_BYTES", 1)
    monkeypatch.setattr("plugin.audio.tts_models.PIPER_JSON_MIN_BYTES", 1)
    cache_dir = _piper_cache(tmp_path, monkeypatch)
    with patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: io.BytesIO(b"fake-piper-model-bytes")):
        resolved = resolve_piper_model_file("de_DE-thorsten-medium")
        assert resolved == str(cache_dir / "de_DE-thorsten-medium.onnx")
        assert os.path.exists(resolved)
        assert os.path.exists(str(cache_dir / "de_DE-thorsten-medium.onnx.json"))


def test_resolve_piper_model_file_reports_download_status(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.audio.tts_models.PIPER_ONNX_MIN_BYTES", 1)
    monkeypatch.setattr("plugin.audio.tts_models.PIPER_JSON_MIN_BYTES", 1)
    messages: list[str] = []
    cache_dir = _piper_cache(tmp_path, monkeypatch)
    with patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: io.BytesIO(b"fake-piper-model-bytes")):
        resolved = resolve_piper_model_file("de_DE-thorsten-medium", on_status=messages.append)

    assert resolved == str(cache_dir / "de_DE-thorsten-medium.onnx")
    assert os.path.exists(resolved)
    assert messages == ["Downloading Piper voice Thorsten…"]


def test_resolve_piper_model_file_reports_lessac_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.audio.tts_models.PIPER_ONNX_MIN_BYTES", 1)
    monkeypatch.setattr("plugin.audio.tts_models.PIPER_JSON_MIN_BYTES", 1)
    messages: list[str] = []
    cache_dir = _piper_cache(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True)
    (cache_dir / "en_US-lessac-medium.onnx").write_bytes(b"lessac")
    (cache_dir / "en_US-lessac-medium.onnx.json").write_bytes(b"{}")

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
        resolved = resolve_piper_model_file("de_DE-thorsten-medium", on_status=messages.append)

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
        resolved = resolve_piper_model_file("de_DE-thorsten-medium", on_status=messages.append)

    assert resolved is None
    assert len(messages) >= 2


def test_resolve_kokoro_model_files_reports_download_failure(tmp_path, monkeypatch):
    messages: list[str] = []
    _kokoro_cache(tmp_path, monkeypatch)

    with patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}), \
         patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
        model_path, voices_path = resolve_kokoro_model_files(on_status=messages.append)

    assert model_path.endswith("kokoro-v1.0.onnx")
    assert voices_path.endswith("voices-v1.0.bin")
    assert messages == [
        "Downloading Kokoro voice model…",
        "Couldn't download Kokoro; using OS speech",
    ]


def test_resolve_kokoro_model_files_downloads_multilingual_release(tmp_path, monkeypatch):
    cache_dir = _kokoro_cache(tmp_path, monkeypatch)
    downloaded: list[tuple[str, str]] = []

    def _fake_download_to(url: str, dest: str, **kwargs) -> DownloadOutcome:
        downloaded.append((url, dest))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(b"asset")
        return DownloadOutcome.OK

    with patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}, clear=False), \
         patch("plugin.audio.tts_models._download_to", side_effect=_fake_download_to):
        model_path, voices_path = resolve_kokoro_model_files()

    urls = [url for url, _dest in downloaded]
    assert urls == [
        f"{_KOKORO_RELEASE_BASE}/{KOKORO_VOICES_FILENAME}",
        f"{_KOKORO_RELEASE_BASE}/{KOKORO_MODEL_FILENAME}",
    ]
    assert model_path == str(cache_dir / "kokoro-v1.0.onnx")
    assert voices_path == str(cache_dir / "voices-v1.0.bin")


def test_resolve_kokoro_model_files_supersedes_english_only_cache(tmp_path, monkeypatch):
    cache_dir = _kokoro_cache(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True)
    (cache_dir / "kokoro-v0_19.onnx").write_bytes(b"old-model")
    (cache_dir / "voices.bin").write_bytes(b"old-voices")
    downloaded: list[str] = []

    def _fake_download_to(url: str, dest: str, **kwargs) -> DownloadOutcome:
        downloaded.append(url)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(b"new")
        return DownloadOutcome.OK

    with patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}, clear=False), \
         patch("plugin.audio.tts_models._download_to", side_effect=_fake_download_to):
        model_path, voices_path = resolve_kokoro_model_files()

    assert model_path == str(cache_dir / "kokoro-v1.0.onnx")
    assert voices_path == str(cache_dir / "voices-v1.0.bin")
    assert downloaded == [
        f"{_KOKORO_RELEASE_BASE}/{KOKORO_VOICES_FILENAME}",
        f"{_KOKORO_RELEASE_BASE}/{KOKORO_MODEL_FILENAME}",
    ]
    assert (cache_dir / "kokoro-v0_19.onnx").read_bytes() == b"old-model"
    assert (cache_dir / "voices.bin").read_bytes() == b"old-voices"


def test_resolve_kokoro_model_files_reuses_v1_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.audio.tts_models.KOKORO_ONNX_MIN_BYTES", 1)
    monkeypatch.setattr("plugin.audio.tts_models.KOKORO_VOICES_MIN_BYTES", 1)
    cache_dir = _kokoro_cache(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True)
    (cache_dir / "kokoro-v0_19.onnx").write_bytes(b"old-model")
    (cache_dir / "voices.bin").write_bytes(b"old-voices")
    (cache_dir / "kokoro-v1.0.onnx").write_bytes(b"model")
    (cache_dir / "voices-v1.0.bin").write_bytes(b"voices")

    with patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}, clear=False), \
         patch("plugin.audio.tts_models._download_to") as download_mock:
        model_path, voices_path = resolve_kokoro_model_files()

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
        model_path, voices_path = resolve_kokoro_model_files()

    download_mock.assert_not_called()
    assert model_path == str(model)
    assert voices_path == str(voices)


def test_resolve_kokoro_model_files_reuses_pipecat_without_download(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.audio.tts_models.KOKORO_ONNX_MIN_BYTES", 1)
    monkeypatch.setattr("plugin.audio.tts_models.KOKORO_VOICES_MIN_BYTES", 1)
    root = _isolate_home_cache(tmp_path, monkeypatch)
    pipecat = root / "pipecat" / "kokoro-onnx"
    pipecat.mkdir(parents=True)
    (pipecat / "kokoro-v1.0.onnx").write_bytes(b"model")
    (pipecat / "voices-v1.0.bin").write_bytes(b"voices")

    with patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}, clear=False), \
         patch("plugin.audio.tts_models._download_to") as download_mock:
        model_path, voices_path = resolve_kokoro_model_files()

    download_mock.assert_not_called()
    assert model_path == str(pipecat / "kokoro-v1.0.onnx")
    assert voices_path == str(pipecat / "voices-v1.0.bin")
    assert not (root / "kokoro").exists()


def test_resolve_kokoro_model_files_downloads_missing_sibling_beside_probe_hit(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.audio.tts_models.KOKORO_ONNX_MIN_BYTES", 1)
    monkeypatch.setattr("plugin.audio.tts_models.KOKORO_VOICES_MIN_BYTES", 1)
    root = _isolate_home_cache(tmp_path, monkeypatch)
    pipecat = root / "pipecat" / "kokoro-onnx"
    pipecat.mkdir(parents=True)
    (pipecat / "kokoro-v1.0.onnx").write_bytes(b"model")
    downloaded: list[str] = []

    def _fake_download_to(url: str, dest: str, **kwargs) -> DownloadOutcome:
        downloaded.append(dest)
        with open(dest, "wb") as handle:
            handle.write(b"voices")
        return DownloadOutcome.OK

    with patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}, clear=False), \
         patch("plugin.audio.tts_models._download_to", side_effect=_fake_download_to):
        model_path, voices_path = resolve_kokoro_model_files()

    assert model_path == str(pipecat / "kokoro-v1.0.onnx")
    assert voices_path == str(pipecat / "voices-v1.0.bin")
    assert downloaded == [str(pipecat / "voices-v1.0.bin")]
    assert not (root / "kokoro").exists()


def test_resolve_piper_model_file_reuses_pipecat_without_download(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.audio.tts_models.PIPER_ONNX_MIN_BYTES", 1)
    monkeypatch.setattr("plugin.audio.tts_models.PIPER_JSON_MIN_BYTES", 1)
    voice_dir = _isolate_home_cache(tmp_path, monkeypatch) / "pipecat" / "piper"
    voice_dir.mkdir(parents=True)
    onnx = voice_dir / "de_DE-thorsten-medium.onnx"
    onnx.write_bytes(b"onnx")
    (voice_dir / "de_DE-thorsten-medium.onnx.json").write_bytes(b"{}")

    with patch("urllib.request.urlopen") as urlopen:
        resolved = resolve_piper_model_file("de_DE-thorsten-medium")

    urlopen.assert_not_called()
    assert resolved == str(onnx)


def test_resolve_kokoro_model_files_cancelled_during_voices(tmp_path, monkeypatch):
    messages: list[str] = []
    _kokoro_cache(tmp_path, monkeypatch)
    utt = Utterance(SendCancellation())

    with patch("plugin.audio.tts_models._download_to", return_value=DownloadOutcome.CANCELLED):
        resolve_kokoro_model_files(on_status=messages.append, utt=utt)

    assert not utt.download_failed("kokoro")
    assert not any("using OS speech" in m for m in messages)


def test_resolve_kokoro_model_files_cancelled_during_model(tmp_path, monkeypatch):
    messages: list[str] = []
    cache_dir = _kokoro_cache(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True)
    (cache_dir / KOKORO_VOICES_FILENAME).write_bytes(b"x" * 20_000_000)
    utt = Utterance(SendCancellation())

    with patch("plugin.audio.tts_models._download_to", return_value=DownloadOutcome.CANCELLED):
        resolve_kokoro_model_files(on_status=messages.append, utt=utt)

    assert not utt.download_failed("kokoro")
    assert not any("using OS speech" in m for m in messages)


def test_resolve_piper_model_file_cancelled_during_download(tmp_path, monkeypatch):
    messages: list[str] = []
    _piper_cache(tmp_path, monkeypatch)
    utt = Utterance(SendCancellation())

    with patch("plugin.audio.tts_models._download_to", return_value=DownloadOutcome.CANCELLED):
        res = resolve_piper_model_file("de_DE-thorsten-medium", on_status=messages.append, utt=utt)

    assert res is None
    assert not utt.download_failed("de_DE-thorsten-medium")
    assert not any("using OS speech" in m for m in messages)
    assert not any("using Lessac" in m for m in messages)


def test_resolve_piper_model_file_cancelled_before_download(tmp_path, monkeypatch):
    messages: list[str] = []
    _piper_cache(tmp_path, monkeypatch)
    cancel = SendCancellation()
    cancel.cancel()
    utt = Utterance(cancel)

    res = resolve_piper_model_file("de_DE-thorsten-medium", on_status=messages.append, utt=utt)
    assert res is None
    assert not any("Downloading Piper" in m for m in messages)
    assert not utt.download_failed("de_DE-thorsten-medium")


def test_piper_cancelled_attempt_does_not_delete_other_files(tmp_path, monkeypatch):
    cache_dir = _piper_cache(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True)
    onnx_path = cache_dir / "de_DE-thorsten-medium.onnx"
    json_path = cache_dir / "de_DE-thorsten-medium.onnx.json"
    onnx_path.write_bytes(b"B_BYTES_ONNX")
    json_path.write_bytes(b"B_BYTES_JSON")
    cancel = SendCancellation()
    cancel.cancel()

    outcome = _download_piper_pair("de_DE-thorsten-medium", str(onnx_path), str(json_path), cancel=cancel)
    assert outcome is DownloadOutcome.CANCELLED
    assert onnx_path.read_bytes() == b"B_BYTES_ONNX"
    assert json_path.read_bytes() == b"B_BYTES_JSON"


def test_piper_one_file_failure_leaves_cache_intact(tmp_path, monkeypatch):
    cache_dir = _piper_cache(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True)
    onnx_path = cache_dir / "de_DE-thorsten-medium.onnx"
    json_path = cache_dir / "de_DE-thorsten-medium.onnx.json"
    onnx_path.write_bytes(b"EXISTING_ONNX")
    json_path.write_bytes(b"EXISTING_JSON")

    calls = 0

    def fake_download(url, dest, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            with open(dest, "wb") as f:
                f.write(b"NEW_ONNX")
            return DownloadOutcome.OK
        return DownloadOutcome.FAILED

    with patch("plugin.audio.tts_models._download_to", side_effect=fake_download):
        outcome = _download_piper_pair("de_DE-thorsten-medium", str(onnx_path), str(json_path))

    assert outcome is DownloadOutcome.FAILED
    assert onnx_path.read_bytes() == b"EXISTING_ONNX"
    assert json_path.read_bytes() == b"EXISTING_JSON"
    assert list(cache_dir.glob("writeragent_piper_*")) == []


def test_piper_unknown_voice_id_returns_failed(tmp_path):
    res = _download_piper_pair("nonexistent-voice-xyz", str(tmp_path / "x.onnx"), str(tmp_path / "x.json"))
    assert res is DownloadOutcome.FAILED


def test_download_to_short_body_fails(tmp_path):
    dest = tmp_path / "file.bin"
    resp = io.BytesIO(b"short")
    setattr(resp, "headers", {"Content-Length": "100"})
    with patch("urllib.request.urlopen", return_value=resp):
        outcome = _download_to("https://example.com/f", str(dest))
    assert outcome is DownloadOutcome.FAILED
    assert not dest.exists()


def test_download_to_html_fails(tmp_path):
    dest = tmp_path / "file.bin"
    resp = io.BytesIO(b"<html>error</html>")
    setattr(resp, "headers", {"Content-Type": "text/html; charset=utf-8"})
    with patch("urllib.request.urlopen", return_value=resp):
        outcome = _download_to("https://example.com/f", str(dest))
    assert outcome is DownloadOutcome.FAILED
    assert not dest.exists()


def test_zero_byte_canonical_kokoro_triggers_download(tmp_path, monkeypatch):
    cache_dir = _kokoro_cache(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True)
    (cache_dir / KOKORO_MODEL_FILENAME).write_bytes(b"")
    (cache_dir / KOKORO_VOICES_FILENAME).write_bytes(b"v" * 20_000_000)

    download_called = False

    def fake_download(url, dest, **kwargs):
        nonlocal download_called
        download_called = True
        with open(dest, "wb") as f:
            f.write(b"m" * 250_000_000)
        return DownloadOutcome.OK

    with patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}, clear=False), \
         patch("plugin.audio.tts_models._download_to", side_effect=fake_download):
        resolve_kokoro_model_files()

    assert download_called


def test_kokoro_env_override_1_byte_returned_as_is(tmp_path, monkeypatch):
    cache_dir = _kokoro_cache(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True)
    (cache_dir / KOKORO_VOICES_FILENAME).write_bytes(b"v" * 20_000_000)
    custom_model = tmp_path / "custom.onnx"
    custom_model.write_bytes(b"x")

    with patch.dict("os.environ", {"KOKORO_MODEL_PATH": str(custom_model), "KOKORO_VOICES_PATH": ""}, clear=False), \
         patch("urllib.request.urlopen") as mock_url:
        m_path, v_path = resolve_kokoro_model_files()

    mock_url.assert_not_called()
    assert m_path == str(custom_model)


def test_piper_floor_rejects_10_byte_body(tmp_path):
    dest = tmp_path / "voice.onnx"
    resp = io.BytesIO(b"0123456789")
    with patch("urllib.request.urlopen", return_value=resp):
        outcome = _download_to("https://example.com/v", str(dest), min_bytes=100_000)
    assert outcome is DownloadOutcome.FAILED
    assert not dest.exists()


def test_download_to_socket_timeout_retries_and_succeeds(tmp_path):
    dest = tmp_path / "file.bin"
    timeouts = 0

    class RetryingStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, chunk_size):
            nonlocal timeouts
            if timeouts < 2:
                timeouts += 1
                raise socket.timeout("timed out")
            if timeouts == 2:
                timeouts += 1
                return b"full-model-data"
            return b""

    with patch("urllib.request.urlopen", return_value=RetryingStream()):
        outcome = _download_to("https://example.com/f", str(dest))
    assert outcome is DownloadOutcome.OK
    assert timeouts == 3
    assert dest.read_bytes() == b"full-model-data"


def test_download_to_503_then_200_succeeds(tmp_path):
    dest = tmp_path / "file.bin"
    calls = 0

    def fake_urlopen(req, timeout=30.0):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(req.full_url if hasattr(req, "full_url") else str(req), 503, "Service Unavailable", email.message.Message(), None)
        return io.BytesIO(b"data-after-retry")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        outcome = _download_to("https://example.com/f", str(dest))
    assert outcome is DownloadOutcome.OK
    assert calls == 2
    assert dest.read_bytes() == b"data-after-retry"


def test_content_type_failure_is_one_attempt(tmp_path):
    dest = tmp_path / "file.bin"
    calls = 0

    def fake_urlopen(req, timeout=30.0):
        nonlocal calls
        calls += 1
        resp = io.BytesIO(b"html error")
        setattr(resp, "headers", {"Content-Type": "text/html"})
        return resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        outcome = _download_to("https://example.com/f", str(dest))
    assert outcome is DownloadOutcome.FAILED
    assert calls == 1


def test_cancelled_during_backoff_returns_cancelled(tmp_path):
    dest = tmp_path / "file.bin"
    calls = 0
    cancel = SendCancellation()

    def fake_urlopen(req, timeout=30.0):
        nonlocal calls
        calls += 1
        cancel.cancel()
        raise urllib.error.HTTPError(req.full_url if hasattr(req, "full_url") else str(req), 503, "Unavailable", email.message.Message(), None)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        outcome = _download_to("https://example.com/f", str(dest), cancel=cancel)
    assert outcome is DownloadOutcome.CANCELLED
    assert calls == 1


def test_disk_space_below_floor_emits_status_and_remembers_failure(tmp_path, monkeypatch):
    import plugin.audio.tts_service as tts

    _kokoro_cache(tmp_path, monkeypatch)
    messages: list[str] = []

    mock_usage = MagicMock(free=100)

    utt = tts._begin_utterance()
    try:
        with patch("shutil.disk_usage", return_value=mock_usage), \
             patch("urllib.request.urlopen") as mock_url:
            resolve_kokoro_model_files(on_status=messages.append, utt=utt)

        mock_url.assert_not_called()
        assert utt.download_failed("kokoro")
        assert any("Not enough disk space" in m for m in messages)
    finally:
        tts.stop_speech()


def test_percent_callbacks_throttled_and_stops_on_cancel(tmp_path):
    dest = tmp_path / "file.bin"
    status_lines: list[str] = []

    chunks = [b"A" * 1000 for _ in range(10)]
    total = 10000

    class Stream:
        def __init__(self):
            self.idx = 0
            self.headers = {"Content-Length": str(total)}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, n=-1):
            if self.idx < len(chunks):
                c = chunks[self.idx]
                self.idx += 1
                return c
            return b""

    with patch("urllib.request.urlopen", return_value=Stream()):
        outcome = _download_to("https://example.com/f", str(dest), on_status=status_lines.append)

    assert outcome is DownloadOutcome.OK
    assert len(status_lines) <= 2
    assert any("%" in s for s in status_lines)
