"""Tests for DEFAULT_MODELS and get_provider_defaults."""


from plugin.framework.default_models import get_provider_defaults, resolve_model_id


class TestGetProviderDefaults:
    def test_together_serverless_audio_catalog_merges_known_families(self):
        from plugin.framework.default_models import together_speech_ids

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

    def test_gpt_oss_20b_not_stray_on_openrouter(self):
        # Same slug as Together/Groq. Without it, an OpenRouter sidebar treats
        # openai/gpt-oss-20b as a stray id and restashes text_model to 120b:nitro.
        from plugin.chatbot.config_ui_helpers import _is_incompatible_model_for_provider

        assert not _is_incompatible_model_for_provider("openai/gpt-oss-20b", "openrouter")

    def test_writeragent_mock_not_custom_or_openai_default(self):
        from plugin.framework.default_models import DEFAULT_MODELS

        row = next((m for m in DEFAULT_MODELS if m.get("ids", {}).get("mock") == "writeragent-mock"), None)
        assert (row) is not None
        assert not (row.get("default_text"))
        # Must not become the custom-endpoint Settings default.
        assert (get_provider_defaults("custom").get("text_model")) is None
        assert (resolve_model_id(row, "openai")) is None
        assert (resolve_model_id(row, "custom")) is None

    def test_chat_audio_rows_are_not_speech_fallbacks(self):
        # CHAT|AUDIO with neither stt nor tts flag must not become the speech default.
        google = get_provider_defaults("google")
        assert google.get("text_model")
        assert google.get("stt_model") is None
        assert google.get("tts_model") is None
        mock = get_provider_defaults("mock")
        assert mock.get("text_model")
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

    def test_get_model_capability(self):
        from plugin.framework.default_models import get_model_capability
        from plugin.framework.constants import ModelCapability

        caps = get_model_capability("openai/gpt-oss-120b:nitro", "https://openrouter.ai/api")
        assert isinstance(caps, int) and (caps & ModelCapability.TOOLS)
        assert get_model_capability("deepseek-chat", "https://api.deepseek.com") & ModelCapability.CHAT
        assert get_model_capability("nonexistent-model", "https://openrouter.ai/api") == ModelCapability.NONE


