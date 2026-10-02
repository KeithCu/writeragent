
import pytest

from plugin.chatbot.config_ui_helpers import _is_incompatible_model_for_provider
from plugin.framework.client.auth import (
    PROVIDERS,
    _resolve_provider_id,
    provider_requires_slug_model_id,
)
from plugin.framework.client.model_fetcher import ENDPOINT_PRESETS
from plugin.framework.client.provider_detection import get_provider_from_endpoint, is_openrouter_endpoint
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
    "https://generativelanguage.googleapis.com": "google",
    "https://integrate.api.nvidia.com": "nvidia",
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
        ("groq", "llama-3.1-8b-instant", True),
        ("ollama", "llama3.2", True),
        ("openrouter", "google/gemini-3.1-flash-lite", True),
        ("zai", "google/gemini-3.1-flash-lite", False),
        ("lmstudio", "llama3.2", True),
        (None, "llama3.2", True),
    ],
)
def test_is_incompatible_model_for_provider_matrix(provider, model_id, compatible):
    assert _is_incompatible_model_for_provider(model_id, provider) is (not compatible)


class TestProviderSlugPolicy:

    def test_only_openrouter_and_together_require_slug(self):
        slug_providers = {pid for pid, cfg in PROVIDERS.items() if cfg.model_id_style == "slug"}
        assert (slug_providers) == ({"openrouter", "together"})

    def test_provider_requires_slug_model_id(self):
        assert (provider_requires_slug_model_id("openrouter"))
        assert (provider_requires_slug_model_id("together"))
        assert not (provider_requires_slug_model_id("zai"))
        assert not (provider_requires_slug_model_id("deepseek"))
        assert not (provider_requires_slug_model_id("lmstudio"))
        assert not (provider_requires_slug_model_id(None))


class TestPresetProviderDetection:

    def test_every_endpoint_preset_resolves_expected_provider(self):
        for _label, url in ENDPOINT_PRESETS:
            normalized = normalize_endpoint_url(url)
            expected = _PRESET_EXPECTED_PROVIDER[normalized]
            assert (get_provider_from_endpoint(normalized)) == (expected), f"preset url {url!r} normalized {normalized!r}"

    def test_auth_matches_detection_for_hosted_presets(self):
        """Hosted presets: detection and auth resolution agree. Host matching is not on ProviderConfig."""
        for _label, url in ENDPOINT_PRESETS:
            normalized = normalize_endpoint_url(url)
            detected = get_provider_from_endpoint(normalized)
            assert (detected) is not None, normalized
            resolved = _resolve_provider_id(normalized, detected)
            assert (resolved) == (detected), normalized


def test_is_openrouter_endpoint_matches_host_not_substring():
    assert is_openrouter_endpoint("https://openrouter.ai/api/v1")
    assert is_openrouter_endpoint("https://gateway.openrouter.ai/api/v1")
    assert is_openrouter_endpoint("http://127.0.0.1:11434/v1", explicit_is_openrouter=True)
    assert not is_openrouter_endpoint("http://127.0.0.1:11434/v1")
    assert not is_openrouter_endpoint("https://notopenrouter.ai/v1")
    assert not is_openrouter_endpoint("https://example.com/openrouter.ai/v1")


def test_provider_config_has_no_host_matches_and_grok_alias_is_gone():
    """Detection is the host matcher. ``grok`` duplicated ``xai``."""
    import dataclasses

    from plugin.framework.client.grok_shim import GrokShim
    from plugin.framework.client.openai_shim import OpenAIShim, _SHIM_REGISTRY, get_provider_shim_class

    assert "host_matches" not in {f.name for f in dataclasses.fields(PROVIDERS["openai"])}
    assert "grok" not in _SHIM_REGISTRY
    assert get_provider_shim_class("xai") is GrokShim
    assert get_provider_shim_class("grok") is OpenAIShim
