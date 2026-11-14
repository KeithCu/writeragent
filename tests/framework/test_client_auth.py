import pytest

from plugin.framework.client.auth import (
    AuthError,
    resolve_auth_for_config,
    build_auth_headers,
)
from plugin.framework.client.provider_detection import get_provider_from_endpoint


@pytest.mark.parametrize(
    ("api_config", "expected_provider", "expected_endpoint", "expected_key", "expected_style"),
    [
        pytest.param(
            {"endpoint": "https://openrouter.ai/api/v1", "api_key": "sk-or-testkey123"},
            "openrouter",
            "https://openrouter.ai/api",
            "sk-or-testkey123",
            "bearer",
            id="openrouter-strips-v1",
        ),
        pytest.param(
            {"endpoint": "http://localhost:11434", "api_key": ""},
            "ollama",
            "http://localhost:11434",
            "",
            "none",
            id="ollama-keyless-no-header",
        ),
        pytest.param(
            {"endpoint": "http://localhost:1234", "api_key": ""},
            "lmstudio",
            "http://localhost:1234",
            "",
            "none",
            id="lmstudio-keyless-no-header",
        ),
        pytest.param(
            {"endpoint": "http://my-custom-endpoint:8080/v1"},
            "custom",
            "http://my-custom-endpoint:8080",
            "",
            "bearer",
            id="custom-keyless-defaults-bearer",
        ),
        pytest.param(
            {"endpoint": "https://completely.different.url", "api_key": "valid-key", "is_openrouter": True},
            "openrouter",
            "https://completely.different.url",
            "valid-key",
            "bearer",
            id="is_openrouter-bypasses-url-detection",
        ),
    ],
)
def test_resolve_auth_for_config_success(
    api_config, expected_provider, expected_endpoint, expected_key, expected_style
):
    result = resolve_auth_for_config(api_config)
    assert result["provider"] == expected_provider
    assert result["endpoint"] == expected_endpoint
    assert result["api_key"] == expected_key
    assert result["header_style"] == expected_style


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
    """Test generating headers for no-auth providers without an API key."""
    auth_info = {
        "header_style": "none",
        "api_key": ""
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


def test_build_auth_headers_extra_headers_case_insensitive():
    auth_info = {
        "header_style": "bearer",
        "api_key": "sk-test",
        "headers": {"authorization": "Bearer other", "anthropic-version": "1"},
    }
    headers = build_auth_headers(auth_info)
    assert headers == {
        "Authorization": "Bearer sk-test",
        "anthropic-version": "1",
    }


def test_build_auth_headers_empty_key():
    """Test building auth headers with an empty API key."""
    auth_info = {
        "header_style": "bearer",
        "api_key": ""
    }

    headers = build_auth_headers(auth_info)
    # If key is empty, the header shouldn't be added at all
    assert "Authorization" not in headers


def test_build_auth_headers_non_str_style_coerced():
    """Non-str header_style must not AttributeError (CrossHair int case)."""
    headers_empty = build_auth_headers({"header_style": 2, "api_key": ""})
    assert "Authorization" not in headers_empty
    headers = build_auth_headers({"header_style": 2, "api_key": "k"})
    assert headers["Authorization"] == "Bearer k"  # unknown style falls back to Bearer when key is present
    headers_bearer = build_auth_headers({"header_style": "BEARER", "api_key": "k"})
    assert headers_bearer["Authorization"] == "Bearer k"


def test_build_auth_headers_style_none_with_key_fallback():
    # Style none plus key -> Authorization: Bearer <key>
    headers = build_auth_headers({"header_style": "none", "api_key": "secret"})
    assert headers["Authorization"] == "Bearer secret"

    # Style none plus empty key -> no Authorization
    headers_empty = build_auth_headers({"header_style": "none", "api_key": ""})
    assert "Authorization" not in headers_empty

    # Style bearer still produces one Bearer header, not two
    headers_bearer = build_auth_headers({"header_style": "bearer", "api_key": "secret"})
    assert headers_bearer["Authorization"] == "Bearer secret"
    assert len([k for k in headers_bearer if k.lower() == "authorization"]) == 1


def test_build_auth_headers_rejects_control_chars():
    with pytest.raises(AuthError) as err:
        build_auth_headers({"header_style": "bearer", "api_key": "sk-\r\ninject"})
    assert err.value.code == "invalid_api_key"

    # Smart quote, interior NBSP, DEL, and interior space reject
    for bad_key in ("sk-\u201csmart", "sk-\u00a0nbsp", "sk-\x7fdel", "sk interior space"):
        with pytest.raises(AuthError) as exc_info:
            build_auth_headers({"header_style": "bearer", "api_key": bad_key})
        assert exc_info.value.code == "invalid_api_key"
        assert "printable ASCII" in str(exc_info.value)

    # Whitespace padding is stripped cleanly
    padded = build_auth_headers({"header_style": "bearer", "api_key": "  sk-abc  "})
    assert padded["Authorization"] == "Bearer sk-abc"


@pytest.mark.parametrize(
    ("api_config", "expected_code", "expected_provider", "expected_msg"),
    [
        pytest.param({}, "missing_endpoint", "", "No endpoint configured", id="missing-endpoint"),
        pytest.param(
            {"endpoint": "https://api.openai.com/v1", "api_key": "   "},
            "missing_api_key",
            "openai",
            "No API key configured",
            id="openai-requires-nonblank-api-key",
        ),
    ],
)
def test_resolve_auth_for_config_errors(api_config, expected_code, expected_provider, expected_msg):
    with pytest.raises(AuthError) as exc_info:
        resolve_auth_for_config(api_config)

    assert exc_info.value.code == expected_code
    assert exc_info.value.provider == expected_provider
    assert expected_msg in str(exc_info.value)


def test_resolve_provider_id_does_not_substring_match_host():
    """notollama.example is not Ollama; a path containing openrouter.ai is not OpenRouter."""
    assert (get_provider_from_endpoint("https://notollama.example/v1") or "custom") == "custom"
    assert (get_provider_from_endpoint("https://example.com/openrouter.ai/v1") or "custom") == "custom"
    assert (get_provider_from_endpoint("https://api.openai.com/v1") or "custom") == "openai"


def test_resolve_provider_id_deal_contract_endpoints():
    """Real endpoints with IPv6 brackets or query strings must not fail @deal.pre in dev/pytest."""
    assert (get_provider_from_endpoint("http://[::1]:11434") or "custom") == "ollama"
    assert (get_provider_from_endpoint("https://example.com/v1?x=1") or "custom") == "custom"

