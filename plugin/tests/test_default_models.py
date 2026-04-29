"""Tests for DEFAULT_MODELS and get_provider_defaults."""

import unittest

from plugin.framework.default_models import get_provider_defaults


class TestGetProviderDefaults(unittest.TestCase):
    def test_together_defaults_match_catalog(self):
        d = get_provider_defaults("together")
        self.assertEqual(d.get("text_model"), "Qwen/Qwen3-Coder-Next-FP8")
        self.assertEqual(d.get("image_model"), "google/flash-image-2.5")
        self.assertEqual(d.get("stt_model"), "mistralai/Voxtral-Mini-3B-2507")

    def test_qwen_coder_presence(self):
        from plugin.framework.default_models import DEFAULT_MODELS
        qwen = next((m for m in DEFAULT_MODELS if m["display_name"] == "Qwen 3 Coder Next FP8"), None)
        self.assertIsNotNone(qwen)
        self.assertEqual(qwen["ids"]["together"], "Qwen/Qwen3-Coder-Next-FP8")
        self.assertEqual(qwen["context_length"], 262144)


if __name__ == "__main__":
    unittest.main()
