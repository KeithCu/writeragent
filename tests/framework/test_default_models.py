"""Tests for DEFAULT_MODELS and get_provider_defaults."""


from plugin.framework.default_models import get_provider_defaults, resolve_model_id
import pytest


class TestGetProviderDefaults:
    def test_together_defaults_match_catalog(self):
        d = get_provider_defaults("together")
        assert (bool(d.get("text_model")))
        assert (bool(d.get("image_model")))
        assert (bool(d.get("stt_model")))
        assert (bool(d.get("tts_model")))

    @pytest.mark.parametrize(
        "value, value_2, expected",
        [
            pytest.param("openrouter", "tts_model", "hexgrad/Kokoro-82M", id="test_openrouter_default_tts_model_uses_kokoro"),
            pytest.param("together", "tts_model", "hexgrad/Kokoro-82M", id="test_together_default_tts_model_uses_kokoro"),
            pytest.param("openai", "tts_model", "tts-1", id="test_openai_default_tts_model_uses_tts_1"),
            pytest.param("groq", "text_model", "openai/gpt-oss-120b", id="test_groq_default_text_model_uses_gpt_oss_120b"),
            pytest.param("openrouter", "text_model", "openai/gpt-oss-120b:nitro", id="test_openrouter_default_text_model_uses_nitro"),
            pytest.param("openrouter", "stt_model", "mistralai/voxtral-mini-transcribe", id="test_openrouter_default_stt_model_uses_voxtral"),
            pytest.param("openrouter", "image_model", "google/gemini-3.1-flash-lite-image", id="test_openrouter_default_image_model"),
        ],
    )
    def test_openrouter_default_tts_model_uses_kokoro(self, value, value_2, expected):
        d = get_provider_defaults(value)
        assert (d.get(value_2)) == (expected)


    def test_together_serverless_audio_catalog(self):
        from plugin.framework.default_models import catalog_speech_ids, together_speech_ids

        tts = catalog_speech_ids("together", "tts")
        assert tts == [
            "hexgrad/Kokoro-82M",
            "cartesia/sonic",
            "cartesia/sonic-2",
            "cartesia/sonic-3",
            "canopylabs/orpheus-3b-0.1-ft",
        ]
        stt = catalog_speech_ids("together", "stt")
        assert stt == [
            "nvidia/parakeet-tdt-0.6b-v3",
            "openai/whisper-large-v3",
            "nvidia/nemotron-3-asr-streaming-0.6b",
            "nvidia/nemotron-3.5-asr-streaming-0.6b",
        ]
        # Defaults stay Kokoro and Parakeet; the rest are selectable, not the default.
        d = get_provider_defaults("together")
        assert d.get("tts_model") == "hexgrad/Kokoro-82M"
        assert d.get("stt_model") == "nvidia/parakeet-tdt-0.6b-v3"
        # Raw /v1/models chat rows are ignored; a new id in a known family is kept.
        merged = together_speech_ids(
            "tts",
            ["openai/gpt-oss-120b", "cartesia/sonic-4", "hexgrad/Kokoro-82M"],
        )
        assert "openai/gpt-oss-120b" not in merged
        assert merged[-1] == "cartesia/sonic-4"
        assert merged.count("hexgrad/Kokoro-82M") == 1


    def test_resolve_model_id_requires_ids_dict(self):
        assert (resolve_model_id({"": 0, "ids": ""}, 0)) is None
        assert (resolve_model_id({"ids": None}, "openrouter")) is None
        assert (resolve_model_id({"ids": {"openrouter": "x"}}, "openrouter")) == ("x")
        assert (resolve_model_id({"ids": {"release": ()}}, "release")) is None

    def test_minimax_m3_together_catalog(self):
        from plugin.framework.default_models import DEFAULT_MODELS

        mm = next((m for m in DEFAULT_MODELS if m["display_name"] == "MiniMax M3"), None)
        assert (mm) is not None
        assert (mm["ids"]["together"]) == ("MiniMaxAI/MiniMax-M3")
        assert (mm["context_length"]) == (1000000)

    def test_gpt_oss_20b_openrouter_id(self):
        # Same slug as Together/Groq. Without it, an OpenRouter sidebar treats
        # openai/gpt-oss-20b as a stray id and restashes text_model to 120b:nitro.
        from plugin.framework.default_models import DEFAULT_MODELS

        oss20 = next((m for m in DEFAULT_MODELS if m.get("display_name") == "GPT-OSS 20B"), None)
        assert (oss20) is not None
        assert (oss20["ids"].get("openrouter")) == ("openai/gpt-oss-20b")
        assert (oss20["ids"].get("together")) == ("openai/gpt-oss-20b")
        assert (oss20["ids"].get("groq")) == ("openai/gpt-oss-20b")

    def test_together_default_image_model_is_flux2_dev(self):
        from plugin.framework.default_models import DEFAULT_MODELS
        from plugin.framework.constants import ModelCapability

        d = get_provider_defaults("together")
        assert (d.get("image_model")) == ("black-forest-labs/FLUX.2-dev")

        flux = next((m for m in DEFAULT_MODELS if m.get("display_name") == "FLUX.2 [dev]"), None)
        assert (flux) is not None
        assert (flux["ids"].get("together")) == ("black-forest-labs/FLUX.2-dev")
        assert (flux.get("default_image"))
        assert (bool(flux["capability"] & ModelCapability.IMAGE))

        flash = next((m for m in DEFAULT_MODELS if m.get("ids", {}).get("together") == "google/flash-image-2.5"), None)
        assert (flash) is not None
        assert not (flash.get("default_image"))
        assert (bool(flash["capability"] & ModelCapability.IMAGE))

    def test_openrouter_free_model_catalog(self):
        from plugin.framework.default_models import DEFAULT_MODELS
        from plugin.framework.constants import ModelCapability

        free_m = next((m for m in DEFAULT_MODELS if m.get("ids", {}).get("openrouter") == "openrouter/free"), None)
        assert (free_m) is not None
        assert (free_m["display_name"]) == ("Free Models (Auto)")
        caps = free_m["capability"]
        assert (bool(caps & ModelCapability.CHAT))
        assert (bool(caps & ModelCapability.TOOLS))
        assert (bool(caps & ModelCapability.VISION))
        assert (free_m["context_length"]) == (200000)

    def test_gemini_31_pro_catalog(self):
        from plugin.framework.default_models import DEFAULT_MODELS
        from plugin.framework.constants import ModelCapability

        pro = next((m for m in DEFAULT_MODELS if m.get("display_name") == "Gemini 3.1 Pro"), None)
        assert (pro) is not None
        assert (pro["ids"].get("google")) == ("gemini-3.1-pro")
        assert (pro["ids"].get("openrouter")) == ("google/gemini-3.1-pro")
        caps = pro["capability"]
        assert (bool(caps & ModelCapability.CHAT))
        assert (bool(caps & ModelCapability.TOOLS))
        assert (bool(caps & ModelCapability.VISION))
        assert (bool(caps & ModelCapability.AUDIO))

    def test_writeragent_mock_catalog_window(self):
        from plugin.framework.default_models import DEFAULT_MODELS
        from plugin.framework.constants import ModelCapability

        row = next((m for m in DEFAULT_MODELS if m.get("ids", {}).get("mock") == "writeragent-mock"), None)
        assert (row) is not None
        assert (row["context_length"]) == (32768)
        assert not (row.get("default_text"))
        assert (bool(row["capability"] & ModelCapability.CHAT))
        # Must not become the custom-endpoint Settings default.
        assert (get_provider_defaults("custom").get("text_model")) is None
        assert (resolve_model_id(row, "openai")) is None
        assert (resolve_model_id(row, "custom")) is None

    def test_chat_audio_rows_are_not_speech_fallbacks(self):
        # CHAT|AUDIO with neither stt nor tts flag must not become the speech default.
        google = get_provider_defaults("google")
        assert google.get("text_model") == "gemini-3.1-flash-lite-preview"
        assert google.get("stt_model") is None
        assert google.get("tts_model") is None
        mock = get_provider_defaults("mock")
        assert mock.get("text_model") == "writeragent-mock"
        assert mock.get("stt_model") is None
        assert mock.get("tts_model") is None

    def test_together_nemotron_stt_prefix_requires_asr(self):
        from plugin.framework.default_models import together_speech_ids

        merged = together_speech_ids(
            "stt",
            [
                "nvidia/nemotron-nano",
                "nvidia/nemotron-9-asr-test",
                "openai/whisper-medium",
                "nvidia/parakeet-extra",
            ],
        )
        assert "nvidia/nemotron-nano" not in merged
        assert "nvidia/nemotron-9-asr-test" in merged
        assert "openai/whisper-medium" in merged
        assert "nvidia/parakeet-extra" in merged

    def test_together_deepseek_v4_flash_catalog(self):
        from plugin.framework.default_models import DEFAULT_MODELS
        from plugin.framework.constants import ModelCapability

        v4 = next((m for m in DEFAULT_MODELS if m.get("display_name") == "DeepSeek V4 Flash"), None)
        assert (v4) is not None
        assert (v4["ids"].get("together")) == ("deepseek-ai/DeepSeek-V4-Flash-0731")
        caps = v4["capability"]
        assert (bool(caps & ModelCapability.CHAT))
        assert (bool(caps & ModelCapability.TOOLS))


