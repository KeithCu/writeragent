"""Tests for DEFAULT_MODELS and get_provider_defaults."""

import unittest

from plugin.framework.default_models import get_provider_defaults


class TestGetProviderDefaults(unittest.TestCase):
    def test_together_defaults_match_catalog(self):
        d = get_provider_defaults("together")
        self.assertTrue(bool(d.get("text_model")))
        self.assertTrue(bool(d.get("image_model")))
        self.assertTrue(bool(d.get("stt_model")))

    def test_minimax_m27_together_catalog(self):
        from plugin.framework.default_models import DEFAULT_MODELS

        mm = next((m for m in DEFAULT_MODELS if m["display_name"] == "MiniMax M2.7"), None)
        self.assertIsNotNone(mm)
        self.assertEqual(mm["ids"]["together"], "MiniMaxAI/MiniMax-M2.7")
        self.assertEqual(mm["context_length"], 197000)

    def test_openrouter_default_text_model_uses_nitro(self):
        d = get_provider_defaults("openrouter")
        self.assertEqual(d.get("text_model"), "openai/gpt-oss-120b:nitro")

    def test_openrouter_default_stt_model_uses_voxtral(self):
        d = get_provider_defaults("openrouter")
        self.assertEqual(d.get("stt_model"), "mistralai/voxtral-mini-transcribe")

    def test_openrouter_default_image_model(self):
        d = get_provider_defaults("openrouter")
        self.assertEqual(d.get("image_model"), "google/gemini-2.5-flash-image")


if __name__ == "__main__":
    unittest.main()
