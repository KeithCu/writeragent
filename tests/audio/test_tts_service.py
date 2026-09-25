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
                mock_sys.assert_called_once_with("Testing system speech", speed=1.0)
                assert completed is True


def test_stop_speech_terminates_proc():
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with patch("plugin.audio.tts_service._active_speech_proc", mock_proc):
        assert is_speaking() is True
        stop_speech()
        mock_proc.terminate.assert_called_once()


def test_resolve_tts_voice():
    from plugin.audio.tts_service import _resolve_tts_voice

    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "nova") == "af_bella"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "alloy") == "af_bella"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "shimmer") == "af_sarah"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "echo") == "am_adam"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "af_bella") == "af_bella"
    assert _resolve_tts_voice("hexgrad/Kokoro-82M", "am_adam") == "am_adam"
    assert _resolve_tts_voice("openai/tts-1", "nova") == "nova"
    assert _resolve_tts_voice("openai/tts-1", "alloy") == "alloy"


def test_speak_endpoint_payload():
    from plugin.audio.tts_service import _speak_endpoint
    import json

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
        assert payload["voice"] == "af_bella"
        assert payload["response_format"] == "mp3"


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
    assert get_voice_family("endpoint", "openai/tts-1") == "openai"


def test_scoped_tts_voice_persistence():
    from plugin.audio.tts_service import get_scoped_tts_voice, set_scoped_tts_voice

    store = {}
    with patch("plugin.audio.tts_service.get_config", side_effect=lambda k, d=None: store.get(k, d)), \
         patch("plugin.audio.tts_service.set_config", side_effect=lambda k, v: store.__setitem__(k, v)):
        # Default for kokoro
        assert get_scoped_tts_voice("kokoro") == "af_bella"
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
        mock_kokoro.assert_called_once_with("Hello Kokoro", voice="af_bella", speed=1.1)
        mock_piper.assert_not_called()

        cfg["audio.tts_provider"] = "piper"
        cfg["audio.tts_voice_piper"] = "en_US-lessac-medium"
        speak_text_async("Hello Piper")
        mock_piper.assert_called_once_with("Hello Piper", voice="en_US-lessac-medium", speed=1.1)


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
    assert get_default_voice_for_locale("kokoro", "en_US") == "af_bella"
    assert get_default_voice_for_locale("kokoro", "de_DE") == "af_bella"


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


def test_all_writeragent_locales_have_piper_model_mapping():
    from plugin.audio.tts_service import get_default_voice_for_locale, _PIPER_VOICE_MODELS
    import os

    locales_dir = os.path.join(os.path.dirname(__file__), "..", "..", "locales")
    locale_dirs = [d for d in os.listdir(locales_dir) if os.path.isdir(os.path.join(locales_dir, d))]

    for loc in locale_dirs:
        voice = get_default_voice_for_locale("piper", loc)
        assert voice in _PIPER_VOICE_MODELS, f"Locale {loc} resolved to unmapped voice {voice}"

