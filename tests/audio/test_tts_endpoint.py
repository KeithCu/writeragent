# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for TTS HTTP endpoint synthesis and audio format handling."""

import json
import struct
from unittest.mock import MagicMock, patch

from plugin.audio.tts_endpoint import (
    _default_endpoint_format,
    _download_endpoint_speech,
    _pcm_rate_from_content_type,
    _pcm_s16le_to_wav,
    _speak_endpoint,
)
from plugin.audio.tts_service import _release_temp
from plugin.framework.client import model_fetcher as cfg


def _speech_meta(body: bytes, content_type: str, status: int = 200):
    from plugin.framework.client.requests import HttpResult

    return HttpResult(status=status, body=body, content_type=content_type)


def _speech_http_error(code: int, body: str):
    from plugin.framework.errors import NetworkError

    return NetworkError(body, code="HTTP_ERROR", details={"status": code, "url": "https://openrouter.ai/api/v1/audio/speech"})


@patch("plugin.audio.tts_endpoint._default_endpoint_format", new=lambda: "mp3")
def test_speak_endpoint_payload():
    cfg._model_fetch_tts_cache.clear()
    cfg._tts_response_format.clear()

    with patch("plugin.framework.client.requests.sync_request", return_value=_speech_meta(b"fake-audio-bytes", "audio/mpeg")) as mock_sync:
        with patch("plugin.audio.tts_service._play_audio_file") as mock_play:
            _speak_endpoint("Test hello", "https://openrouter.ai/api", "test-key", model="hexgrad/Kokoro-82M", voice="nova")
            mock_play.assert_called_once()

        assert mock_sync.call_args.args[0] == "https://openrouter.ai/api/v1/audio/speech"
        headers = mock_sync.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer test-key"
        payload = json.loads(mock_sync.call_args.kwargs["data"].decode("utf-8"))
        assert payload["model"] == "hexgrad/Kokoro-82M"
        assert payload["voice"] == "af_sky"
        assert payload["response_format"] == "mp3"


def test_speak_endpoint_prefers_cached_openrouter_speech_id():
    cfg._model_fetch_tts_cache.clear()
    cfg._tts_response_format.clear()
    cfg._model_fetch_tts_cache["speech-test"] = ["hexgrad/kokoro-82m", "microsoft/mai-voice-2"]
    try:
        with patch("plugin.framework.client.requests.sync_request", return_value=_speech_meta(b"fake-audio-bytes", "audio/mpeg")) as mock_sync:
            with patch("plugin.audio.tts_service._play_audio_file"):
                _speak_endpoint(
                    "Test hello",
                    "https://openrouter.ai/api",
                    "test-key",
                    model="hexgrad/Kokoro-82M",
                    voice="af_bella",
                )
            payload = json.loads(mock_sync.call_args.kwargs["data"].decode("utf-8"))
            assert payload["model"] == "hexgrad/kokoro-82m"
            assert payload["voice"] == "af_bella"
    finally:
        cfg._model_fetch_tts_cache.clear()
        cfg._tts_response_format.clear()


@patch("plugin.audio.tts_endpoint._default_endpoint_format", new=lambda: "mp3")
def test_endpoint_pcm_failure_is_remembered_and_wrapped():
    model = "google/gemini-2.5-flash-preview-tts"
    cfg._tts_response_format.clear()
    cfg._model_fetch_tts_cache.clear()
    pcm = b"\x01\x00\x02\x00"
    body = '{"error":{"message":"response_format must be pcm"}}'
    calls: list[dict] = []
    statuses: list[str] = []
    reject_mp3 = True

    def fake_sync(url, data=None, headers=None, parse_json=True, method=None, **kwargs):
        del url, headers, parse_json, method, kwargs
        assert data is not None
        payload = json.loads(data.decode("utf-8"))
        calls.append(payload)
        if reject_mp3 and payload["response_format"] == "mp3":
            raise _speech_http_error(400, body)
        return _speech_meta(pcm, "audio/pcm;rate=16000")

    path = None
    path2 = None
    try:
        with patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync):
            path = _download_endpoint_speech(
                "Hello",
                "https://openrouter.ai/api",
                "test-key",
                model=model,
                voice="Kore",
                on_status=statuses.append,
            )
            assert path is not None
            assert path.endswith(".wav")
            with open(path, "rb") as handle:
                wav = handle.read()
        assert not statuses
        assert [item["response_format"] for item in calls] == ["mp3", "pcm"]
        assert cfg.cached_tts_response_format(model) == "pcm"
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"
        assert struct.unpack_from("<H", wav, 22)[0] == 1
        assert struct.unpack_from("<I", wav, 24)[0] == 16000
        assert struct.unpack_from("<H", wav, 34)[0] == 16
        assert wav[44:] == pcm

        reject_mp3 = False
        calls.clear()
        with patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync):
            path2 = _download_endpoint_speech(
                "Again",
                "https://openrouter.ai/api",
                "test-key",
                model=model,
                voice="Kore",
            )
        assert path2 is not None
        assert [item["response_format"] for item in calls] == ["pcm"]
    finally:
        _release_temp(path)
        _release_temp(path2)
        cfg._tts_response_format.clear()


def test_pcm_wrap_defaults_to_24khz():
    assert _pcm_rate_from_content_type("audio/pcm") == 24000
    assert _pcm_rate_from_content_type("audio/L16;rate=22050") == 22050
    wav = _pcm_s16le_to_wav(b"\x00\x00", 24000)
    assert wav[:4] == b"RIFF"
    assert struct.unpack_from("<I", wav, 24)[0] == 24000
    assert struct.unpack_from("<H", wav, 22)[0] == 1
    assert struct.unpack_from("<H", wav, 34)[0] == 16


@patch("plugin.audio.tts_endpoint._default_endpoint_format", new=lambda: "mp3")
def test_endpoint_wav_error_retries_wav():
    model = "vendor/wav-only"
    cfg._tts_response_format.clear()
    calls: list[dict] = []
    wav_bytes = b"RIFF" + b"\x00" * 40

    def fake_sync(url, data=None, headers=None, parse_json=True, method=None, **kwargs):
        del url, headers, parse_json, method, kwargs
        assert data is not None
        payload = json.loads(data.decode("utf-8"))
        calls.append(payload)
        if payload["response_format"] == "mp3":
            raise _speech_http_error(400, "unsupported response_format mp3; use wav")
        return _speech_meta(wav_bytes, "audio/wav")

    path = None
    try:
        with patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync):
            path = _download_endpoint_speech(
                "Hello",
                "https://openrouter.ai/api",
                "k",
                model=model,
                voice="cedar",
            )
        assert path is not None
        assert path.endswith(".wav")
        assert [item["response_format"] for item in calls] == ["mp3", "wav"]
        assert cfg.cached_tts_response_format(model) == "wav"
        with open(path, "rb") as handle:
            assert handle.read().startswith(b"RIFF")
    finally:
        _release_temp(path)
        cfg._tts_response_format.clear()


@patch("plugin.audio.tts_endpoint._default_endpoint_format", new=lambda: "mp3")
def test_endpoint_mp3_success_does_not_cache_format():
    model = "x-ai/grok-voice-tts-1.0"
    cfg._tts_response_format.clear()
    calls: list[dict] = []

    def fake_sync(url, data=None, headers=None, parse_json=True, method=None, **kwargs):
        del url, headers, parse_json, method, kwargs
        assert data is not None
        calls.append(json.loads(data.decode("utf-8")))
        return _speech_meta(b"ID3fake-mp3", "audio/mpeg")

    path = None
    try:
        with patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync):
            path = _download_endpoint_speech(
                "Hello",
                "https://openrouter.ai/api",
                "test-key",
                model=model,
                voice="ara",
            )
        assert path is not None
        assert path.endswith(".mp3")
        assert calls[0]["response_format"] == "mp3"
        assert cfg.cached_tts_response_format(model) is None
    finally:
        _release_temp(path)
        cfg._tts_response_format.clear()


def test_endpoint_http_error_is_reported_to_status():
    cfg._tts_response_format.clear()
    statuses: list[str] = []

    def fake_sync(url, data=None, headers=None, parse_json=True, method=None, **kwargs):
        del url, data, headers, parse_json, method, kwargs
        raise _speech_http_error(401, '{"error":{"message":"invalid api key"}}')

    try:
        with patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync):
            path = _download_endpoint_speech(
                "Hello",
                "https://api.openai.com",
                "test-key",
                model="tts-1",
                voice="alloy",
                on_status=statuses.append,
            )
        assert path is None
        assert len(statuses) == 1
        assert "401" in statuses[0]
        assert "invalid api key" in statuses[0]
        assert cfg.cached_tts_response_format("tts-1") is None
    finally:
        cfg._tts_response_format.clear()


def test_speech_http_error_redacts_api_key(caplog):
    import logging
    from plugin.framework.client.request_controls import reset_host_pacing_for_tests

    secret = "sk-speech-unique-secret"
    cfg._tts_response_format.clear()
    reset_host_pacing_for_tests()
    response = MagicMock()
    response.status = 401
    response.reason = "Unauthorized"
    response.read.return_value = f'{{"error":{{"message":"bad {secret}"}}}}'.encode()
    response.getheader.return_value = None
    conn = MagicMock()
    conn.getresponse.return_value = response
    statuses: list[str] = []
    try:
        with caplog.at_level(logging.DEBUG), patch("http.client.HTTPSConnection", return_value=conn):
            path = _download_endpoint_speech(
                "Hello",
                "https://api.openai.com",
                secret,
                model="tts-1",
                voice="alloy",
                on_status=statuses.append,
            )
        assert path is None
        assert statuses
        assert secret not in statuses[0]
        assert secret not in caplog.text
        assert "<redacted>" in statuses[0]
    finally:
        cfg._tts_response_format.clear()


def test_default_endpoint_format_is_wav_on_windows_only():
    with patch("sys.platform", "win32"):
        assert _default_endpoint_format() == "wav"
    with patch("sys.platform", "linux"):
        assert _default_endpoint_format() == "mp3"


def test_endpoint_audio_suffix_from_content_type():
    """Bug 6: Trust content_type and magic bytes, do not wrap MP3 in WAV if pcm was requested."""
    model = "custom-tts"
    cfg.remember_tts_response_format(model, "pcm")

    mp3_data = b"ID3\x03\x00\x00\x00\x00\x00#TSSE\x00\x00\x00fake-mp3-bytes"
    with patch("plugin.framework.client.requests.sync_request", return_value=_speech_meta(mp3_data, "audio/mpeg")):
        path = _download_endpoint_speech(
            "Hello", "https://api.test", "key", model=model, voice="v"
        )
        try:
            assert path is not None
            assert path.endswith(".mp3")
            with open(path, "rb") as f:
                content = f.read()
            assert not content.startswith(b"RIFF")
            assert content.startswith(b"ID3")
        finally:
            _release_temp(path)

    wav_data = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    with patch("plugin.framework.client.requests.sync_request", return_value=_speech_meta(wav_data, "audio/wav")):
        path = _download_endpoint_speech(
            "Hello", "https://api.test", "key", model="wav-model", voice="v"
        )
        try:
            assert path is not None
            assert path.endswith(".wav")
            with open(path, "rb") as f:
                content = f.read()
            assert content.startswith(b"RIFF")
        finally:
            _release_temp(path)
