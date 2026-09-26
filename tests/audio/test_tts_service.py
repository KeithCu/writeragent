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
        mock_kokoro.assert_called_once_with("Hello Kokoro", voice="af_bella", speed=1.1, on_status=None)
        mock_piper.assert_not_called()

        cfg["audio.tts_provider"] = "piper"
        cfg["audio.tts_voice_piper"] = "en_US-lessac-medium"
        speak_text_async("Hello Piper")
        mock_piper.assert_called_once_with("Hello Piper", voice="en_US-lessac-medium", speed=1.1, on_status=None)


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


def test_kokoro_g2p_lang_routes_misaki_from_text():
    """Script picks Misaki; ASCII Latin stays on English espeak even with a ja/fr voice."""
    from plugin.audio.tts_service import kokoro_g2p_lang

    english = "Hello, I'm your LibreOffice WriterAgent."
    # English-looking text keeps the voice but does not enter ja/fr/es Misaki.
    assert kokoro_g2p_lang(english, "jf_alpha", "ja_JP") == "en-us"
    assert kokoro_g2p_lang(english, "ff_siwis", "fr_FR") == "en-us"
    assert kokoro_g2p_lang(english, "ef_dora", "es_ES") == "en-us"
    assert kokoro_g2p_lang("hello", "jf_alpha", "en_US") == "en-us"
    assert kokoro_g2p_lang(english, "bf_emma", "en_US") == "en-gb"
    assert kokoro_g2p_lang(english, "af_bella", "en_GB") == "en-gb"
    # The translated Test sample (and any chat line) follows the text's script.
    assert kokoro_g2p_lang("こんにちは、WriterAgent です。", "af_bella", "en_US") == "ja"
    assert kokoro_g2p_lang("こんにちは", "jf_alpha", "ja_JP") == "ja"
    assert kokoro_g2p_lang("非常に強力", "jf_alpha", "ja") == "ja"
    assert kokoro_g2p_lang("強力", "af_bella", "ja") == "ja"
    assert kokoro_g2p_lang("強力", "af_bella", "zh_CN") == "zh"
    assert kokoro_g2p_lang("你好", "zf_xiaobei", "zh_CN") == "zh"
    assert kokoro_g2p_lang("你好", "af_bella", "de_DE") == "zh"
    assert kokoro_g2p_lang("नमस्ते", "hf_alpha", "hi_IN") == "hi"
    # Accented Latin uses the voice's Misaki lang, else the UI locale.
    assert kokoro_g2p_lang("Café au lait", "ff_siwis", "en_US") == "fr-fr"
    assert kokoro_g2p_lang("niño", "ef_dora", "en_US") == "es"
    assert kokoro_g2p_lang("cão", "pf_dora", "en_US") == "pt-br"
    assert kokoro_g2p_lang("città", "af_bella", "it_IT") == "it"
    # ASCII French is not distinguished from English without langdetect.
    assert kokoro_g2p_lang("Bonjour, je suis WriterAgent.", "ff_siwis", "fr_FR") == "en-us"


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
        mock_kokoro.assert_called_once_with(
            "Bonjour", voice="jf_alpha", speed=1.25, on_status=None,
        )


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
    assert [item["value"] for item in _KOKORO_CATALOG_ITEMS] == [row["id"] for row in raw["kokoro"]["voices"]]
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
    """Japanese/accented text passes the Misaki script; ASCII English on those voices does not."""
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

    fr_cmd, fr_ensure = _launch("Café", "ff_siwis")
    assert fr_cmd[4] == "ff_siwis"
    assert fr_cmd[9] == "fr-fr"
    assert "EspeakG2P(language='fr-fr')" in fr_cmd[2]
    assert fr_ensure.call_args.args[1] == "fr-fr"

    es_cmd, es_ensure = _launch("niño", "ef_dora")
    assert es_cmd[4] == "ef_dora"
    assert es_cmd[9] == "es"
    assert "EspeakG2P(language='es')" in es_cmd[2]
    assert es_ensure.call_args.args[1] == "es"

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


def test_all_writeragent_locales_have_piper_model_mapping():
    from plugin.audio.tts_service import get_default_voice_for_locale, _PIPER_VOICE_MODELS
    import os

    locales_dir = os.path.join(os.path.dirname(__file__), "..", "..", "locales")
    locale_dirs = [d for d in os.listdir(locales_dir) if os.path.isdir(os.path.join(locales_dir, d))]

    for loc in locale_dirs:
        voice = get_default_voice_for_locale("piper", loc)
        assert voice in _PIPER_VOICE_MODELS, f"Locale {loc} resolved to unmapped voice {voice}"

