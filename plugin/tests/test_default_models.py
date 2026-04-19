"""Tests for DEFAULT_MODELS and get_provider_defaults."""

import unittest

from plugin.framework.default_models import get_provider_defaults


class TestGetProviderDefaults(unittest.TestCase):
    def test_together_defaults_match_catalog(self):
        d = get_provider_defaults("together")
        self.assertEqual(d.get("text_model"), "openai/gpt-oss-120b")
        self.assertEqual(d.get("image_model"), "google/flash-image-2.5")
        self.assertEqual(d.get("stt_model"), "mistralai/Voxtral-Mini-3B-2507")


if __name__ == "__main__":
    unittest.main()
