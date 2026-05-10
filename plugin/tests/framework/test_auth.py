import pytest
from unittest.mock import patch

from plugin.framework.auth import (
    AuthError,
    _resolve_provider_id,
    resolve_auth_for_config,
    build_auth_headers,
)

@patch("plugin.framework.auth.normalize_endpoint_url")
def test_resolve_provider_id_with_hint(mock_normalize):
    """Test that explicit provider hint skips host detection."""
    # The normalization logic should not even matter if a valid hint is provided
    # However, _resolve_provider_id calls normalize_endpoint_url early.
    mock_normalize.return_value = "https://something.else"

    # Valid hint maps directly
    assert _resolve_provider_id("https://something.else", "openrouter") == "openrouter"

    # Hint is case-insensitive
    assert _resolve_provider_id("https://something.else", " OPENROUTER ") == "openrouter"

    # Invalid hint falls back to URL detection, which here is mocked to return something else.
    # So we'll update the mock to return openai's url for this assert.
    mock_normalize.return_value = "https://api.openai.com"
    assert _resolve_provider_id("https://api.openai.com", "unknown_hint") == "openai"


@patch("plugin.framework.auth.normalize_endpoint_url")
def test_resolve_provider_id_by_url(mock_normalize):
    """Test host matching for known providers."""

    def side_effect(url):
        return url
    mock_normalize.side_effect = side_effect

    # Should match OpenAI
    assert _resolve_provider_id("https://api.openai.com/v1") == "openai"

    # Should match Together AI
    assert _resolve_provider_id("https://api.together.xyz/v1") == "together"

    # Should match Ollama
    assert _resolve_provider_id("http://localhost:11434") == "ollama"

    # Should match DeepSeek
    assert _resolve_provider_id("https://api.deepseek.com") == "deepseek"

    # Case insensitivity
    assert _resolve_provider_id("HTTPS://API.OPENAI.COM") == "openai"


@patch("plugin.framework.auth.normalize_endpoint_url")
def test_resolve_provider_id_custom_fallback(mock_normalize):
    """Test that an unknown URL without a valid hint returns 'custom'."""
    mock_normalize.return_value = "https://my-own-endpoint.com/v1"

    assert _resolve_provider_id("https://my-own-endpoint.com/v1") == "custom"


@patch("plugin.framework.auth.normalize_endpoint_url")
@patch("plugin.framework.auth.get_provider_from_endpoint")
def test_resolve_auth_for_config_known_provider(mock_get_provider, mock_normalize):
    mock_normalize.return_value = "https://openrouter.ai/api/v1"
    mock_get_provider.return_value = "openrouter"

    api_config = {
        "endpoint": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-testkey123"
    }

    result = resolve_auth_for_config(api_config)
    assert result["provider"] == "openrouter"


def test_build_auth_headers_bearer():
    """Test generating a bearer token header."""
    auth_info = {
        "header_style": "bearer",
        "api_key": "my-secret-key"
    }

    headers = build_auth_headers(auth_info)
    assert headers["Authorization"] == "Bearer my-secret-key"


def test_build_auth_headers_x_api_key():
    """Test generating an x-api-key header."""
    auth_info = {
        "header_style": "x-api-key",
        "api_key": "my-secret-key"
    }

    headers = build_auth_headers(auth_info)
    assert headers["x-api-key"] == "my-secret-key"
    assert "Authorization" not in headers


def test_build_auth_headers_none():
    """Test generating headers for no-auth providers."""
    auth_info = {
        "header_style": "none",
        "api_key": "this-key-will-be-ignored"
    }

    headers = build_auth_headers(auth_info)
    assert "Authorization" not in headers
    assert "x-api-key" not in headers


def test_build_auth_headers_with_extra_headers():
    """Test that extra provider-specific headers are merged without overwriting auth."""
    auth_info = {
        "header_style": "bearer",
        "api_key": "my-key",
        "headers": {
            "HTTP-Referer": "https://writeragent.test",
            "Authorization": "Bearer this-should-not-overwrite",
            "x-custom-version": "1.0"
        }
    }

    headers = build_auth_headers(auth_info)

    # Auth header comes from style + key, so extra header 'Authorization' should not override
    assert headers["Authorization"] == "Bearer my-key"
    assert headers["HTTP-Referer"] == "https://writeragent.test"
    assert headers["x-custom-version"] == "1.0"


def test_build_auth_headers_empty_key():
    """Test building auth headers with an empty API key."""
    auth_info = {
        "header_style": "bearer",
        "api_key": ""
    }

    headers = build_auth_headers(auth_info)
    # If key is empty, the header shouldn't be added at all
    assert "Authorization" not in headers


@patch("plugin.framework.auth.normalize_endpoint_url")
@patch("plugin.framework.auth.get_provider_from_endpoint")
def test_resolve_auth_for_config_local_no_api_key(mock_get_provider, mock_normalize):
    mock_normalize.return_value = "http://localhost:11434"
    mock_get_provider.return_value = "ollama"

    # Ollama is header_style="none", and doesn't require an api_key
    api_config = {
        "endpoint": "http://localhost:11434",
        "api_key": ""
    }

    result = resolve_auth_for_config(api_config)
    assert result["provider"] == "ollama"
    assert result["endpoint"] == "http://localhost:11434"
    assert result["api_key"] == ""
    assert result["header_style"] == "none"


@patch("plugin.framework.auth.normalize_endpoint_url")
@patch("plugin.framework.auth.get_provider_from_endpoint")
def test_resolve_auth_for_config_custom_no_api_key(mock_get_provider, mock_normalize):
    mock_normalize.return_value = "http://my-custom-endpoint:8080/v1"
    mock_get_provider.return_value = None

    # Custom providers don't require an api_key
    api_config = {
        "endpoint": "http://my-custom-endpoint:8080/v1"
    }

    result = resolve_auth_for_config(api_config)
    assert result["provider"] == "custom"
    assert result["endpoint"] == "http://my-custom-endpoint:8080/v1"
    assert result["api_key"] == ""
    assert result["header_style"] == "bearer"  # custom defaults to bearer style


@patch("plugin.framework.auth.normalize_endpoint_url")
def test_resolve_auth_for_config_missing_endpoint(mock_normalize):
    mock_normalize.return_value = ""
    api_config = {}

    with pytest.raises(AuthError) as exc_info:
        resolve_auth_for_config(api_config)

    assert exc_info.value.code == "missing_endpoint"
    assert "No endpoint configured" in str(exc_info.value)


@patch("plugin.framework.auth.normalize_endpoint_url")
@patch("plugin.framework.auth.get_provider_from_endpoint")
def test_resolve_auth_for_config_missing_api_key_known_provider(mock_get_provider, mock_normalize):
    mock_normalize.return_value = "https://api.openai.com/v1"
    mock_get_provider.return_value = "openai"

    # OpenAI is a known provider that requires an api_key
    api_config = {
        "endpoint": "https://api.openai.com/v1",
        "api_key": "   " # empty after strip
    }

    with pytest.raises(AuthError) as exc_info:
        resolve_auth_for_config(api_config)

    assert exc_info.value.code == "missing_api_key"
    assert exc_info.value.provider == "openai"
    assert "No API key configured" in str(exc_info.value)


@patch("plugin.framework.auth.normalize_endpoint_url")
def test_resolve_auth_for_config_is_openrouter_flag(mock_normalize):
    """Test that `is_openrouter` bypasses URL detection entirely."""
    mock_normalize.return_value = "https://completely.different.url"

    api_config = {
        "endpoint": "https://completely.different.url",
        "api_key": "valid-key",
        "is_openrouter": True
    }

    result = resolve_auth_for_config(api_config)
    assert result["provider"] == "openrouter"
