import unittest

import pytest

from plugin.chatbot.config_ui_helpers import _is_incompatible_model_for_provider
from plugin.framework.client.auth import (
    PROVIDERS,
    _resolve_provider_id,
    provider_requires_slug_model_id,
)
from plugin.framework.client.model_fetcher import ENDPOINT_PRESETS
from plugin.framework.client.provider_detection import get_provider_from_endpoint
from plugin.framework.url_utils import normalize_endpoint_url


# Expected provider per ENDPOINT_PRESETS URL (after normalize).
_PRESET_EXPECTED_PROVIDER = {
    "http://localhost:11434": "ollama",
    "http://localhost:1234": "lmstudio",
    "https://openrouter.ai/api": "openrouter",
    "https://api.mistral.ai": "mistral",
    "https://api.together.xyz": "together",
    "https://api.groq.com/openai": "groq",
    "https://api.deepseek.com": "deepseek",
    "https://api.cerebras.ai": "cerebras",
    "https://api.perplexity.ai": "perplexity",
    "https://api.x.ai": "xai",
    "https://api.anthropic.com": "anthropic",
    "https://generativelanguage.googleapis.com/v1beta/openai": "google",
    "https://api.z.ai/api/paas": "zai",
}


@pytest.mark.parametrize(
    "provider,model_id,compatible",
    [
        ("openrouter", "openai/gpt-4o", True),
        ("openrouter", "llama3.2", False),
        ("together", "meta-llama/Llama-3-70b", True),
        ("together", "llama3.2", False),
        ("zai", "glm-5.2", True),
        ("deepseek", "deepseek-chat", True),
        ("mistral", "mistral-large-latest", True),
        ("groq", "llama-3.3-70b-versatile", True),
        ("ollama", "llama3.2", True),
        ("openrouter", "google/gemini-3.1-flash-lite", True),
        ("zai", "google/gemini-3.1-flash-lite", False),
        ("lmstudio", "llama3.2", True),
        (None, "llama3.2", True),
    ],
)
def test_is_incompatible_model_for_provider_matrix(provider, model_id, compatible):
    assert _is_incompatible_model_for_provider(model_id, provider) is (not compatible)


class TestProviderSlugPolicy(unittest.TestCase):

    def test_only_openrouter_and_together_require_slug(self):
        slug_providers = {pid for pid, cfg in PROVIDERS.items() if cfg.model_id_style == "slug"}
        self.assertEqual(slug_providers, {"openrouter", "together"})

    def test_provider_requires_slug_model_id(self):
        self.assertTrue(provider_requires_slug_model_id("openrouter"))
        self.assertTrue(provider_requires_slug_model_id("together"))
        self.assertFalse(provider_requires_slug_model_id("zai"))
        self.assertFalse(provider_requires_slug_model_id("deepseek"))
        self.assertFalse(provider_requires_slug_model_id("lmstudio"))
        self.assertFalse(provider_requires_slug_model_id(None))


class TestPresetProviderDetection(unittest.TestCase):

    def test_every_endpoint_preset_resolves_expected_provider(self):
        for _label, url in ENDPOINT_PRESETS:
            normalized = normalize_endpoint_url(url)
            expected = _PRESET_EXPECTED_PROVIDER[normalized]
            self.assertEqual(
                get_provider_from_endpoint(normalized),
                expected,
                msg=f"preset url {url!r} normalized {normalized!r}",
            )

    def test_auth_matches_detection_for_hosted_presets(self):
        """Hosted presets in PROVIDERS: detection hint and host_matches agree."""
        skip_normalized = {"http://localhost:1234"}  # lmstudio: detection only, auth -> custom
        for _label, url in ENDPOINT_PRESETS:
            normalized = normalize_endpoint_url(url)
            if normalized in skip_normalized:
                continue
            detected = get_provider_from_endpoint(normalized)
            self.assertIsNotNone(detected, normalized)
            resolved = _resolve_provider_id(normalized, detected)
            self.assertEqual(resolved, detected, normalized)
