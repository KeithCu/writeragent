from unittest.mock import MagicMock, patch


from plugin.audio.tts_service import (
    clean_text_for_speech,
    is_speaking,
    speak_text_async,
    stop_speech,
)


def test_clean_text_for_speech_markdown():
    raw = (
        "# Heading\n\n"
        "Here is some **bold** and *italic* text with a [link](https://example.com).\n\n"
        "```python\ndef foo():\n    return 42\n```\n\n"
        "And some `inline code` here."
    )
    cleaned = clean_text_for_speech(raw)
    assert "#" not in cleaned
    assert "**" not in cleaned
    assert "*" not in cleaned
    assert "https://example.com" not in cleaned
    assert "link" in cleaned
    assert "inline code" in cleaned
    assert "def foo" not in cleaned
    assert "[code block omitted]" in cleaned


def test_clean_text_for_speech_empty():
    assert clean_text_for_speech("") == ""
    assert clean_text_for_speech("   ") == ""


def test_speak_text_async_disabled_by_default():
    with patch("plugin.audio.tts_service.get_config", return_value=False):
        with patch("plugin.audio.tts_service.run_in_background") as mock_bg:
            speak_text_async("Hello world")
            mock_bg.assert_not_called()


def test_speak_text_async_enabled_system():
    with patch("plugin.audio.tts_service.get_config") as mock_get_config:
        # audio.tts_enabled -> True, audio.tts_provider -> "system"
        def _cfg(key, default=None):
            if key == "audio.tts_enabled":
                return True
            if key == "audio.tts_provider":
                return "system"
            if key == "audio.tts_speed":
                return 1.0
            return default

        mock_get_config.side_effect = _cfg

        with patch("plugin.audio.tts_service._speak_system") as mock_sys:
            # Run directly to verify worker execution
            with patch("plugin.audio.tts_service.run_in_background", side_effect=lambda fn, **kw: fn()):
                completed = False

                def _done():
                    nonlocal completed
                    completed = True

                speak_text_async("Testing system speech", on_complete=_done)
                mock_sys.assert_called_once()
                assert mock_sys.call_args.args == ("Testing system speech",)
                assert mock_sys.call_args.kwargs["speed"] == 1.0
                assert isinstance(mock_sys.call_args.kwargs["generation"], int)
                assert completed is True


def test_stop_speech_terminates_proc():
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with patch("plugin.audio.tts_service._play_proc", mock_proc):
        assert is_speaking() is True
        stop_speech()
        mock_proc.terminate.assert_called_once()


def test_resolve_tts_voice():
    from plugin.audio.tts_service import _resolve_tts_voice

    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "nova") == "af_sky"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "alloy") == "af_sky"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "shimmer") == "af_sarah"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "echo") == "am_adam"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "af_bella") == "af_bella"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "am_adam") == "am_adam"
    assert _resolve_tts_voice("openai/tts-1", "nova") == "nova"
    assert _resolve_tts_voice("openai/tts-1", "alloy") == "alloy"


def test_speak_endpoint_payload():
    from plugin.audio.tts_service import _speak_endpoint
    from plugin.framework.client import model_fetcher as cfg
    import json

    # A warm speech-list cache must not rewrite this assertion's catalog id.
    cfg._model_fetch_tts_cache.clear()
    cfg._tts_response_format.clear()

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"fake-audio-bytes"
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch("plugin.audio.tts_service._play_audio_file") as mock_play:
            _speak_endpoint("Test hello", "https://openrouter.ai/api", "test-key", model="hexgrad/Kokoro-82M", voice="nova")
            mock_play.assert_called_once()

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://openrouter.ai/api/v1/audio/speech"
        assert req.headers["Authorization"] == "Bearer test-key"
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["model"] == "hexgrad/Kokoro-82M"
        assert payload["voice"] == "af_sky"
        assert payload["response_format"] == "mp3"


def test_speak_endpoint_prefers_cached_openrouter_speech_id():
    from plugin.audio.tts_service import _speak_endpoint
    from plugin.framework.client import model_fetcher as cfg
    import json

    cfg._model_fetch_tts_cache.clear()
    cfg._tts_response_format.clear()
    cfg._model_fetch_tts_cache["speech-test"] = ["hexgrad/kokoro-82m", "microsoft/mai-voice-2"]
    try:
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b"fake-audio-bytes"
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            with patch("plugin.audio.tts_service._play_audio_file"):
                _speak_endpoint(
                    "Test hello",
                    "https://openrouter.ai/api",
                    "test-key",
                    model="hexgrad/Kokoro-82M",
                    voice="af_bella",
                )
            payload = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
            assert payload["model"] == "hexgrad/kokoro-82m"
            assert payload["voice"] == "af_bella"
    finally:
        cfg._model_fetch_tts_cache.clear()
        cfg._tts_response_format.clear()


def _speech_response(data: bytes, content_type: str):
    class _Resp:
        def __init__(self) -> None:
            self.headers = {"Content-Type": content_type}
            self.status = 200

        def read(self) -> bytes:
            return data

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

    return _Resp()


def _speech_http_error(code: int, body: str):
    import io
    import urllib.error

    return urllib.error.HTTPError(
        "https://openrouter.ai/api/v1/audio/speech",
        code,
        "error",
        hdrs=None,
        fp=io.BytesIO(body.encode("utf-8")),
    )


def test_endpoint_pcm_failure_is_remembered_and_wrapped():
    """Gemini-like 400 on mp3 then pcm: cache pcm, wrap s16le as WAV, next call asks pcm."""
    import json
    import struct

    from plugin.audio.tts_service import _download_endpoint_speech, _release_temp
    from plugin.framework.client import model_fetcher as cfg

    model = "google/gemini-2.5-flash-preview-tts"
    cfg._tts_response_format.clear()
    cfg._model_fetch_tts_cache.clear()
    pcm = b"\x01\x00\x02\x00"
    body = '{"error":{"message":"response_format must be pcm"}}'
    calls: list[dict] = []
    statuses: list[str] = []
    # First download: reject mp3 once, then return pcm. Later downloads are pcm-only.
    reject_mp3 = True

    def urlopen(req, timeout=None):
        del timeout
        payload = json.loads(req.data.decode("utf-8"))
        calls.append(payload)
        if reject_mp3 and payload["response_format"] == "mp3":
            raise _speech_http_error(400, body)
        return _speech_response(pcm, "audio/pcm;rate=16000")

    path = None
    path2 = None
    try:
        with patch("urllib.request.urlopen", side_effect=urlopen):
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
        with patch("urllib.request.urlopen", side_effect=urlopen):
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
    import struct

    from plugin.audio.tts_service import _pcm_rate_from_content_type, _pcm_s16le_to_wav

    assert _pcm_rate_from_content_type("audio/pcm") == 24000
    assert _pcm_rate_from_content_type("audio/L16;rate=22050") == 22050
    wav = _pcm_s16le_to_wav(b"\x00\x00", 24000)
    assert wav[:4] == b"RIFF"
    assert struct.unpack_from("<I", wav, 24)[0] == 24000
    assert struct.unpack_from("<H", wav, 22)[0] == 1
    assert struct.unpack_from("<H", wav, 34)[0] == 16


def test_endpoint_wav_error_retries_wav():
    import json

    from plugin.audio.tts_service import _download_endpoint_speech, _release_temp
    from plugin.framework.client import model_fetcher as cfg

    model = "vendor/wav-only"
    cfg._tts_response_format.clear()
    calls: list[dict] = []
    wav_bytes = b"RIFF" + b"\x00" * 40

    def urlopen(req, timeout=None):
        del timeout
        payload = json.loads(req.data.decode("utf-8"))
        calls.append(payload)
        if payload["response_format"] == "mp3":
            raise _speech_http_error(400, "unsupported response_format mp3; use wav")
        return _speech_response(wav_bytes, "audio/wav")

    path = None
    try:
        with patch("urllib.request.urlopen", side_effect=urlopen):
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


def test_endpoint_mp3_success_does_not_cache_format():
    import json

    from plugin.audio.tts_service import _download_endpoint_speech, _release_temp
    from plugin.framework.client import model_fetcher as cfg

    model = "x-ai/grok-voice-tts-1.0"
    cfg._tts_response_format.clear()
    calls: list[dict] = []

    def urlopen(req, timeout=None):
        del timeout
        calls.append(json.loads(req.data.decode("utf-8")))
        return _speech_response(b"ID3fake-mp3", "audio/mpeg")

    path = None
    try:
        with patch("urllib.request.urlopen", side_effect=urlopen):
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
    from plugin.audio.tts_service import _download_endpoint_speech
    from plugin.framework.client import model_fetcher as cfg

    cfg._tts_response_format.clear()
    statuses: list[str] = []

    def urlopen(req, timeout=None):
        del req, timeout
        raise _speech_http_error(401, '{"error":{"message":"invalid api key"}}')

    try:
        with patch("urllib.request.urlopen", side_effect=urlopen):
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


def test_endpoint_voice_options_follow_cached_openrouter_voices():
    from plugin.audio.tts_service import (
        get_scoped_tts_voice,
        get_voice_catalog,
        get_voice_family,
        voice_options_for_provider,
    )
    from plugin.framework.client import model_fetcher as cfg

    gemini = "google/gemini-2.5-flash-preview-tts"
    grok = "x-ai/grok-voice-tts-1.0"
    cfg._tts_supported_voices.pop(gemini, None)
    cfg._tts_supported_voices.pop(grok, None)
    cfg._tts_supported_voices.pop("hexgrad/Kokoro-82M", None)
    cfg._tts_supported_voices.pop("hexgrad/kokoro-82m", None)
    cfg._model_fetch_tts_cache.clear()
    try:
        # API order is not alphabetical. The combo sorts labels; Kokoro does not.
        cfg._tts_supported_voices[gemini] = ["Zephyr", "Puck", "Kore"]
        assert get_voice_family("endpoint", gemini) == "openrouter"
        assert get_voice_family("endpoint", "hexgrad/Kokoro-82M") == "kokoro"
        assert get_voice_family("kokoro") == "kokoro"
        assert get_voice_family("piper") == "piper"
        assert voice_options_for_provider("endpoint", gemini) == [
            {"value": "Kore", "label": "Kore"},
            {"value": "Puck", "label": "Puck"},
            {"value": "Zephyr", "label": "Zephyr"},
        ]
        assert voice_options_for_provider("piper", "") == get_voice_catalog("piper")
        assert voice_options_for_provider("kokoro", "") == get_voice_catalog("kokoro")
        assert voice_options_for_provider("endpoint", "hexgrad/Kokoro-82M") == get_voice_catalog("kokoro")
        kokoro_labels = [row["label"] for row in get_voice_catalog("kokoro", "en_US")]
        assert kokoro_labels != sorted(kokoro_labels, key=str.casefold)
        cfg._tts_supported_voices["case-model"] = ["nova", "Alloy", "echo"]
        assert [row["label"] for row in voice_options_for_provider("endpoint", "case-model")] == [
            "Alloy",
            "echo",
            "nova",
        ]

        store = {
            "audio.tts_voice_openrouter": "alloy",
            "audio.tts_voice": "alloy",
        }

        def _cfg(key, default=None):
            return store.get(key, default)

        with patch("plugin.audio.tts_service.get_config", side_effect=_cfg):
            # Missing id and no Aoede: speak uses the first label-sorted id.
            assert get_scoped_tts_voice("endpoint", gemini) == "Kore"
            store["audio.tts_voice_openrouter"] = "Puck"
            assert get_scoped_tts_voice("endpoint", gemini) == "Puck"

        cfg._tts_supported_voices.pop(gemini, None)
        cfg._model_fetch_tts_cache["speech"] = [grok]
        assert voice_options_for_provider("endpoint", grok) == []
        assert get_voice_family("endpoint", grok) == "openrouter"

        cfg._model_fetch_tts_cache.clear()
        with patch("plugin.framework.config.get_current_endpoint", return_value="https://openrouter.ai/api"):
            assert voice_options_for_provider("endpoint", gemini) == []
        # Together is not the OpenAI alloy list. A model with no /v1/voices rows stays empty.
        with patch("plugin.framework.config.get_current_endpoint", return_value="https://api.together.xyz"), \
             patch("plugin.framework.client.requests.sync_request", return_value={"model": "openai/tts-1", "voices": []}):
            rows = voice_options_for_provider("endpoint", "openai/tts-1")
            assert rows == []
            assert get_voice_family("endpoint", "openai/tts-1") == "together"
        with patch("plugin.framework.config.get_current_endpoint", return_value="https://api.openai.com"):
            rows = voice_options_for_provider("endpoint", "openai/tts-1")
            labels = [row["label"] for row in rows]
            assert labels == sorted(labels, key=str.casefold)
            assert labels[0].startswith("alloy")
            assert get_voice_family("endpoint", "openai/tts-1") == "openai"
    finally:
        cfg._tts_supported_voices.pop(gemini, None)
        cfg._tts_supported_voices.pop(grok, None)
        cfg._tts_supported_voices.pop("case-model", None)
        cfg._model_fetch_tts_cache.clear()
        cfg._together_voices_fetch_cache.clear()


def test_preferred_harvested_voice_prefers_aoede_for_gemini():
    """Gemini fallback is Aoede when the harvested list advertises it.

    Achernar sorts before Aoede, and Zephyr is first in API order, so neither
    "first entry" would be Aoede. A saved in-list id still wins.
    """
    from plugin.audio.tts_service import _preferred_harvested_voice, get_scoped_tts_voice
    from plugin.framework.client import model_fetcher as cfg

    gemini = "google/gemini-2.5-flash-preview-tts"
    grok = "x-ai/grok-voice-tts-1.0"
    advertised = ["Zephyr", "Achernar", "Aoede"]
    assert _preferred_harvested_voice(gemini, advertised) == "Aoede"
    assert _preferred_harvested_voice("Google/GEMINI-2.5-flash-preview-tts", ["Zephyr", "aoede"]) == "aoede"
    assert _preferred_harvested_voice(gemini, ["Zephyr", "Puck", "Kore"]) == "Kore"
    assert _preferred_harvested_voice(grok, advertised) == "Achernar"
    assert _preferred_harvested_voice("canopylabs/orpheus-3b-0.1-ft", ["tara", "leah"]) == "leah"

    cfg._tts_supported_voices.pop(gemini, None)
    cfg._tts_supported_voices.pop(grok, None)
    folded = "models/Gemini-2.5-flash-preview-tts"
    cfg._tts_supported_voices.pop(folded, None)
    try:
        cfg._tts_supported_voices[gemini] = list(advertised)
        cfg._tts_supported_voices[grok] = list(advertised)

        def _run(model: str, store: dict[str, str]) -> str:
            with patch("plugin.audio.tts_service.get_config", side_effect=lambda key, default=None: store.get(key, default)):
                return get_scoped_tts_voice("endpoint", model)

        missing = {"audio.tts_voice_openrouter": "alloy", "audio.tts_voice": "alloy"}
        assert _run(gemini, missing) == "Aoede"
        assert _run(grok, dict(missing)) == "Achernar"

        assert _run(gemini, {"audio.tts_voice_openrouter": "Aoede", "audio.tts_voice": "alloy"}) == "Aoede"
        assert _run(grok, {"audio.tts_voice_openrouter": "Aoede", "audio.tts_voice": "alloy"}) == "Aoede"
        assert _run(gemini, {"audio.tts_voice_openrouter": "Zephyr", "audio.tts_voice": "alloy"}) == "Zephyr"
        # Scoped id is missing; the general voice is still in the harvested list.
        assert _run(gemini, {"audio.tts_voice_openrouter": "alloy", "audio.tts_voice": "Zephyr"}) == "Zephyr"

        cfg._tts_supported_voices[gemini] = ["Zephyr", "Puck", "Kore"]
        assert _run(gemini, missing) == "Kore"

        cfg._tts_supported_voices[folded] = ["Puck", "aoede"]
        assert _run(folded, {"audio.tts_voice_openrouter": "alloy", "audio.tts_voice": ""}) == "aoede"
    finally:
        cfg._tts_supported_voices.pop(gemini, None)
        cfg._tts_supported_voices.pop(grok, None)
        cfg._tts_supported_voices.pop(folded, None)


def test_is_speaking_lifecycle():
    assert is_speaking() is False
    with patch("plugin.audio.tts_service._speech_active", True):
        assert is_speaking() is True
    assert is_speaking() is False


def test_get_tts_model_sanitizes_placeholder():
    from plugin.framework.client.model_fetcher import get_tts_model

    with patch("plugin.framework.client.model_fetcher.get_config", return_value="(Default for current endpoint)"):
        with patch("plugin.framework.client.model_fetcher.get_current_endpoint", return_value="https://openrouter.ai/api"):
            model = get_tts_model()
            assert model == "hexgrad/Kokoro-82M"
            assert "(Default for current endpoint)" not in model


def test_clean_provider_and_voice_names():
    from plugin.audio.tts_service import clean_provider_name, clean_voice_name

    assert clean_provider_name("Kokoro (Local Neural, ONNX CPU)") == "kokoro"
    assert clean_provider_name("Piper (Local Fast Neural, CPU)") == "piper"
    assert clean_provider_name("LLM Endpoint") == "endpoint"
    # Older builds showed this label; a stale combo string still maps to endpoint.
    assert clean_provider_name("Current Chat Endpoint (/audio/speech)") == "endpoint"
    assert clean_provider_name("OS Native (say / SAPI / spd-say)") == "system"

    assert clean_voice_name("af_bella (Kokoro US Female - Bella)") == "af_bella"
    assert clean_voice_name("en_US-lessac-medium (Piper US Female - Lessac)") == "en_US-lessac-medium"
    assert clean_voice_name("alloy (OpenAI Neutral)") == "alloy"
    assert clean_voice_name("alloy") == "alloy"


def test_get_voice_family():
    from plugin.audio.tts_service import get_voice_family

    assert get_voice_family("kokoro") == "kokoro"
    assert get_voice_family("piper") == "piper"
    assert get_voice_family("system") == "system"
    assert get_voice_family("endpoint", "hexgrad/Kokoro-82M") == "kokoro"
    with patch("plugin.framework.config.get_current_endpoint", return_value="https://api.openai.com"):
        assert get_voice_family("endpoint", "openai/tts-1") == "openai"
    with patch("plugin.framework.config.get_current_endpoint", return_value="https://api.together.xyz"):
        assert get_voice_family("endpoint", "cartesia/sonic") == "together"
        assert get_voice_family("endpoint", "hexgrad/Kokoro-82M") == "kokoro"


def test_together_voice_options_use_cached_voices_and_fetch_on_miss():
    from plugin.audio.tts_service import voice_options_for_provider
    from plugin.framework.client import model_fetcher as cfg

    cfg._tts_supported_voices.clear()
    cfg._together_voices_fetch_cache.clear()
    cfg.remember_tts_supported_voices(
        "canopylabs/orpheus-3b-0.1-ft",
        ["tara", "leah"],
    )
    try:
        with patch("plugin.framework.client.requests.sync_request") as mock_sync:
            options = voice_options_for_provider(
                "endpoint",
                "canopylabs/orpheus-3b-0.1-ft",
                endpoint="https://api.together.xyz",
            )
            mock_sync.assert_not_called()
        # Cached Together order is tara, leah. The combo sorts labels.
        assert [opt["value"] for opt in options] == ["leah", "tara"]
        assert options[0]["label"] == "leah"

        payload = {
            "model": "cartesia/sonic",
            "voices": [{"name": "Sidekick", "id": "cart-id", "language": "en"}],
        }
        with patch("plugin.framework.client.requests.sync_request", return_value=payload) as mock_sync:
            cartesia = voice_options_for_provider(
                "endpoint",
                "cartesia/sonic",
                endpoint="https://api.together.xyz",
                api_key="sk-test",
            )
            assert "model=cartesia%2Fsonic" in mock_sync.call_args[0][0]
        assert [opt["value"] for opt in cartesia] == ["cart-id"]

        with patch("plugin.framework.client.requests.sync_request") as mock_sync:
            local = voice_options_for_provider("kokoro", "hexgrad/Kokoro-82M")
            mock_sync.assert_not_called()
        assert any(opt["value"] == "af_bella" for opt in local)
        assert any("Kokoro" in opt["label"] for opt in local)
    finally:
        cfg._tts_supported_voices.clear()
        cfg._together_voices_fetch_cache.clear()


def test_scoped_tts_voice_persistence():
    from plugin.audio.tts_service import get_scoped_tts_voice, set_scoped_tts_voice

    store = {}
    with patch("plugin.audio.tts_service.get_config", side_effect=lambda k, d=None: store.get(k, d)), \
         patch("plugin.audio.tts_service.set_config", side_effect=lambda k, v: store.__setitem__(k, v)):
        # Default for kokoro
        assert get_scoped_tts_voice("kokoro") == "af_sky"
        # Set kokoro voice
        set_scoped_tts_voice("af_sarah", "kokoro")
        assert get_scoped_tts_voice("kokoro") == "af_sarah"
        assert store.get("audio.tts_voice_kokoro") == "af_sarah"
        assert store.get("audio.tts_voice") == "af_sarah"

        # Piper has its own scoped voice
        assert get_scoped_tts_voice("piper") == "en_US-lessac-medium"
        set_scoped_tts_voice("en_US-amy-medium", "piper")
        assert get_scoped_tts_voice("piper") == "en_US-amy-medium"
        assert store.get("audio.tts_voice_piper") == "en_US-amy-medium"

        # Kokoro is still preserved
        assert get_scoped_tts_voice("kokoro") == "af_sarah"


def test_speak_text_async_routing_local():
    from plugin.audio.tts_service import speak_text_async

    cfg = {
        "audio.tts_enabled": True,
        "audio.tts_provider": "kokoro",
        "audio.tts_speed": 1.1,
        "audio.tts_voice_kokoro": "af_bella",
    }
    with patch("plugin.audio.tts_service.get_config", side_effect=lambda k, d=None: cfg.get(k, d)), \
         patch("plugin.audio.tts_service.run_in_background", side_effect=lambda fn, **kw: fn()), \
         patch("plugin.audio.tts_service._speak_kokoro_local") as mock_kokoro, \
         patch("plugin.audio.tts_service._speak_piper_local") as mock_piper:
        speak_text_async("Hello Kokoro")
        assert mock_kokoro.call_args.args == ("Hello Kokoro",)
        assert mock_kokoro.call_args.kwargs["voice"] == "af_bella"
        assert mock_kokoro.call_args.kwargs["speed"] == 1.1
        assert mock_kokoro.call_args.kwargs["on_status"] is None
        assert isinstance(mock_kokoro.call_args.kwargs["generation"], int)
        mock_piper.assert_not_called()

        cfg["audio.tts_provider"] = "piper"
        cfg["audio.tts_voice_piper"] = "en_US-lessac-medium"
        speak_text_async("Hello Piper")
        assert mock_piper.call_args.args == ("Hello Piper",)
        assert mock_piper.call_args.kwargs["voice"] == "en_US-lessac-medium"
        assert mock_piper.call_args.kwargs["speed"] == 1.1
        assert mock_piper.call_args.kwargs["on_status"] is None


def test_parse_tts_speed():
    from plugin.audio.tts_service import parse_tts_speed

    assert parse_tts_speed(1) == 1.0
    assert parse_tts_speed(1.0) == 1.0
    assert parse_tts_speed("1") == 1.0
    assert parse_tts_speed("1.0") == 1.0
    assert parse_tts_speed("1.0x") == 1.0
    assert parse_tts_speed("1.0x (Normal)") == 1.0
    assert parse_tts_speed("1.1") == 1.1
    assert parse_tts_speed("1.1x") == 1.1
    assert parse_tts_speed("1.25x") == 1.25
    assert parse_tts_speed("1,5x") == 1.5
    assert parse_tts_speed("1.75x") == 1.75
    assert parse_tts_speed("2x") == 2.0
    assert parse_tts_speed("0.5") == 0.5
    assert parse_tts_speed("0.25") == 0.25
    # Clamped to slowest 0.25
    assert parse_tts_speed("0.1") == 0.25
    assert parse_tts_speed("0") == 0.25
    assert parse_tts_speed(-2) == 0.25
    # Fallback to default 1.0 on invalid
    assert parse_tts_speed("abc") == 1.0
    assert parse_tts_speed(None) == 1.0
    assert parse_tts_speed("") == 1.0


def test_get_default_voice_for_locale():
    from plugin.audio.tts_service import get_default_voice_for_locale

    # Piper per-locale defaults
    assert get_default_voice_for_locale("piper", "de_DE") == "de_DE-thorsten-medium"
    assert get_default_voice_for_locale("piper", "de") == "de_DE-thorsten-medium"
    assert get_default_voice_for_locale("piper", "fr_FR") == "fr_FR-siwis-medium"
    assert get_default_voice_for_locale("piper", "es_ES") == "es_ES-davefx-medium"
    assert get_default_voice_for_locale("piper", "it_IT") == "it_IT-paola-medium"
    assert get_default_voice_for_locale("piper", "ru_RU") == "ru_RU-denis-medium"
    assert get_default_voice_for_locale("piper", "zh_CN") == "zh_CN-huayan-medium"
    assert get_default_voice_for_locale("piper", "ja_JP") == "ja_JP-hi_fi_captain-medium"
    assert get_default_voice_for_locale("piper", "nb_NO") == "no_NO-talesyntese-medium"
    assert get_default_voice_for_locale("piper", "nn_NO") == "no_NO-talesyntese-medium"
    assert get_default_voice_for_locale("piper", "hr") == "sl_SI-artur-medium"
    assert get_default_voice_for_locale("piper", "en_US") == "en_US-lessac-medium"

    # Kokoro per-locale defaults
    assert get_default_voice_for_locale("kokoro", "es_ES") == "ef_dora"
    assert get_default_voice_for_locale("kokoro", "fr_FR") == "ff_siwis"
    assert get_default_voice_for_locale("kokoro", "it_IT") == "if_sara"
    assert get_default_voice_for_locale("kokoro", "ja_JP") == "jf_alpha"
    assert get_default_voice_for_locale("kokoro", "zh_CN") == "zf_xiaobei"
    assert get_default_voice_for_locale("kokoro", "hi_IN") == "hf_alpha"
    assert get_default_voice_for_locale("kokoro", "pt_BR") == "pf_dora"
    assert get_default_voice_for_locale("kokoro", "en_US") == "af_sky"
    assert get_default_voice_for_locale("kokoro", "de_DE") == "af_sky"


def test_get_voice_catalog_locale_prioritized():
    from plugin.audio.tts_service import get_voice_catalog

    # German locale puts Thorsten first
    de_catalog = get_voice_catalog("piper", "de_DE")
    assert de_catalog[0]["value"] == "de_DE-thorsten-medium"
    assert any("de_DE-thorsten_emotional-medium" == v["value"] for v in de_catalog[:3])

    # French locale puts Siwis first
    fr_catalog = get_voice_catalog("piper", "fr_FR")
    assert fr_catalog[0]["value"] == "fr_FR-siwis-medium"

    # Spanish locale with Kokoro puts Dora first
    es_kokoro = get_voice_catalog("kokoro", "es_ES")
    assert es_kokoro[0]["value"] == "ef_dora"

    # English locale puts Lessac first
    en_catalog = get_voice_catalog("piper", "en_US")
    assert en_catalog[0]["value"] == "en_US-lessac-medium"


def test_kokoro_g2p_lang_english_on_non_english_voice():
    """Latin text falls back to English espeak for Asian voices; Romance voices keep native Misaki lang."""
    from plugin.audio.tts_service import kokoro_g2p_lang

    english = "Hello, I'm your LibreOffice WriterAgent."
    # Asian voices fall back to en-us when text is Latin script:
    assert kokoro_g2p_lang(english, "jf_alpha") == "en-us"
    assert kokoro_g2p_lang(english, "zf_xiaobei") == "en-us"
    assert kokoro_g2p_lang(english, "hf_alpha") == "en-us"
    # English voices use en-us or en-gb:
    assert kokoro_g2p_lang("hello", "af_bella") == "en-us"
    assert kokoro_g2p_lang(english, "bf_emma") == "en-gb"
    # Romance voices keep their native Misaki language:
    assert kokoro_g2p_lang("Café au lait", "ff_siwis") == "fr-fr"
    assert kokoro_g2p_lang("Bonjour, je suis WriterAgent.", "ff_siwis") == "fr-fr"
    assert kokoro_g2p_lang("Hola, ¿cómo estás?", "ef_dora") == "es"
    assert kokoro_g2p_lang("Ciao, come stai?", "if_sara") == "it"
    assert kokoro_g2p_lang("Olá, tudo bem?", "pf_dora") == "pt-br"
    # Non-Latin voices use their Misaki language when non-Latin characters are present:
    assert kokoro_g2p_lang("こんにちは、WriterAgent です。", "jf_alpha") == "ja"
    assert kokoro_g2p_lang("你好", "zf_xiaobei") == "zh"
    assert kokoro_g2p_lang("नमस्ते", "hf_alpha") == "hi"


def test_tts_test_sample_uses_gettext():
    from plugin.audio.tts_service import TTS_TEST_SAMPLE, tts_test_sample

    with patch("plugin.audio.tts_service._", return_value="Bonjour, je suis WriterAgent.") as mock_gettext:
        assert tts_test_sample() == "Bonjour, je suis WriterAgent."
        mock_gettext.assert_called_once_with(TTS_TEST_SAMPLE)


def test_speak_text_async_uses_dialog_overrides_when_config_disabled():
    """Test voice speaks the on-screen controls before OK saves audio.tts_enabled."""
    from plugin.audio.tts_service import speak_text_async

    with patch("plugin.audio.tts_service.get_config", return_value=False), \
         patch("plugin.audio.tts_service.run_in_background", side_effect=lambda fn, **kw: fn()), \
         patch("plugin.audio.tts_service._speak_kokoro_local") as mock_kokoro:
        speak_text_async(
            "Bonjour",
            provider="Kokoro (Local Neural, ONNX CPU)",
            voice="jf_alpha (Kokoro JP Female - Alpha)",
            speed=1.25,
            enabled=True,
        )
        mock_kokoro.assert_called_once()
        assert mock_kokoro.call_args.args == ("Bonjour",)
        assert mock_kokoro.call_args.kwargs["voice"] == "jf_alpha"
        assert mock_kokoro.call_args.kwargs["speed"] == 1.25
        assert mock_kokoro.call_args.kwargs["on_status"] is None
        assert isinstance(mock_kokoro.call_args.kwargs["generation"], int)


def test_kokoro_lang_for_voice():
    from plugin.audio.tts_service import _kokoro_lang_for_voice

    assert _kokoro_lang_for_voice("af_bella") == "en-us"
    assert _kokoro_lang_for_voice("am_adam") == "en-us"
    assert _kokoro_lang_for_voice("bf_emma") == "en-gb"
    assert _kokoro_lang_for_voice("bm_george") == "en-gb"
    assert _kokoro_lang_for_voice("ef_dora") == "es"
    assert _kokoro_lang_for_voice("em_alex") == "es"
    assert _kokoro_lang_for_voice("ff_siwis") == "fr-fr"
    assert _kokoro_lang_for_voice("if_sara") == "it"
    assert _kokoro_lang_for_voice("jf_alpha") == "ja"
    assert _kokoro_lang_for_voice("zf_xiaobei") == "zh"
    assert _kokoro_lang_for_voice("hf_alpha") == "hi"
    assert _kokoro_lang_for_voice("pf_dora") == "pt-br"


def test_resolve_piper_model_file_ondemand_download(tmp_path):
    from plugin.audio.tts_service import _resolve_piper_model_file
    import io
    import os

    cache_dir = tmp_path / "piper_cache"
    with patch("os.path.expanduser", return_value=str(cache_dir)):
        with patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: io.BytesIO(b"fake-piper-model-bytes")):
            resolved = _resolve_piper_model_file("de_DE-thorsten-medium")
            assert resolved == str(cache_dir / "de_DE-thorsten-medium.onnx")
            assert os.path.exists(resolved)
            assert os.path.exists(str(cache_dir / "de_DE-thorsten-medium.onnx.json"))


def test_module_yaml_voice_options_use_catalog_provider():
    """Settings yaml keeps a short stub; the catalog provider fills the real list."""
    import os

    import yaml

    with open(os.path.join(os.path.dirname(__file__), "..", "..", "plugin", "audio", "module.yaml"), encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle)
    voice = manifest["config"]["tts_voice"]
    assert voice["options_provider"] == "plugin.audio.tts_service:settings_voice_options"
    assert len(voice["options"]) <= 4


def test_voice_catalog_asset_drives_runtime_maps():
    import json
    import os

    from plugin.audio.tts_service import VOICE_CATALOGS, _PIPER_VOICE_MODELS, _KOKORO_CATALOG_ITEMS
    from plugin.audio.voice_catalog import CATALOG_PATH, voice_short_name

    assert os.path.isfile(CATALOG_PATH)
    with open(CATALOG_PATH, encoding="utf-8") as handle:
        raw = json.load(handle)

    piper_ids = [row["id"] for row in raw["piper"]["voices"]]
    assert list(_PIPER_VOICE_MODELS) == piper_ids
    assert [item["value"] for item in VOICE_CATALOGS["piper"]] == piper_ids
    kokoro_ids = [row["id"] for row in raw["kokoro"]["voices"]]
    assert [item["value"] for item in _KOKORO_CATALOG_ITEMS] == kokoro_ids
    # Sky is the default; Bella stays a selectable catalog voice.
    assert "af_sky" in kokoro_ids
    assert "af_bella" in kokoro_ids
    assert raw["kokoro"]["fallback_voice"] == "af_sky"
    assert voice_short_name("de_DE-thorsten-medium") == "Thorsten"
    assert voice_short_name("en_US-lessac-medium") == "Lessac"
    # Catalog file is the model path used for on-demand Hugging Face downloads.
    onnx, config, lang, _label = _PIPER_VOICE_MODELS["de_DE-thorsten-medium"]
    assert onnx.endswith("de_DE-thorsten-medium.onnx")
    assert config.endswith(".onnx.json")
    assert lang == "de_DE"


def test_settings_voice_options_match_catalog_for_provider():
    from plugin.audio.tts_service import get_voice_catalog, settings_voice_options

    def _cfg(key, default=None):
        values = {
            "audio.tts_provider": "piper",
            "audio.tts_model": "",
        }
        return values.get(key, default)

    with patch("plugin.audio.tts_service.get_config", side_effect=_cfg), \
         patch("plugin.framework.i18n.get_active_locale", return_value="de_DE"):
        options = settings_voice_options(None)

    assert options == get_voice_catalog("piper", "de_DE")
    assert options[0]["value"] == "de_DE-thorsten-medium"
    assert any(opt["value"] == "fr_FR-siwis-medium" for opt in options)
    assert all(opt["value"] != "af_bella" for opt in options)


def test_resolve_piper_model_file_reports_download_status(tmp_path):
    from plugin.audio.tts_service import _resolve_piper_model_file
    import io
    import os

    messages: list[str] = []
    cache_dir = tmp_path / "piper_cache"
    with patch("os.path.expanduser", return_value=str(cache_dir)):
        with patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: io.BytesIO(b"fake-piper-model-bytes")):
            resolved = _resolve_piper_model_file("de_DE-thorsten-medium", on_status=messages.append)

    assert resolved == str(cache_dir / "de_DE-thorsten-medium.onnx")
    assert os.path.exists(resolved)
    assert messages == ["Downloading Piper voice Thorsten…"]


def test_resolve_piper_model_file_reports_lessac_fallback(tmp_path):
    from plugin.audio.tts_service import _resolve_piper_model_file
    import urllib.error

    messages: list[str] = []
    cache_dir = tmp_path / "piper_cache"
    cache_dir.mkdir()
    (cache_dir / "en_US-lessac-medium.onnx").write_bytes(b"lessac")
    (cache_dir / "en_US-lessac-medium.onnx.json").write_bytes(b"{}")

    with patch("plugin.audio.tts_service.os.path.expanduser", return_value=str(cache_dir)):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            resolved = _resolve_piper_model_file("de_DE-thorsten-medium", on_status=messages.append)

    assert resolved == str(cache_dir / "en_US-lessac-medium.onnx")
    assert messages == [
        "Downloading Piper voice Thorsten…",
        "Couldn't download Thorsten; using Lessac",
    ]


def test_resolve_piper_model_file_reports_os_speech_fallback(tmp_path):
    from plugin.audio.tts_service import _resolve_piper_model_file
    import urllib.error

    messages: list[str] = []
    cache_dir = tmp_path / "piper_cache"

    with patch("plugin.audio.tts_service.os.path.expanduser", return_value=str(cache_dir)):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            with patch("urllib.request.urlretrieve", side_effect=urllib.error.URLError("offline")):
                resolved = _resolve_piper_model_file("de_DE-thorsten-medium", on_status=messages.append)

    assert resolved == "de_DE-thorsten-medium"
    assert messages[0] == "Downloading Piper voice Thorsten…"
    assert "using Lessac" in messages[1]
    assert messages[-1] == "Couldn't download Thorsten; using OS speech"


def test_resolve_kokoro_model_files_reports_download_failure(tmp_path):
    from plugin.audio.tts_service import _resolve_kokoro_model_files
    import urllib.error

    messages: list[str] = []
    cache_dir = tmp_path / "kokoro_cache"

    with patch("plugin.audio.tts_service.os.path.expanduser", return_value=str(cache_dir)), \
         patch("plugin.audio.tts_service.os.makedirs"), \
         patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}), \
         patch("urllib.request.urlretrieve", side_effect=urllib.error.URLError("offline")):
        model_path, voices_path = _resolve_kokoro_model_files(on_status=messages.append)

    assert model_path.endswith("kokoro-v1.0.onnx")
    assert voices_path.endswith("voices-v1.0.bin")
    assert messages == [
        "Downloading Kokoro voice model…",
        "Couldn't download Kokoro; using OS speech",
    ]


def test_resolve_kokoro_model_files_downloads_multilingual_release(tmp_path):
    """Auto-download must fetch the v1.0 pack, not the English-only model-files release."""
    from plugin.audio.tts_service import (
        _KOKORO_MODEL_FILENAME,
        _KOKORO_RELEASE_BASE,
        _KOKORO_VOICES_FILENAME,
        _resolve_kokoro_model_files,
    )

    cache_dir = tmp_path / "kokoro_cache"
    downloaded: list[tuple[str, str]] = []

    def _fake_retrieve(url: str, dest: str) -> None:
        downloaded.append((url, dest))
        with open(dest, "wb") as handle:
            handle.write(b"asset")

    with patch("plugin.audio.tts_service.os.path.expanduser", return_value=str(cache_dir)), \
         patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}, clear=False), \
         patch("urllib.request.urlretrieve", side_effect=_fake_retrieve):
        model_path, voices_path = _resolve_kokoro_model_files()

    urls = [url for url, _dest in downloaded]
    assert urls == [
        f"{_KOKORO_RELEASE_BASE}/{_KOKORO_VOICES_FILENAME}",
        f"{_KOKORO_RELEASE_BASE}/{_KOKORO_MODEL_FILENAME}",
    ]
    assert _KOKORO_RELEASE_BASE.endswith("/model-files-v1.1")
    assert "/model-files/" not in _KOKORO_RELEASE_BASE
    assert "v0_19" not in _KOKORO_MODEL_FILENAME
    assert _KOKORO_VOICES_FILENAME != "voices.bin"
    assert model_path == str(cache_dir / "kokoro-v1.0.onnx")
    assert voices_path == str(cache_dir / "voices-v1.0.bin")
    assert all("v0_19" not in url and not url.endswith("/voices.bin") for url in urls)


def test_resolve_kokoro_model_files_supersedes_english_only_cache(tmp_path):
    """An existing v0_19 / voices.bin cache must not block the v1.0 download."""
    from plugin.audio.tts_service import _resolve_kokoro_model_files

    cache_dir = tmp_path / "kokoro_cache"
    cache_dir.mkdir()
    (cache_dir / "kokoro-v0_19.onnx").write_bytes(b"old-model")
    (cache_dir / "voices.bin").write_bytes(b"old-voices")
    downloaded: list[str] = []

    def _fake_retrieve(url: str, dest: str) -> None:
        downloaded.append(url)
        with open(dest, "wb") as handle:
            handle.write(b"new")

    with patch("plugin.audio.tts_service.os.path.expanduser", return_value=str(cache_dir)), \
         patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}, clear=False), \
         patch("urllib.request.urlretrieve", side_effect=_fake_retrieve):
        model_path, voices_path = _resolve_kokoro_model_files()

    assert model_path == str(cache_dir / "kokoro-v1.0.onnx")
    assert voices_path == str(cache_dir / "voices-v1.0.bin")
    assert downloaded == [
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin",
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.onnx",
    ]
    assert (cache_dir / "kokoro-v0_19.onnx").read_bytes() == b"old-model"
    assert (cache_dir / "voices.bin").read_bytes() == b"old-voices"


def test_resolve_kokoro_model_files_reuses_v1_cache(tmp_path):
    from plugin.audio.tts_service import _resolve_kokoro_model_files

    cache_dir = tmp_path / "kokoro_cache"
    cache_dir.mkdir()
    (cache_dir / "kokoro-v0_19.onnx").write_bytes(b"old-model")
    (cache_dir / "voices.bin").write_bytes(b"old-voices")
    (cache_dir / "kokoro-v1.0.onnx").write_bytes(b"model")
    (cache_dir / "voices-v1.0.bin").write_bytes(b"voices")

    with patch("plugin.audio.tts_service.os.path.expanduser", return_value=str(cache_dir)), \
         patch.dict("os.environ", {"KOKORO_MODEL_PATH": "", "KOKORO_VOICES_PATH": ""}, clear=False), \
         patch("urllib.request.urlretrieve") as retrieve:
        model_path, voices_path = _resolve_kokoro_model_files()

    retrieve.assert_not_called()
    assert model_path == str(cache_dir / "kokoro-v1.0.onnx")
    assert voices_path == str(cache_dir / "voices-v1.0.bin")


def test_resolve_kokoro_model_files_honors_env_overrides(tmp_path):
    from plugin.audio.tts_service import _resolve_kokoro_model_files

    model = tmp_path / "custom-model.onnx"
    voices = tmp_path / "custom-voices.bin"
    model.write_bytes(b"m")
    voices.write_bytes(b"v")

    with patch.dict(
        "os.environ",
        {"KOKORO_MODEL_PATH": str(model), "KOKORO_VOICES_PATH": str(voices)},
    ), patch("urllib.request.urlretrieve") as retrieve:
        model_path, voices_path = _resolve_kokoro_model_files()

    retrieve.assert_not_called()
    assert model_path == str(model)
    assert voices_path == str(voices)


def test_speak_kokoro_local_uses_misaki_script_for_non_english(tmp_path):
    """Non-Latin text uses the voice's Misaki lang; Latin text on that voice uses English espeak."""
    import plugin.audio.tts_service as tts
    from plugin.audio.kokoro_g2p import KOKORO_ONNX_SCRIPT
    from plugin.audio.tts_service import _speak_kokoro_local

    tts._speech_cancelled.clear()
    tts._speech_active = False

    model = tmp_path / "kokoro-v1.0.onnx"
    voices = tmp_path / "voices-v1.0.bin"
    model.write_bytes(b"m")
    voices.write_bytes(b"v")
    py = tmp_path / "python"
    py.write_text("")

    def _launch(text: str, voice: str) -> tuple[list[str], object]:
        launched: list[list[str]] = []

        def fake_popen(cmd, **kwargs):
            launched.append(list(cmd))
            with open(cmd[6], "wb") as handle:
                handle.write(b"RIFF")

            class _Proc:
                returncode = 0

                def communicate(self, input=None, timeout=None):
                    return "", ""

                def poll(self):
                    return 0

            return _Proc()

        ensure = MagicMock(return_value=True)
        with patch("plugin.audio.tts_service.get_config_str", return_value=str(tmp_path)), \
             patch("plugin.audio.tts_service.resolve_venv_python", return_value=str(py)), \
             patch("plugin.audio.tts_service._resolve_kokoro_model_files", return_value=(str(model), str(voices))), \
             patch("plugin.audio.tts_service.ensure_kokoro_misaki", ensure), \
             patch("plugin.audio.tts_service.subprocess.Popen", side_effect=fake_popen), \
             patch("plugin.audio.tts_service._play_audio_file") as play:
            _speak_kokoro_local(text, voice=voice, speed=1.0)
        assert play.called
        assert len(launched) == 1
        return launched[0], ensure

    ja_cmd, ja_ensure = _launch("こんにちは", "jf_alpha")
    assert ja_cmd[1] == "-c"
    assert ja_cmd[2] == KOKORO_ONNX_SCRIPT
    assert ja_cmd[4] == "jf_alpha"
    assert ja_cmd[9] == "ja"
    assert "is_phonemes=True" in ja_cmd[2]
    assert "JAG2P" in ja_cmd[2]
    ja_ensure.assert_called_once()
    assert ja_ensure.call_args.args[1] == "ja"

    # English on a Japanese voice keeps jf_alpha and uses English espeak, not JAG2P.
    en_on_ja_cmd, en_on_ja_ensure = _launch("hello", "jf_alpha")
    assert en_on_ja_cmd[4] == "jf_alpha"
    assert en_on_ja_cmd[9] == "en-us"
    en_on_ja_ensure.assert_not_called()

    # French voice keeps ff_siwis and uses fr-fr Misaki.
    fr_cmd, fr_ensure = _launch("Bonjour, je suis WriterAgent.", "ff_siwis")
    assert fr_cmd[4] == "ff_siwis"
    assert fr_cmd[9] == "fr-fr"
    assert "EspeakG2P(language='fr-fr')" in fr_cmd[2]
    assert fr_ensure.call_args.args[1] == "fr-fr"

    en_cmd, en_ensure = _launch("hello", "af_bella")
    assert en_cmd[9] == "en-us"
    assert en_cmd[2] == KOKORO_ONNX_SCRIPT
    en_ensure.assert_not_called()


def test_speak_kokoro_local_skips_synthesis_when_phonemizer_install_cancelled(tmp_path):
    import plugin.audio.tts_service as tts
    from plugin.audio.tts_service import _speak_kokoro_local

    tts._speech_cancelled.clear()
    tts._speech_active = False

    model = tmp_path / "model.onnx"
    voices = tmp_path / "voices.bin"
    model.write_bytes(b"m")
    voices.write_bytes(b"v")

    with patch("plugin.audio.tts_service.get_config_str", return_value=str(tmp_path)), \
         patch("plugin.audio.tts_service.resolve_venv_python", return_value=str(tmp_path / "python")), \
         patch("plugin.audio.tts_service._resolve_kokoro_model_files", return_value=(str(model), str(voices))), \
         patch("plugin.audio.tts_service.ensure_kokoro_misaki", return_value=None), \
         patch("plugin.audio.tts_service.subprocess.Popen") as popen, \
         patch("plugin.audio.tts_service._speak_system") as system:
        _speak_kokoro_local("こんにちは", voice="jf_alpha")

    popen.assert_not_called()
    system.assert_not_called()


def test_sentence_pipeline_installs_misaki_once_per_reply(tmp_path):
    """Prefetch must not probe or pip-install Misaki once per sentence."""
    import plugin.audio.tts_service as tts

    ensure = MagicMock(return_value=True)
    synthesized: list[str] = []

    def synth(sentence, provider, voice, speed, model, endpoint_url, api_key, on_status, generation):
        del provider, voice, speed, model, endpoint_url, api_key, on_status, generation
        synthesized.append(sentence)
        path = tmp_path / f"{len(synthesized)}.wav"
        path.write_bytes(b"RIFF")
        return tts._ReadyClip(str(path), sentence, 4, False)

    generation = tts._begin_utterance()
    try:
        with patch.object(tts, "ensure_kokoro_misaki", ensure), \
             patch.object(tts, "get_config_str", return_value="/venv"), \
             patch.object(tts, "resolve_venv_python", return_value="/venv/bin/python"), \
             patch.object(tts, "_synthesize_sentence_clip", side_effect=synth), \
             patch.object(tts, "_play_audio_file"), \
             patch.object(tts, "_speak_system") as system:
            tts._run_sentence_pipeline(
                ["一。", "二。", "三。"],
                "kokoro",
                "jf_alpha",
                1.0,
                "",
                "",
                "",
                None,
                generation,
            )
    finally:
        tts.stop_speech()

    assert synthesized == ["一。", "二。", "三。"]
    ensure.assert_called_once()
    assert ensure.call_args.args[0] == "/venv/bin/python"
    assert ensure.call_args.args[1] == "ja"
    system.assert_not_called()


def test_sentence_pipeline_stop_during_misaki_install_skips_os_speech():
    import plugin.audio.tts_service as tts

    generation = tts._begin_utterance()
    try:
        with patch.object(tts, "ensure_kokoro_misaki", return_value=None), \
             patch.object(tts, "get_config_str", return_value="/venv"), \
             patch.object(tts, "resolve_venv_python", return_value="/venv/bin/python"), \
             patch.object(tts, "_synthesize_sentence_clip") as synth, \
             patch.object(tts, "_speak_system") as system:
            tts._run_sentence_pipeline(
                ["こんにちは。", "次の文。"],
                "kokoro",
                "jf_alpha",
                1.0,
                "",
                "",
                "",
                None,
                generation,
            )
    finally:
        tts.stop_speech()

    synth.assert_not_called()
    system.assert_not_called()


def test_all_writeragent_locales_have_piper_model_mapping():
    from plugin.audio.tts_service import get_default_voice_for_locale, _PIPER_VOICE_MODELS
    import os

    locales_dir = os.path.join(os.path.dirname(__file__), "..", "..", "locales")
    locale_dirs = [d for d in os.listdir(locales_dir) if os.path.isdir(os.path.join(locales_dir, d))]

    for loc in locale_dirs:
        voice = get_default_voice_for_locale("piper", loc)
        assert voice in _PIPER_VOICE_MODELS, f"Locale {loc} resolved to unmapped voice {voice}"


class _FakeSentenceBI:
    """Same period/question/exclamation boundaries the grammar unit tests use."""

    def endOfSentence(self, text, pos, locale):
        import re

        match = re.search(r"[.!?]", text[pos:])
        if match:
            return pos + match.end()
        return len(text)


def test_sentences_for_speech_uses_grammar_splitter_and_keeps_short_fragments():
    """TTS must not drop the short tail that grammar threshold filtering removes."""
    from plugin.audio.tts_service import sentences_for_speech

    with patch(
        "plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale",
        return_value=(_FakeSentenceBI(), "en-US"),
    ), patch(
        "plugin.writer.locale.grammar_proofread_text.filter_sentence_spans_for_thresholds",
        side_effect=AssertionError("TTS must not apply the grammar churn filter"),
    ):
        spoken = sentences_for_speech("Hi. No", object())

    assert spoken == ["Hi.", "No"]


def test_sentences_for_speech_merges_dialogue():
    from plugin.audio.tts_service import sentences_for_speech

    with patch(
        "plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale",
        return_value=(_FakeSentenceBI(), "en-US"),
    ):
        spoken = sentences_for_speech('"Fire! Fire!" he yelled.', object())

    assert spoken == ['"Fire! Fire!" he yelled.']


def test_module_yaml_sentence_mode_defaults_on():
    import os

    import yaml

    with open(os.path.join(os.path.dirname(__file__), "..", "..", "plugin", "audio", "module.yaml"), encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle)
    field = manifest["config"]["tts_sentence_mode"]
    assert field["type"] == "boolean"
    assert field["widget"] == "checkbox"
    assert field["default"] is True


def test_module_yaml_short_answers_defaults_off():
    import os

    import yaml

    with open(os.path.join(os.path.dirname(__file__), "..", "..", "plugin", "audio", "module.yaml"), encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle)
    field = manifest["config"]["tts_short_answers"]
    assert field["type"] == "boolean"
    assert field["widget"] == "checkbox"
    assert field["default"] is False
    assert field["label"] == "Keep replies brief"
    assert "speech output (TTS) is on" in field["helper"]


def test_prefetch_keeps_synthesizing_during_playback(tmp_path):
    import threading

    from plugin.audio import tts_service as tts

    short = "Hi."
    longer = "This sentence is much longer and should already be synthesizing."
    long_started = threading.Event()
    paths = []

    def synth(sentence, provider, voice, speed, model, endpoint_url, api_key, on_status, generation):
        del provider, voice, speed, model, endpoint_url, api_key, on_status, generation
        if sentence == longer:
            long_started.set()
        path = tmp_path / f"{len(paths)}.wav"
        path.write_bytes(b"RIFF")
        paths.append(str(path))
        return tts._ReadyClip(str(path), sentence, 4, False)

    def play(path, generation=None):
        del path
        if generation is not None and not long_started.is_set():
            assert long_started.wait(2), "next sentence was not synthesizing while the short one played"

    generation = tts._begin_utterance()
    try:
        with patch.object(tts, "_synthesize_sentence_clip", side_effect=synth), patch.object(
            tts, "_play_audio_file", side_effect=play
        ):
            tts._run_sentence_pipeline(
                [short, longer],
                "kokoro",
                "af_bella",
                1.0,
                "",
                "",
                "",
                None,
                generation,
            )
    finally:
        tts.stop_speech()
    assert long_started.is_set()


def test_ready_queue_backpressure_stops_past_the_cap():
    import threading
    import time

    from plugin.audio import tts_service as tts

    # Cap 2 counts clips waiting, not the one already taken for playback.
    # Synth-then-put means one more clip can be built while put blocks, so a
    # held first sentence allows A (playing) + B,C (queued) + D (in hand).
    # E must not start until playback frees a slot.
    sentences = ["A.", "B.", "C.", "D.", "E."]
    started: list[str] = []
    release_play = threading.Event()

    def synth(sentence, provider, voice, speed, model, endpoint_url, api_key, on_status, generation):
        del provider, voice, speed, model, endpoint_url, api_key, on_status, generation
        started.append(sentence)
        return tts._ReadyClip(None, sentence, 0, True)

    def play(path, generation=None):
        del path, generation

    def speak_system(text, speed=1.0, generation=None):
        del text, speed, generation
        release_play.wait(30)

    generation = tts._begin_utterance()
    finished = threading.Event()

    def run() -> None:
        try:
            with patch.object(tts, "_synthesize_sentence_clip", side_effect=synth), patch.object(
                tts, "_play_audio_file", side_effect=play
            ), patch.object(tts, "_speak_system", side_effect=speak_system):
                tts._run_sentence_pipeline(
                    sentences,
                    "system",
                    "default",
                    1.0,
                    "",
                    "",
                    "",
                    None,
                    generation,
                    max_clips=2,
                    max_bytes=10**9,
                )
        finally:
            finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(started) < 4:
            time.sleep(0.02)
        time.sleep(0.2)
        assert started == sentences[:4]
        release_play.set()
        assert finished.wait(2)
    finally:
        release_play.set()
        tts.stop_speech()
        worker.join(2)
    assert started == sentences


def test_stop_drops_ready_clips_and_ignores_late_synth(tmp_path):
    import threading

    from plugin.audio import tts_service as tts

    played: list[str] = []
    hold_second = threading.Event()
    first_playing = threading.Event()
    late_path = tmp_path / "S.wav"

    def synth(sentence, provider, voice, speed, model, endpoint_url, api_key, on_status, generation):
        del provider, voice, speed, model, endpoint_url, api_key, on_status, generation
        if sentence.startswith("Second"):
            hold_second.wait(2)
        path = tmp_path / f"{sentence[:1]}.wav"
        path.write_bytes(b"wav")
        return tts._ReadyClip(str(path), sentence, 3, False)

    def play(path, generation=None):
        played.append(path)
        first_playing.set()
        while generation is not None and not tts._playback_blocked(generation):
            threading.Event().wait(0.01)

    generation = tts._begin_utterance()
    finished = threading.Event()

    def run() -> None:
        try:
            with patch.object(tts, "_synthesize_sentence_clip", side_effect=synth), patch.object(
                tts, "_play_audio_file", side_effect=play
            ):
                tts._run_sentence_pipeline(
                    ["First sentence.", "Second sentence."],
                    "kokoro",
                    "af_bella",
                    1.0,
                    "",
                    "",
                    "",
                    None,
                    generation,
                )
        finally:
            finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert first_playing.wait(2)
        tts.stop_speech()
        hold_second.set()
        assert finished.wait(2)
    finally:
        hold_second.set()
        tts.stop_speech()
        worker.join(2)
    assert len(played) == 1
    assert not late_path.exists()


def test_speak_text_async_passes_split_sentences_when_ctx_present():
    from plugin.audio.tts_service import speak_text_async

    captured: dict[str, list[str]] = {}

    def fake_pipeline(sentences, provider, voice, speed, model, endpoint_url, api_key, on_status, generation, **kwargs):
        del provider, voice, speed, model, endpoint_url, api_key, on_status, generation, kwargs
        captured["sentences"] = list(sentences)

    cfg = {
        "audio.tts_enabled": True,
        "audio.tts_provider": "system",
        "audio.tts_speed": 1.0,
    }
    with patch("plugin.audio.tts_service.get_config", side_effect=lambda key, default=None: cfg.get(key, default)), patch(
        "plugin.audio.tts_service.run_in_background", side_effect=lambda fn, **kwargs: fn()
    ), patch(
        "plugin.writer.locale.grammar_proofread_text.get_break_iterator_and_locale",
        return_value=(_FakeSentenceBI(), "en-US"),
    ), patch("plugin.audio.tts_service._run_sentence_pipeline", side_effect=fake_pipeline):
        speak_text_async("Hi. Not done yet", ctx=object())

    assert captured["sentences"] == ["Hi.", "Not done yet"]

