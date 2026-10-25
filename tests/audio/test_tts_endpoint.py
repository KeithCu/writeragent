# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for TTS HTTP endpoint synthesis and audio format handling."""

from tests.harness.strip_bundle import is_release_build
import json
import logging
import struct
import threading
from unittest.mock import MagicMock, patch

from plugin.audio.tts_endpoint import (
    _default_endpoint_format,
    _download_endpoint_speech,
    _EndpointClip,
    _pcm_rate_from_content_type,
    _pcm_s16le_to_wav,
    _post_audio_speech,
    _resolve_tts_voice,
    _sniff_audio_format,
    _speech_headers,
    _speech_url,
    _tts_format_key,
)
from plugin.audio.tts_service import _speak_endpoint
from plugin.framework.client import model_fetcher as cfg


def _speech_meta(body: bytes, content_type: str, status: int = 200):
    from plugin.framework.client.requests import HttpResult

    return HttpResult(status=status, body=body, content_type=content_type)


def _speech_http_error(code: int, body: str):
    from plugin.framework.errors import NetworkError

    return NetworkError(body, code="HTTP_ERROR", details={"status": code, "url": "https://openrouter.ai/api/v1/audio/speech"})


def test_resolve_tts_voice():
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "nova") == "af_sky"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "alloy") == "af_sky"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "shimmer") == "af_sarah"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "echo") == "am_adam"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "af_bella") == "af_bella"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "am_adam") == "am_adam"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "if_sara") == "if_sara"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "pf_dora") == "pf_dora"
    assert _resolve_tts_voice("openai/tts-1", "nova") == "nova"
    assert _resolve_tts_voice("openai/tts-1", "alloy") == "alloy"


def test_speech_url():
    cases = [
        ("https://openrouter.ai", "https://openrouter.ai/v1/audio/speech"),
        ("https://example.com/v1", "https://example.com/v1/audio/speech"),
        ("https://example.com/v1/", "https://example.com/v1/audio/speech"),
        ("https://openrouter.ai/api", "https://openrouter.ai/api/v1/audio/speech"),
        ("https://example.com/v1?x=1", "https://example.com/v1/audio/speech?x=1"),
        ("https://example.com/v1#frag", "https://example.com/v1/audio/speech"),
        ("https://user:pass@example.com/v1", "https://user:pass@example.com/v1/audio/speech"),
        ("https://example.com/custom/audio/speech", "https://example.com/custom/audio/speech"),
    ]
    for raw, expected in cases:
        assert _speech_url(raw) == expected


def test_speech_headers():
    from plugin.framework.client.auth import AuthError

    headers_or = _speech_headers("https://openrouter.ai/api", "test-key")
    assert headers_or["Authorization"] == "Bearer test-key"
    assert "HTTP-Referer" in headers_or
    assert "X-Title" in headers_or
    assert "User-Agent" not in headers_or

    with patch("plugin.framework.client.auth.resolve_auth_for_config", side_effect=AuthError("auth failure")):
        headers_fallback = _speech_headers("https://api.openai.com/v1", "key123")
        assert headers_fallback["Authorization"] == "Bearer key123"
        assert headers_fallback["Content-Type"] == "application/json"


def test_provider_detection_openrouter_isolation():
    cfg._model_fetch_tts_cache.clear()
    cfg._tts_response_format.clear()
    cfg._model_fetch_tts_cache["speech-test"] = ["hexgrad/kokoro-82m", "microsoft/mai-voice-2"]

    calls: list[str] = []

    def fake_sync(url, data=None, **kwargs):
        assert isinstance(data, bytes)
        payload = json.loads(data.decode("utf-8"))
        calls.append(payload["model"])
        return _speech_meta(b"ID3fake", "audio/mpeg")

    try:
        with patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync):
            _download_endpoint_speech("Hi", "https://notopenrouter.ai/api", "k", model="hexgrad/Kokoro-82M", voice="v")
            assert calls[-1] == "hexgrad/Kokoro-82M"

            _download_endpoint_speech("Hi", "https://evil.example/openrouter.ai", "k", model="hexgrad/Kokoro-82M", voice="v")
            assert calls[-1] == "hexgrad/Kokoro-82M"

            _download_endpoint_speech("Hi", "https://openrouter.ai/api", "k", model="hexgrad/Kokoro-82M", voice="v")
            assert calls[-1] == "hexgrad/kokoro-82m"
    finally:
        cfg._model_fetch_tts_cache.clear()
        cfg._tts_response_format.clear()


def test_caplog_redacts_credentials(caplog):
    caplog.set_level(logging.INFO)
    secret = "s3cret"
    url = f"https://user:{secret}@example.test/v1?key={secret}"

    with patch("plugin.framework.client.requests.sync_request", return_value=_speech_meta(b"ID3fake", "audio/mpeg")):
        _download_endpoint_speech("Hello", url, "k", model="m", voice="v")

    for record in caplog.records:
        assert secret not in record.getMessage()


@patch("plugin.audio.tts_endpoint._default_endpoint_format", new=lambda: "mp3")
def test_speak_endpoint_payload():
    cfg._model_fetch_tts_cache.clear()
    cfg._tts_response_format.clear()

    with patch("plugin.framework.client.requests.sync_request", return_value=_speech_meta(b"ID3fake-audio-bytes", "audio/mpeg")) as mock_sync:
        with patch("plugin.audio.tts_service._play_audio_file") as mock_play:
            _speak_endpoint("Test hello", "https://openrouter.ai/api", "test-key", model="hexgrad/Kokoro-82M", voice="nova")
            mock_play.assert_called_once()

        assert mock_sync.call_args.args[0] == "https://openrouter.ai/api/v1/audio/speech"
        assert mock_sync.call_args.kwargs["max_bytes"] == 32 * 1024 * 1024
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
        with patch("plugin.framework.client.requests.sync_request", return_value=_speech_meta(b"ID3fake-audio-bytes", "audio/mpeg")) as mock_sync:
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

    try:
        with patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync):
            clip = _download_endpoint_speech(
                "Hello",
                "https://openrouter.ai/api",
                "test-key",
                model=model,
                voice="Kore",
                on_status=statuses.append,
            )
            assert clip is not None
            assert clip.suffix == ".wav"
            assert clip.request_format == "pcm"
            wav = clip.audio
        assert not statuses
        assert [item["response_format"] for item in calls] == ["mp3", "pcm"]
        fmt_key = _tts_format_key("https://openrouter.ai/api", model)
        assert cfg.cached_tts_response_format(fmt_key) == "pcm"
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"
        assert struct.unpack_from("<H", wav, 22)[0] == 1
        assert struct.unpack_from("<I", wav, 24)[0] == 16000
        assert struct.unpack_from("<H", wav, 34)[0] == 16
        assert wav[44:] == pcm

        reject_mp3 = False
        calls.clear()
        with patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync):
            clip2 = _download_endpoint_speech(
                "Again",
                "https://openrouter.ai/api",
                "test-key",
                model=model,
                voice="Kore",
            )
        assert clip2 is not None
        assert [item["response_format"] for item in calls] == ["pcm"]
    finally:
        cfg._tts_response_format.clear()


def test_pcm_wrap_defaults_to_24khz():
    assert _pcm_rate_from_content_type("audio/pcm") == 24000
    assert _pcm_rate_from_content_type("audio/L16;rate=22050") == 22050
    assert _pcm_rate_from_content_type("audio/L16;rate=7999") == 24000
    assert _pcm_rate_from_content_type("audio/L16;rate=96001") == 24000
    assert _pcm_rate_from_content_type("audio/L16;rate=8000") == 8000
    assert _pcm_rate_from_content_type("audio/L16;rate=96000") == 96000
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

    try:
        with patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync):
            clip = _download_endpoint_speech(
                "Hello",
                "https://openrouter.ai/api",
                "k",
                model=model,
                voice="cedar",
            )
        assert clip is not None
        assert clip.suffix == ".wav"
        assert [item["response_format"] for item in calls] == ["mp3", "wav"]
        fmt_key = _tts_format_key("https://openrouter.ai/api", model)
        assert cfg.cached_tts_response_format(fmt_key) == "wav"
        assert clip.audio.startswith(b"RIFF")
    finally:
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

    try:
        with patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync):
            clip = _download_endpoint_speech(
                "Hello",
                "https://openrouter.ai/api",
                "test-key",
                model=model,
                voice="ara",
            )
        assert clip is not None
        assert clip.suffix == ".mp3"
        assert calls[0]["response_format"] == "mp3"
        fmt_key = _tts_format_key("https://openrouter.ai/api", model)
        assert cfg.cached_tts_response_format(fmt_key) == "mp3"
    finally:
        cfg._tts_response_format.clear()


def test_two_origins_one_model_learned_format():
    model = "vendor/shared-model"
    cfg._tts_response_format.clear()
    calls_a: list[dict] = []
    calls_b: list[dict] = []

    def sync_a(url, data=None, **kwargs):
        assert isinstance(data, bytes)
        calls_a.append(json.loads(data.decode("utf-8")))
        return _speech_meta(b"RIFF" + b"\x00" * 40, "audio/wav")

    def sync_b(url, data=None, **kwargs):
        assert isinstance(data, bytes)
        calls_b.append(json.loads(data.decode("utf-8")))
        return _speech_meta(b"ID3fake", "audio/mpeg")

    try:
        with patch("plugin.audio.tts_endpoint._default_endpoint_format", return_value="mp3"), \
             patch("plugin.framework.client.requests.sync_request", side_effect=sync_a):
            _download_endpoint_speech("Hi", "https://host-a.com/v1", "k", model=model, voice="v")
        key_a = _tts_format_key("https://host-a.com/v1", model)
        assert cfg.cached_tts_response_format(key_a) == "wav"

        with patch("plugin.audio.tts_endpoint._default_endpoint_format", return_value="mp3"), \
             patch("plugin.framework.client.requests.sync_request", side_effect=sync_b):
            _download_endpoint_speech("Hi", "https://host-b.com/v1", "k", model=model, voice="v")
        assert calls_b[0]["response_format"] == "mp3"
    finally:
        cfg._tts_response_format.clear()


def test_retry_status_gates():
    cfg._tts_response_format.clear()
    # 401 with wav body is one POST and remembers nothing
    with patch("plugin.framework.client.requests.sync_request", side_effect=_speech_http_error(401, "error with wav")):
        clip = _download_endpoint_speech("Hi", "https://host.com/v1", "k", model="m", voice="v")
        assert clip is None
        assert cfg.cached_tts_response_format(_tts_format_key("https://host.com/v1", "m")) is None

    # 400 with unsupported response_format with no format word retries to table alternate (mp3 -> wav)
    calls_400: list[dict] = []

    def fake_400(url, data=None, **kwargs):
        assert isinstance(data, bytes)
        calls_400.append(json.loads(data.decode("utf-8")))
        if len(calls_400) == 1:
            raise _speech_http_error(400, "unsupported response_format")
        return _speech_meta(b"RIFF" + b"\x00" * 40, "audio/wav")

    with patch("plugin.audio.tts_endpoint._default_endpoint_format", return_value="mp3"), \
         patch("plugin.framework.client.requests.sync_request", side_effect=fake_400):
        clip = _download_endpoint_speech("Hi", "https://host.com/v1", "k", model="m", voice="v")
        assert clip is not None
        assert [c["response_format"] for c in calls_400] == ["mp3", "wav"]

    # 400 with "use wav" while mp3 requested retries wav
    calls_hint: list[dict] = []

    def fake_hint(url, data=None, **kwargs):
        assert isinstance(data, bytes)
        calls_hint.append(json.loads(data.decode("utf-8")))
        if len(calls_hint) == 1:
            raise _speech_http_error(400, "bad format; use wav")
        return _speech_meta(b"RIFF" + b"\x00" * 40, "audio/wav")

    with patch("plugin.audio.tts_endpoint._default_endpoint_format", return_value="mp3"), \
         patch("plugin.framework.client.requests.sync_request", side_effect=fake_hint):
        clip = _download_endpoint_speech("Hi", "https://host.com/v1", "k", model="m2", voice="v")
        assert clip is not None
        assert [c["response_format"] for c in calls_hint] == ["mp3", "wav"]


def test_sniff_and_pcm_treatments():
    # ID3 with Content-Type: audio/wav -> suffix .mp3, bytes not wrapped
    id3_data = b"ID3\x03\x00\x00\x00\x00\x00#TSSE"
    with patch("plugin.framework.client.requests.sync_request", return_value=_speech_meta(id3_data, "audio/wav")):
        clip = _download_endpoint_speech("H", "https://h.test", "k", model="m", voice="v")
        assert clip is not None
        assert clip.suffix == ".mp3"
        assert clip.audio == id3_data

    # Raw PCM with audio/L16;rate=22050 and requested mp3 -> starts with RIFF, rate 22050, remembered pcm
    pcm_data = b"\x01\x00\x02\x00"
    with patch("plugin.audio.tts_endpoint._default_endpoint_format", return_value="mp3"), \
         patch("plugin.framework.client.requests.sync_request", return_value=_speech_meta(pcm_data, "audio/L16;rate=22050")):
        clip = _download_endpoint_speech("H", "https://h.test", "k", model="m_pcm", voice="v")
        assert clip is not None
        assert clip.suffix == ".wav"
        assert clip.audio.startswith(b"RIFF")
        assert struct.unpack_from("<I", clip.audio, 24)[0] == 22050
        assert cfg.cached_tts_response_format(_tts_format_key("https://h.test", "m_pcm")) == "pcm"

    # audio/x-pcm and codec=pcm take same wrap
    with patch("plugin.framework.client.requests.sync_request", return_value=_speech_meta(pcm_data, "audio/x-pcm")):
        clip_x = _download_endpoint_speech("H", "https://h.test", "k", model="m_x", voice="v")
        assert clip_x is not None
        assert clip_x.audio.startswith(b"RIFF")

    with patch("plugin.framework.client.requests.sync_request", return_value=_speech_meta(pcm_data, "audio/wave;codec=pcm")):
        clip_codec = _download_endpoint_speech("H", "https://h.test", "k", model="m_codec", voice="v")
        assert clip_codec is not None
        assert clip_codec.audio.startswith(b"RIFF")

    # _sniff_audio_format table
    assert _sniff_audio_format(b"RIFF\x00\x00\x00\x00WAVE") == "wav"
    assert _sniff_audio_format(b"ID3\x03\x00") == "mp3"
    assert _sniff_audio_format(b"\xff\xfb\x90\x44") == "mp3"
    assert _sniff_audio_format(b"") is None
    assert _sniff_audio_format(b"short") is None


def test_endpoint_http_error_is_reported_to_status():
    cfg._tts_response_format.clear()
    statuses: list[str] = []

    def fake_sync(url, data=None, headers=None, parse_json=True, method=None, **kwargs):
        del url, data, headers, parse_json, method, kwargs
        raise _speech_http_error(401, '{"error":{"message":"invalid api key"}}')

    try:
        with patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync):
            clip = _download_endpoint_speech(
                "Hello",
                "https://api.openai.com",
                "test-key",
                model="tts-1",
                voice="alloy",
                on_status=statuses.append,
            )
        assert clip is None
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
            clip = _download_endpoint_speech(
                "Hello",
                "https://api.openai.com",
                secret,
                model="tts-1",
                voice="alloy",
                on_status=statuses.append,
            )
        assert clip is None
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
    cfg.remember_tts_response_format(_tts_format_key("https://api.test", model), "pcm")

    mp3_data = b"ID3\x03\x00\x00\x00\x00\x00#TSSE\x00\x00\x00fake-mp3-bytes"
    with patch("plugin.framework.client.requests.sync_request", return_value=_speech_meta(mp3_data, "audio/mpeg")):
        clip = _download_endpoint_speech(
            "Hello", "https://api.test", "key", model=model, voice="v"
        )
        assert clip is not None
        assert clip.suffix == ".mp3"
        assert not clip.audio.startswith(b"RIFF")
        assert clip.audio.startswith(b"ID3")

    wav_data = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    with patch("plugin.framework.client.requests.sync_request", return_value=_speech_meta(wav_data, "audio/wav")):
        clip2 = _download_endpoint_speech(
            "Hello", "https://api.test", "key", model="wav-model", voice="v"
        )
        assert clip2 is not None
        assert clip2.suffix == ".wav"
        assert clip2.audio.startswith(b"RIFF")


@patch("plugin.audio.tts_endpoint._default_endpoint_format", new=lambda: "mp3")
def test_endpoint_retry_remembers_format_without_content_type():
    """A successful retry with a generic Content-Type must stick for the next clip."""
    model = "vendor/wav-generic"
    cfg._tts_response_format.clear()
    calls: list[dict] = []
    wav_bytes = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 16

    def fake_sync(url, data=None, headers=None, parse_json=True, method=None, **kwargs):
        del url, headers, parse_json, method, kwargs
        assert data is not None
        payload = json.loads(data.decode("utf-8"))
        calls.append(payload)
        if payload["response_format"] == "mp3":
            raise _speech_http_error(400, "unsupported response_format mp3; use wav")
        return _speech_meta(wav_bytes, "application/octet-stream")

    try:
        with patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync):
            clip = _download_endpoint_speech(
                "Hello", "https://api.example", "k", model=model, voice="cedar",
            )
        assert clip is not None
        assert [item["response_format"] for item in calls] == ["mp3", "wav"]
        fmt_key = _tts_format_key("https://api.example", model)
        assert cfg.cached_tts_response_format(fmt_key) == "wav"

        calls.clear()
        with patch("plugin.framework.client.requests.sync_request", side_effect=fake_sync):
            clip2 = _download_endpoint_speech(
                "Again", "https://api.example", "k", model=model, voice="cedar",
            )
        assert clip2 is not None
        assert [item["response_format"] for item in calls] == ["wav"]
    finally:
        cfg._tts_response_format.clear()


def test_speak_endpoint_chunks_long_text():
    downloads: list[str] = []

    def fake_download(chunk, *args, **kwargs):
        del args, kwargs
        downloads.append(chunk)
        return _EndpointClip(b"RIFFfake", ".wav", "wav")

    with patch("plugin.audio.tts_service._download_endpoint_speech", side_effect=fake_download), \
         patch("plugin.audio.tts_service._play_audio_file", return_value=True) as play, \
         patch("plugin.audio.tts_service._release_temp"), \
         patch("plugin.audio.tts_service._speak_system") as system, \
         patch("plugin.audio.tts_service._playback_blocked", return_value=False):
        _speak_endpoint("A" * 4500, "https://api.openai.com/v1", "k", model="tts-1", voice="alloy")
    assert [len(chunk) for chunk in downloads] == [600, 3900]
    assert play.call_count == 2
    system.assert_not_called()


def test_speak_endpoint_short_reply_single_download():
    downloads: list[str] = []

    def fake_download(chunk, *args, **kwargs):
        del args, kwargs
        downloads.append(chunk)
        return _EndpointClip(b"RIFFfake", ".wav", "wav")

    with patch("plugin.audio.tts_service._download_endpoint_speech", side_effect=fake_download), \
         patch("plugin.audio.tts_service._play_audio_file", return_value=True) as play, \
         patch("plugin.audio.tts_service._release_temp"), \
         patch("plugin.audio.tts_service._speak_system") as system, \
         patch("plugin.audio.tts_service._playback_blocked", return_value=False):
        _speak_endpoint("A" * 4000, "https://api.openai.com/v1", "k", model="tts-1", voice="alloy")
    assert [len(chunk) for chunk in downloads] == [4000]
    assert play.call_count == 1
    system.assert_not_called()


def test_speak_endpoint_falls_back_with_unplayed_tail():
    downloads: list[str] = []

    def fake_download(chunk, *args, **kwargs):
        del args, kwargs
        downloads.append(chunk)
        if len(downloads) == 1:
            return _EndpointClip(b"RIFFfake", ".wav", "wav")
        return None

    with patch("plugin.audio.tts_service._download_endpoint_speech", side_effect=fake_download), \
         patch("plugin.audio.tts_service._play_audio_file", return_value=True) as play, \
         patch("plugin.audio.tts_service._release_temp"), \
         patch("plugin.audio.tts_service._speak_system") as system, \
         patch("plugin.audio.tts_service._playback_blocked", return_value=False):
        _speak_endpoint(("A" * 4000) + " tail stays", "https://api.openai.com/v1", "k", model="tts-1", voice="alloy")
    assert len(downloads) == 2
    play.assert_called_once()
    system.assert_called_once()
    assert "tail stays" in system.call_args.args[0]


def test_speak_endpoint_playback_failure_uses_system_speech():
    with patch("plugin.audio.tts_service._download_endpoint_speech", return_value=_EndpointClip(b"RIFFfake", ".wav", "wav")), \
         patch("plugin.audio.tts_service._play_audio_file", return_value=False), \
         patch("plugin.audio.tts_service._release_temp"), \
         patch("plugin.audio.tts_service._speak_system") as system, \
         patch("plugin.audio.tts_service._playback_blocked", return_value=False):
        _speak_endpoint("Hello", "https://api.openai.com/v1", "k", model="tts-1", voice="alloy")
    system.assert_called_once()
    assert system.call_args.args[0] == "Hello"


def test_speak_endpoint_stopped_during_playback_skips_system():
    import plugin.audio.tts_service as tts

    generation = tts._begin_utterance()

    def play(path, generation=None):
        del path, generation
        tts.stop_speech()
        return False

    try:
        with patch("plugin.audio.tts_service._download_endpoint_speech", return_value=_EndpointClip(b"RIFFfake", ".wav", "wav")), \
             patch("plugin.audio.tts_service._play_audio_file", side_effect=play), \
             patch("plugin.audio.tts_service._release_temp"), \
             patch("plugin.audio.tts_service._speak_system") as system:
            _speak_endpoint(
                "Hello", "https://api.openai.com/v1", "k", model="tts-1", voice="alloy",
                generation=generation,
            )
        system.assert_not_called()
    finally:
        tts.stop_speech()


def test_speak_endpoint_save_temp_failure():
    statuses: list[str] = []
    with patch("plugin.audio.tts_service._download_endpoint_speech", return_value=_EndpointClip(b"RIFFfake", ".wav", "wav")), \
         patch("plugin.audio.tts_service._new_speech_temp", return_value=None), \
         patch("plugin.audio.tts_service._speak_system") as system, \
         patch("plugin.audio.tts_service._playback_blocked", return_value=False):
        _speak_endpoint("Hello", "https://api.openai.com/v1", "k", on_status=statuses.append)
    assert any("Could not save speech audio" in s for s in statuses)
    system.assert_called_once_with("Hello", speed=1.0, generation=None)

    # Blocked generation suppresses save error
    statuses.clear()
    with patch("plugin.audio.tts_service._download_endpoint_speech", return_value=_EndpointClip(b"RIFFfake", ".wav", "wav")), \
         patch("plugin.audio.tts_service._new_speech_temp", return_value=None), \
         patch("plugin.audio.tts_service._speak_system") as system_blocked, \
         patch("plugin.audio.tts_service._playback_blocked", return_value=True):
        _speak_endpoint("Hello", "https://api.openai.com/v1", "k", on_status=statuses.append)
    assert not statuses
    system_blocked.assert_not_called()


def test_speak_endpoint_prefetch_pipeline_overlap():
    chunk2_download_started = threading.Event()
    download_calls = 0

    def fake_download(chunk, *args, **kwargs):
        nonlocal download_calls
        download_calls += 1
        if download_calls == 2:
            chunk2_download_started.set()
        return _EndpointClip(b"RIFFfake", ".wav", "wav")

    def fake_play(path, generation=None):
        del path, generation
        # Wait until second download is in flight before returning
        assert chunk2_download_started.wait(timeout=5.0)
        return True

    with patch("plugin.audio.tts_service._download_endpoint_speech", side_effect=fake_download), \
         patch("plugin.audio.tts_service._play_audio_file", side_effect=fake_play) as play, \
         patch("plugin.audio.tts_service._release_temp"), \
         patch("plugin.audio.tts_service._speak_system"), \
         patch("plugin.audio.tts_service._playback_blocked", return_value=False):
        _speak_endpoint("A" * 4500, "https://api.openai.com/v1", "k", model="tts-1", voice="alloy")

    assert play.call_count == 2
    assert chunk2_download_started.is_set()


def test_endpoint_empty_body_is_failure_not_a_file():
    statuses: list[str] = []
    cfg._tts_response_format.clear()
    try:
        with patch("plugin.framework.client.requests.sync_request", return_value=_speech_meta(b"", "audio/pcm;rate=24000")), \
             patch("plugin.audio.tts_service._new_speech_temp") as mock_new_temp:
            result = _download_endpoint_speech(
                "Hello",
                "https://api.example",
                "k",
                model="m",
                voice="v",
                on_status=statuses.append,
            )
            assert result is None
            mock_new_temp.assert_not_called()
            assert len(statuses) == 1
            assert statuses[0] == "Speech endpoint returned no audio."
            assert cfg.cached_tts_response_format(_tts_format_key("https://api.example", "m")) is None
    finally:
        cfg._tts_response_format.clear()


def test_speak_endpoint_later_chunk_failure_still_reports_status():
    recorded_status: list[object] = []
    downloads: list[str] = []

    def fake_download(chunk, *args, on_status=None, **kwargs):
        del args, kwargs
        recorded_status.append(on_status)
        downloads.append(chunk)
        if len(downloads) == 1:
            return _EndpointClip(b"RIFFfake", ".wav", "wav")
        return None

    sentinel_status = MagicMock()
    text = ("A" * 4000) + " tail stays"

    with patch("plugin.audio.tts_service._download_endpoint_speech", side_effect=fake_download), \
         patch("plugin.audio.tts_service._play_audio_file", return_value=True), \
         patch("plugin.audio.tts_service._release_temp"), \
         patch("plugin.audio.tts_service._speak_system") as system, \
         patch("plugin.audio.tts_service._playback_blocked", return_value=False):
        _speak_endpoint(
            text,
            "https://api.openai.com/v1",
            "k",
            model="tts-1",
            voice="alloy",
            on_status=sentinel_status,
        )

    assert len(downloads) == 2
    assert recorded_status == [sentinel_status, sentinel_status]
    system.assert_called_once()
    assert "tail stays" in system.call_args.args[0]


def test_stopped_post_logs_debug_not_warning(caplog):
    import logging
    from plugin.framework.errors import NetworkError

    caplog.set_level(logging.DEBUG)

    with patch(
        "plugin.framework.client.requests.sync_request",
        side_effect=NetworkError("LLM request aborted by Stop", code="STOPPED"),
    ):
        body, content_type, code, err = _post_audio_speech(
            "https://example.com/audio/speech", {}, {"model": "test"}
        )

    assert body is None
    assert content_type == ""
    assert code == 0
    assert "aborted by Stop" in err

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert not any("TTS POST" in msg for msg in warnings)

    debugs = [r.getMessage() for r in caplog.records if r.levelname == "DEBUG"]
    if not is_release_build():  # log.debug call site is stripped in release bundles
        assert any("aborted by Stop" in msg for msg in debugs)

    caplog.clear()

    with patch(
        "plugin.framework.client.requests.sync_request",
        side_effect=NetworkError("nope", code="HTTP_ERROR", details={"status": 503}),
    ):
        body2, content_type2, code2, err2 = _post_audio_speech(
            "https://example.com/audio/speech", {}, {"model": "test"}
        )

    assert body2 is None
    assert code2 == 503
    warnings2 = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("TTS POST" in msg and "503" in msg for msg in warnings2)
