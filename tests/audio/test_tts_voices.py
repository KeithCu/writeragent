# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for TTS voice catalog, scoped voice resolution, and settings."""

from unittest.mock import patch

from plugin.audio.tts_voices import (
    TTS_TEST_SAMPLE,
    _kokoro_lang_for_voice,
    _preferred_harvested_voice,
    _resolve_tts_voice,
    clean_provider_name,
    clean_voice_name,
    get_default_voice_for_locale,
    get_scoped_tts_voice,
    get_voice_catalog,
    get_voice_family,
    kokoro_g2p_lang,
    parse_tts_speed,
    set_scoped_tts_voice,
    settings_voice_options,
    tts_test_sample,
    voice_choice_to_id,
    voice_options_for_provider,
)
from plugin.audio.voice_catalog import (
    CATALOG_PATH,
    KOKORO_CATALOG_ITEMS as _KOKORO_CATALOG_ITEMS,
    PIPER_VOICE_MODELS as _PIPER_VOICE_MODELS,
    VOICE_CATALOGS,
    catalog_voice_display_label,
    voice_short_name,
)
from plugin.framework.client import model_fetcher as cfg


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


def test_clean_provider_and_voice_names():
    assert clean_provider_name("Kokoro (Local Neural, ONNX CPU)") == "kokoro"
    assert clean_provider_name("Piper (Local Fast Neural, CPU)") == "piper"
    assert clean_provider_name("LLM Endpoint") == "endpoint"
    assert clean_provider_name("Current Chat Endpoint (/audio/speech)") == "endpoint"
    assert clean_provider_name("OS Native (say / SAPI / spd-say)") == "system"

    assert clean_voice_name("af_bella (Kokoro US Female - Bella)") == "af_bella"
    assert clean_voice_name("en_US-lessac-medium (Piper US Female - Lessac)") == "en_US-lessac-medium"
    assert clean_voice_name("alloy (OpenAI Neutral)") == "alloy"
    assert clean_voice_name("alloy") == "alloy"


def test_clean_voice_name_parenthesized_path():
    """clean_voice_name must preserve Windows/Unix paths containing parentheses."""
    win_path = r"C:\Program Files (x86)\Piper\voices\en_US-lessac-medium.onnx"
    assert clean_voice_name(win_path) == win_path
    unix_path = "/opt/tts (voices)/piper/en_US-lessac-medium.onnx"
    assert clean_voice_name(unix_path) == unix_path
    labeled_path = r"C:\Program Files (x86)\Piper\model.onnx (Custom Voice)"
    assert clean_voice_name(labeled_path) == r"C:\Program Files (x86)\Piper\model.onnx"


def test_get_voice_family():
    assert get_voice_family("kokoro") == "kokoro"
    assert get_voice_family("piper") == "piper"
    assert get_voice_family("system") == "system"
    assert get_voice_family("endpoint", "hexgrad/Kokoro-82M") == "kokoro"
    with patch("plugin.framework.config.get_current_endpoint", return_value="https://api.openai.com"):
        assert get_voice_family("endpoint", "openai/tts-1") == "openai"
    with patch("plugin.framework.config.get_current_endpoint", return_value="https://api.together.xyz"):
        assert get_voice_family("endpoint", "cartesia/sonic") == "together"
        assert get_voice_family("endpoint", "hexgrad/Kokoro-82M") == "kokoro"


def test_scoped_voice_family_validation():
    """get_scoped_tts_voice validates that a voice belongs to openrouter/together family."""
    store = {
        "audio.tts_voice_openrouter": "af_sky",  # Kokoro voice, invalid for openrouter
    }
    with patch("plugin.framework.config.get_config", side_effect=lambda k, d=None: store.get(k, d)):
        with patch("plugin.framework.config.get_current_endpoint", return_value="https://openrouter.ai/api"):
            cfg._tts_supported_voices["openai/tts-1"] = ["alloy", "echo", "nova"]
            try:
                # Should fall back to valid voice from supported voices or family default
                voice = get_scoped_tts_voice("endpoint", "openai/tts-1")
                assert voice in ("alloy", "echo", "nova")
            finally:
                cfg._tts_supported_voices.pop("openai/tts-1", None)


def test_together_voice_options_read_the_cache_and_do_not_fetch():
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
        assert [opt["value"] for opt in options] == ["leah", "tara"]
        assert options[0]["label"] == "leah"

        with patch("plugin.framework.client.requests.sync_request") as mock_sync:
            cartesia = voice_options_for_provider(
                "endpoint",
                "cartesia/sonic",
                endpoint="https://api.together.xyz",
                api_key="sk-test",
            )
            mock_sync.assert_not_called()
        assert cartesia == []

        with patch("plugin.framework.client.requests.sync_request") as mock_sync:
            local = voice_options_for_provider("kokoro", "hexgrad/Kokoro-82M")
            mock_sync.assert_not_called()
        assert any(opt["value"] == "af_bella" for opt in local)
        assert any(opt["label"] == "US Female - Bella" for opt in local)
    finally:
        cfg._tts_supported_voices.clear()
        cfg._together_voices_fetch_cache.clear()


def test_scoped_tts_voice_persistence():
    store = {}
    with patch("plugin.framework.config.get_config", side_effect=lambda k, d=None: store.get(k, d)), \
         patch("plugin.framework.config.set_config", side_effect=lambda k, v: store.__setitem__(k, v)):
        assert get_scoped_tts_voice("kokoro") == "af_sky"
        set_scoped_tts_voice("af_sarah", "kokoro")
        assert get_scoped_tts_voice("kokoro") == "af_sarah"
        assert store.get("audio.tts_voice_kokoro") == "af_sarah"
        assert store.get("audio.tts_voice") == "af_sarah"

        assert get_scoped_tts_voice("piper") == "en_US-lessac-medium"
        set_scoped_tts_voice("en_US-amy-medium", "piper")
        assert get_scoped_tts_voice("piper") == "en_US-amy-medium"
        assert store.get("audio.tts_voice_piper") == "en_US-amy-medium"

        assert get_scoped_tts_voice("kokoro") == "af_sarah"


def test_parse_tts_speed():
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
    assert parse_tts_speed("0.1") == 0.25
    assert parse_tts_speed("0") == 0.25
    assert parse_tts_speed(-2) == 0.25
    assert parse_tts_speed("abc") == 1.0
    assert parse_tts_speed(None) == 1.0
    assert parse_tts_speed("") == 1.0


def test_get_default_voice_for_locale():
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

    for loc, voice_id in (
        ("fr_FR", "fr_FR-siwis-medium"),
        ("it_IT", "it_IT-paola-medium"),
        ("en_US", "en_US-lessac-medium"),
        ("nl_NL", "nl_NL-mls-medium"),
    ):
        assert get_default_voice_for_locale("piper", loc) == voice_id
        assert "female" in _PIPER_VOICE_MODELS[voice_id][3].casefold()

    assert get_default_voice_for_locale("kokoro", "es_ES") == "ef_dora"
    assert get_default_voice_for_locale("kokoro", "fr_FR") == "ff_siwis"
    assert get_default_voice_for_locale("kokoro", "it_IT") == "if_sara"
    assert get_default_voice_for_locale("kokoro", "ja_JP") == "jf_alpha"
    assert get_default_voice_for_locale("kokoro", "zh_CN") == "zf_xiaobei"
    assert get_default_voice_for_locale("kokoro", "hi_IN") == "hf_alpha"
    assert get_default_voice_for_locale("kokoro", "pt_BR") == "pf_dora"
    assert get_default_voice_for_locale("kokoro", "en_US") == "af_sky"
    assert get_default_voice_for_locale("kokoro", "de_DE") == "af_sky"


def test_piper_default_picks_female_ahead_of_an_earlier_male():
    male = ("x.onnx", "x.onnx.json", "xx_XX", "xx_XX-bob-medium (Test Male - Bob)")
    female = ("x.onnx", "x.onnx.json", "xx_XX", "xx_XX-ann-medium (Test Female - Ann)")
    only_male = ("y.onnx", "y.onnx.json", "yy_YY", "yy_YY-carl-medium (Test Male - Carl)")
    with patch.dict(
        _PIPER_VOICE_MODELS,
        {"xx_XX-bob-medium": male, "xx_XX-ann-medium": female, "yy_YY-carl-medium": only_male},
        clear=True,
    ):
        assert get_default_voice_for_locale("piper", "xx_XX") == "xx_XX-ann-medium"
        assert get_default_voice_for_locale("piper", "yy") == "yy_YY-carl-medium"
        assert get_default_voice_for_locale("piper", "zz_ZZ") == "en_US-lessac-medium"


def test_kokoro_gap_language_uses_first_female():
    gap = list(_KOKORO_CATALOG_ITEMS) + [
        {"value": "sm_bob", "label": "sm_bob (Kokoro Swedish Male - Bob)", "lang": "sv"},
        {"value": "sf_anna", "label": "sf_anna (Kokoro Swedish Female - Anna)", "lang": "sv"},
    ]
    male_only = list(_KOKORO_CATALOG_ITEMS) + [
        {"value": "sm_bob", "label": "sm_bob (Kokoro Swedish Male - Bob)", "lang": "sv"},
    ]
    with patch("plugin.audio.tts_voices._KOKORO_CATALOG_ITEMS", gap):
        assert get_default_voice_for_locale("kokoro", "sv_SE") == "sf_anna"
        assert get_default_voice_for_locale("kokoro", "en_US") == "af_sky"
        assert get_default_voice_for_locale("kokoro", "es_ES") == "ef_dora"
    with patch("plugin.audio.tts_voices._KOKORO_CATALOG_ITEMS", male_only):
        assert get_default_voice_for_locale("kokoro", "sv") == "sm_bob"


def test_saved_scoped_voice_beats_locale_default():
    empty: dict[str, str] = {}
    with patch("plugin.framework.config.get_config", side_effect=lambda key, default=None: empty.get(key, default)):
        assert get_scoped_tts_voice("piper", locale="fr_FR") == "fr_FR-siwis-medium"

    store = {
        "audio.tts_voice_piper": "de_DE-thorsten-medium",
        "audio.tts_voice": "alloy",
    }
    with patch("plugin.framework.config.get_config", side_effect=lambda key, default=None: store.get(key, default)):
        assert get_scoped_tts_voice("piper", locale="fr_FR") == "de_DE-thorsten-medium"

    store = {
        "audio.tts_voice_kokoro": "af_sarah",
        "audio.tts_voice": "alloy",
    }
    with patch("plugin.framework.config.get_config", side_effect=lambda key, default=None: store.get(key, default)):
        assert get_scoped_tts_voice("kokoro", locale="es_ES") == "af_sarah"


def test_get_voice_catalog_locale_prioritized():
    de_catalog = get_voice_catalog("piper", "de_DE")
    assert de_catalog[0]["value"] == "de_DE-thorsten-medium"
    assert any("de_DE-thorsten_emotional-medium" == v["value"] for v in de_catalog[:3])

    fr_catalog = get_voice_catalog("piper", "fr_FR")
    assert fr_catalog[0]["value"] == "fr_FR-siwis-medium"

    es_kokoro = get_voice_catalog("kokoro", "es_ES")
    assert es_kokoro[0]["value"] == "ef_dora"

    en_catalog = get_voice_catalog("piper", "en_US")
    assert en_catalog[0]["value"] == "en_US-lessac-medium"


def test_piper_and_kokoro_voice_list_shows_parenthetical_only():
    assert catalog_voice_display_label(
        "en_US-lessac-medium (US English Female - Lessac)"
    ) == "US English Female - Lessac"
    assert catalog_voice_display_label(
        "af_heart (Kokoro US Female - Heart)"
    ) == "US Female - Heart"
    assert catalog_voice_display_label("af_heart (US Female - Heart)") == "US Female - Heart"
    assert catalog_voice_display_label("Kore") == "Kore"
    assert catalog_voice_display_label("leah") == "leah"

    kokoro = get_voice_catalog("kokoro", "en_US")
    kokoro_by = {row["value"]: row["label"] for row in kokoro}
    assert kokoro_by["af_heart"] == "US Female - Heart"
    assert "af_heart" not in kokoro_by["af_heart"]
    assert "(" not in kokoro_by["af_heart"]
    assert voice_choice_to_id("US Female - Heart", kokoro) == "af_heart"
    assert voice_choice_to_id("af_heart (Kokoro US Female - Heart)", kokoro) == "af_heart"
    assert voice_choice_to_id("af_heart", kokoro) == "af_heart"

    piper = get_voice_catalog("piper", "en_US")
    piper_by = {row["value"]: row["label"] for row in piper}
    assert piper_by["en_US-lessac-medium"] == "US English Female - Lessac"
    assert "en_US-lessac-medium" not in piper_by["en_US-lessac-medium"]
    assert voice_choice_to_id("US English Female - Lessac", piper) == "en_US-lessac-medium"
    assert voice_choice_to_id(
        "en_US-lessac-medium (US English Female - Lessac)", piper,
    ) == "en_US-lessac-medium"

    shared = "French Female - Siwis"
    assert voice_choice_to_id(shared, kokoro) == "ff_siwis"
    assert voice_choice_to_id(shared, piper) == "fr_FR-siwis-medium"

    with patch("plugin.framework.i18n._", side_effect=lambda text: {"US Female - Heart": "Amerikaans vrouwelijk - Heart"}.get(text, text)):
        assert voice_choice_to_id("Amerikaans vrouwelijk - Heart", kokoro) == "af_heart"

    assert len({row["label"] for row in kokoro}) == len(kokoro)
    assert len({row["label"] for row in piper}) == len(piper)
    for row in kokoro + piper:
        assert row["value"]
        assert "(" not in row["label"]
        assert not row["label"].casefold().startswith("kokoro ")

    openai = get_voice_catalog("openai")
    assert any(row["label"] == "alloy (OpenAI Neutral)" for row in openai)
    assert any(row["value"] == "alloy" for row in openai)


def test_kokoro_g2p_lang_english_on_non_english_voice():
    english = "Hello, I'm your LibreOffice WriterAgent."
    assert kokoro_g2p_lang(english, "jf_alpha") == "en-us"
    assert kokoro_g2p_lang(english, "zf_xiaobei") == "en-us"
    assert kokoro_g2p_lang(english, "hf_alpha") == "en-us"
    assert kokoro_g2p_lang("hello", "af_bella") == "en-us"
    assert kokoro_g2p_lang(english, "bf_emma") == "en-gb"
    assert kokoro_g2p_lang("Café au lait", "ff_siwis") == "fr-fr"
    assert kokoro_g2p_lang("Bonjour, je suis WriterAgent.", "ff_siwis") == "fr-fr"
    assert kokoro_g2p_lang("Hola, ¿cómo estás?", "ef_dora") == "es"
    assert kokoro_g2p_lang("Ciao, come stai?", "if_sara") == "it"
    assert kokoro_g2p_lang("Olá, tudo bem?", "pf_dora") == "pt-br"
    assert kokoro_g2p_lang("こんにちは、WriterAgent です。", "jf_alpha") == "ja"
    assert kokoro_g2p_lang("你好", "zf_xiaobei") == "zh"
    assert kokoro_g2p_lang("नमस्ते", "hf_alpha") == "hi"


def test_tts_test_sample_uses_gettext():
    with patch("plugin.audio.tts_voices._", return_value="Bonjour, je suis WriterAgent.") as mock_gettext:
        assert tts_test_sample() == "Bonjour, je suis WriterAgent."
        mock_gettext.assert_called_once_with(TTS_TEST_SAMPLE)


def test_kokoro_lang_for_voice():
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


def test_module_yaml_voice_options_use_catalog_provider():
    import os
    import yaml

    with open(os.path.join(os.path.dirname(__file__), "..", "..", "plugin", "audio", "module.yaml"), encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle)
    voice = manifest["config"]["tts_voice"]
    assert voice["options_provider"] == "plugin.audio.tts_voices:settings_voice_options"
    assert len(voice["options"]) <= 4


def test_voice_catalog_asset_drives_runtime_maps():
    import json
    import os

    assert os.path.isfile(CATALOG_PATH)
    with open(CATALOG_PATH, encoding="utf-8") as handle:
        raw = json.load(handle)

    piper_ids = [row["id"] for row in raw["piper"]["voices"]]
    assert list(_PIPER_VOICE_MODELS) == piper_ids
    assert [item["value"] for item in VOICE_CATALOGS["piper"]] == piper_ids
    kokoro_ids = [row["id"] for row in raw["kokoro"]["voices"]]
    assert [item["value"] for item in _KOKORO_CATALOG_ITEMS] == kokoro_ids
    assert "af_sky" in kokoro_ids
    assert "af_bella" in kokoro_ids
    assert raw["kokoro"]["fallback_voice"] == "af_sky"
    assert voice_short_name("de_DE-thorsten-medium") == "Thorsten"
    assert voice_short_name("en_US-lessac-medium") == "Lessac"
    onnx, config, lang, _label = _PIPER_VOICE_MODELS["de_DE-thorsten-medium"]
    assert onnx.endswith("de_DE-thorsten-medium.onnx")
    assert config.endswith(".onnx.json")
    assert lang == "de_DE"


def test_settings_voice_options_match_catalog_for_provider():
    def _cfg(key, default=None):
        values = {
            "audio.tts_provider": "piper",
            "audio.tts_model": "",
        }
        return values.get(key, default)

    with patch("plugin.framework.config.get_config", side_effect=_cfg), \
         patch("plugin.framework.i18n.get_active_locale", return_value="de_DE"):
        options = settings_voice_options(None)

    assert options == get_voice_catalog("piper", "de_DE")
    assert options[0]["value"] == "de_DE-thorsten-medium"
    assert any(opt["value"] == "fr_FR-siwis-medium" for opt in options)
    assert all(opt["value"] != "af_bella" for opt in options)


def test_endpoint_voice_options_follow_cached_openrouter_voices():
    gemini = "google/gemini-2.5-flash-preview-tts"
    grok = "x-ai/grok-voice-tts-1.0"
    cfg._tts_supported_voices.pop(gemini, None)
    cfg._tts_supported_voices.pop(grok, None)
    cfg._tts_supported_voices.pop("hexgrad/Kokoro-82M", None)
    cfg._tts_supported_voices.pop("hexgrad/kokoro-82m", None)
    cfg._model_fetch_tts_cache.clear()
    try:
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

        with patch("plugin.framework.config.get_config", side_effect=_cfg):
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
        with patch("plugin.framework.config.get_current_endpoint", return_value="https://api.together.xyz"), \
             patch("plugin.framework.client.requests.sync_request") as mock_sync:
            rows = voice_options_for_provider("endpoint", "openai/tts-1")
            mock_sync.assert_not_called()
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
            with patch("plugin.framework.config.get_config", side_effect=lambda key, default=None: store.get(key, default)):
                return get_scoped_tts_voice("endpoint", model)

        missing = {"audio.tts_voice_openrouter": "alloy", "audio.tts_voice": "alloy"}
        assert _run(gemini, missing) == "Aoede"
        assert _run(grok, dict(missing)) == "Achernar"

        assert _run(gemini, {"audio.tts_voice_openrouter": "Aoede", "audio.tts_voice": "alloy"}) == "Aoede"
        assert _run(grok, {"audio.tts_voice_openrouter": "Aoede", "audio.tts_voice": "alloy"}) == "Aoede"
        assert _run(gemini, {"audio.tts_voice_openrouter": "Zephyr", "audio.tts_voice": "alloy"}) == "Zephyr"
        assert _run(gemini, {"audio.tts_voice_openrouter": "alloy", "audio.tts_voice": "Zephyr"}) == "Zephyr"

        cfg._tts_supported_voices[gemini] = ["Zephyr", "Puck", "Kore"]
        assert _run(gemini, missing) == "Kore"

        cfg._tts_supported_voices[folded] = ["Puck", "aoede"]
        assert _run(folded, {"audio.tts_voice_openrouter": "alloy", "audio.tts_voice": ""}) == "aoede"
    finally:
        cfg._tts_supported_voices.pop(gemini, None)
        cfg._tts_supported_voices.pop(grok, None)
        cfg._tts_supported_voices.pop(folded, None)
