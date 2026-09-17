"""Tests for DEFAULT_MODELS and get_provider_defaults."""

import unittest

from plugin.framework.default_models import get_provider_defaults, resolve_model_id


class TestGetProviderDefaults(unittest.TestCase):
    def test_together_defaults_match_catalog(self):
        d = get_provider_defaults("together")
        self.assertTrue(bool(d.get("text_model")))
        self.assertTrue(bool(d.get("image_model")))
        self.assertTrue(bool(d.get("stt_model")))

    def test_resolve_model_id_requires_ids_dict(self):
        self.assertIsNone(resolve_model_id({"": 0, "ids": ""}, 0))
        self.assertIsNone(resolve_model_id({"ids": None}, "openrouter"))
        self.assertEqual(resolve_model_id({"ids": {"openrouter": "x"}}, "openrouter"), "x")
        self.assertIsNone(resolve_model_id({"ids": {"release": ()}}, "release"))

    def test_minimax_m3_together_catalog(self):
        from plugin.framework.default_models import DEFAULT_MODELS

        mm = next((m for m in DEFAULT_MODELS if m["display_name"] == "MiniMax M3"), None)
        self.assertIsNotNone(mm)
        self.assertEqual(mm["ids"]["together"], "MiniMaxAI/MiniMax-M3")
        self.assertEqual(mm["context_length"], 1000000)

    def test_groq_default_text_model_uses_gpt_oss_120b(self):
        d = get_provider_defaults("groq")
        self.assertEqual(d.get("text_model"), "openai/gpt-oss-120b")

    def test_openrouter_default_text_model_uses_nitro(self):
        d = get_provider_defaults("openrouter")
        self.assertEqual(d.get("text_model"), "openai/gpt-oss-120b:nitro")

    def test_openrouter_default_stt_model_uses_voxtral(self):
        d = get_provider_defaults("openrouter")
        self.assertEqual(d.get("stt_model"), "mistralai/voxtral-mini-transcribe")

    def test_openrouter_default_image_model(self):
        d = get_provider_defaults("openrouter")
        self.assertEqual(d.get("image_model"), "google/gemini-3.1-flash-lite-image")

    def test_together_default_image_model_is_flux2_dev(self):
        from plugin.framework.default_models import DEFAULT_MODELS
        from plugin.framework.constants import ModelCapability

        d = get_provider_defaults("together")
        self.assertEqual(d.get("image_model"), "black-forest-labs/FLUX.2-dev")

        flux = next((m for m in DEFAULT_MODELS if m.get("display_name") == "FLUX.2 [dev]"), None)
        self.assertIsNotNone(flux)
        self.assertEqual(flux["ids"].get("together"), "black-forest-labs/FLUX.2-dev")
        self.assertTrue(flux.get("default_image"))
        self.assertTrue(bool(flux["capability"] & ModelCapability.IMAGE))

        flash = next((m for m in DEFAULT_MODELS if m.get("ids", {}).get("together") == "google/flash-image-2.5"), None)
        self.assertIsNotNone(flash)
        self.assertFalse(flash.get("default_image"))
        self.assertTrue(bool(flash["capability"] & ModelCapability.IMAGE))

    def test_openrouter_free_model_catalog(self):
        from plugin.framework.default_models import DEFAULT_MODELS
        from plugin.framework.constants import ModelCapability

        free_m = next((m for m in DEFAULT_MODELS if m.get("ids", {}).get("openrouter") == "openrouter/free"), None)
        self.assertIsNotNone(free_m)
        self.assertEqual(free_m["display_name"], "Free Models (Auto)")
        caps = free_m["capability"]
        self.assertTrue(bool(caps & ModelCapability.CHAT))
        self.assertTrue(bool(caps & ModelCapability.TOOLS))
        self.assertTrue(bool(caps & ModelCapability.VISION))
        self.assertEqual(free_m["context_length"], 200000)

    def test_gemini_31_pro_catalog(self):
        from plugin.framework.default_models import DEFAULT_MODELS
        from plugin.framework.constants import ModelCapability

        pro = next((m for m in DEFAULT_MODELS if m.get("display_name") == "Gemini 3.1 Pro"), None)
        self.assertIsNotNone(pro)
        self.assertEqual(pro["ids"].get("google"), "gemini-3.1-pro")
        self.assertEqual(pro["ids"].get("openrouter"), "google/gemini-3.1-pro")
        caps = pro["capability"]
        self.assertTrue(bool(caps & ModelCapability.CHAT))
        self.assertTrue(bool(caps & ModelCapability.TOOLS))
        self.assertTrue(bool(caps & ModelCapability.VISION))
        self.assertTrue(bool(caps & ModelCapability.AUDIO))

    def test_writeragent_mock_catalog_window(self):
        from plugin.framework.default_models import DEFAULT_MODELS
        from plugin.framework.constants import ModelCapability

        row = next((m for m in DEFAULT_MODELS if m.get("ids", {}).get("mock") == "writeragent-mock"), None)
        self.assertIsNotNone(row)
        self.assertEqual(row["context_length"], 32768)
        self.assertFalse(row.get("default_text"))
        self.assertTrue(bool(row["capability"] & ModelCapability.CHAT))
        # Must not become the custom-endpoint Settings default.
        self.assertIsNone(get_provider_defaults("custom").get("text_model"))
        self.assertIsNone(resolve_model_id(row, "openai"))
        self.assertIsNone(resolve_model_id(row, "custom"))

    def test_together_deepseek_v4_flash_catalog(self):
        from plugin.framework.default_models import DEFAULT_MODELS
        from plugin.framework.constants import ModelCapability

        v4 = next((m for m in DEFAULT_MODELS if m.get("display_name") == "DeepSeek V4 Flash"), None)
        self.assertIsNotNone(v4)
        self.assertEqual(v4["ids"].get("together"), "deepseek-ai/DeepSeek-V4-Flash-0731")
        caps = v4["capability"]
        self.assertTrue(bool(caps & ModelCapability.CHAT))
        self.assertTrue(bool(caps & ModelCapability.TOOLS))


if __name__ == "__main__":
    unittest.main()
