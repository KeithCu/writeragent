"""Tests for DEFAULT_MODELS and get_provider_defaults."""

import unittest

from plugin.framework.default_models import get_provider_defaults


class TestGetProviderDefaults(unittest.TestCase):
    def test_together_defaults_match_catalog(self):
        d = get_provider_defaults("together")
        self.assertEqual(d.get("text_model"), "MiniMaxAI/MiniMax-M2.7")
        self.assertEqual(d.get("image_model"), "google/flash-image-2.5")
        self.assertEqual(d.get("stt_model"), "mistralai/Voxtral-Mini-3B-2507")

    def test_minimax_m27_together_catalog(self):
        from plugin.framework.default_models import DEFAULT_MODELS

        mm = next((m for m in DEFAULT_MODELS if m["display_name"] == "MiniMax M2.7"), None)
        self.assertIsNotNone(mm)
        self.assertEqual(mm["ids"]["together"], "MiniMaxAI/MiniMax-M2.7")
        self.assertEqual(mm["context_length"], 197000)


if __name__ == "__main__":
    unittest.main()
