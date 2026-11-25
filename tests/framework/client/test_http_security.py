# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.framework.client.http_security."""

from __future__ import annotations

import pytest

from plugin.framework.client.http_security import apply_redirect, is_credential_name, secrets_from_target, secrets_from_url_path


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Ocp-Apim-Subscription-Key", True),
        ("X-Functions-Key", True),
        ("X-Authtoken", True),
        ("key", True),
        ("Cookie", True),
        ("Proxy-Authorization", True),
        ("max_tokens", False),
        ("output_modalities", False),
        ("Mcp-Session-Id", False),
        ("Content-Type", False),
        ("code", False),
        ("X-Hub-Signature", False),
    ],
)
def test_is_credential_name(name: str, expected: bool):
    assert is_credential_name(name) is expected


@pytest.mark.parametrize(
    ("param", "collected"),
    [
        ("code", True),
        ("sig", True),
        ("X-Amz-Signature", True),
        ("X-Goog-Signature", True),
        ("error_code", False),
    ],
)
def test_secrets_from_target_collects_signed_query_params(param: str, collected: bool):
    url = f"https://example.com/mcp?{param}=SignedSecret123"
    secrets = secrets_from_target(url)
    assert ("SignedSecret123" in secrets) is collected


def test_secrets_from_url_path():
    url = "https://example.com/s/8d2f6f3e-1a2b-4c5d-8e9f-0a1b2c3d4e5f/AbCdEfGhIjKlMnOpQr%3D%3D/fc-0123456789abcdef0123456789abcdef/mcp"
    secrets = secrets_from_url_path(url)
    assert "8d2f6f3e-1a2b-4c5d-8e9f-0a1b2c3d4e5f" in secrets
    assert "AbCdEfGhIjKlMnOpQr%3D%3D" in secrets
    assert "AbCdEfGhIjKlMnOpQr==" in secrets
    assert "fc-0123456789abcdef0123456789abcdef" in secrets
    assert "mcp" not in secrets

    benign = "https://example.com/api/v1beta/streamable-http/model-context-protocol/mcp"
    assert secrets_from_url_path(benign) == []


def test_secrets_from_target_ignores_model_names_in_path():
    assert secrets_from_target("/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent") == []


def test_secrets_from_target_gives_raw_and_decoded_values():
    url = "https://user:pw%2B123@example.com/mcp?token=a%2Fb%2Bcdef&subscription-key=sub%2Fkey1&max_tokens=4096"
    secrets = secrets_from_target(url)
    assert "a%2Fb%2Bcdef" in secrets
    assert "a/b+cdef" in secrets
    assert "sub%2Fkey1" in secrets
    assert "sub/key1" in secrets
    assert "pw%2B123" in secrets
    assert "pw+123" in secrets
    assert "4096" not in secrets


def test_secret_header_names_strips_cookie_and_proxy_authorization_on_redirect():
    """apply_redirect must strip credential headers on cross-origin hops."""
    headers = {
        "Authorization": "Bearer secret-token",
        "Cookie": "session=abc123xyz",
        "Proxy-Authorization": "Basic proxycreds",
        "Ocp-Apim-Subscription-Key": "sub-secret",
        "X-Custom-Header": "keep-me",
    }
    # Cross-origin redirect
    result = apply_redirect("POST", b"{}", headers, "https://source.com/api", 302, "https://target.com/api")
    assert result is not None
    _, _, new_headers, origin, _, _ = result
    assert origin == "https://target.com"
    lower_keys = {k.lower() for k in new_headers}
    assert "cookie" not in lower_keys
    assert "proxy-authorization" not in lower_keys
    assert "authorization" not in lower_keys
    assert "ocp-apim-subscription-key" not in lower_keys
    assert new_headers.get("X-Custom-Header") == "keep-me"

    # Same-origin redirect preserves Cookie, Proxy-Authorization, and Ocp-Apim-Subscription-Key
    same_result = apply_redirect("GET", None, headers, "https://source.com/api", 302, "/new-path")
    assert same_result is not None
    _, _, same_headers, same_origin, _, _ = same_result
    assert same_origin == "https://source.com"
    assert "Cookie" in same_headers
    assert "Proxy-Authorization" in same_headers
    assert "Ocp-Apim-Subscription-Key" in same_headers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(
            "/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent?key=secret",
            "/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent",
            id="rel-gemini-model-kept",
        ),
        pytest.param(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent?key=secret",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent",
            id="abs-gemini-model-kept",
        ),
        pytest.param(
            "/openai/deployments/gpt-4o-mini-2024-07-18/chat/completions",
            "/openai/deployments/gpt-4o-mini-2024-07-18/chat/completions",
            id="rel-azure-deployment-kept",
        ),
        pytest.param(
            "https://my.openai.azure.com/openai/deployments/gpt-4o-mini-2024-07-18/chat/completions",
            "https://my.openai.azure.com/openai/deployments/gpt-4o-mini-2024-07-18/chat/completions",
            id="abs-azure-deployment-kept",
        ),
        pytest.param(
            "/api/v1/models/anthropic/claude-sonnet-4-20250514/endpoints",
            "/api/v1/models/anthropic/claude-sonnet-4-20250514/endpoints",
            id="rel-openrouter-model-kept",
        ),
        pytest.param(
            "/s/123e4567-e89b-12d3-a456-426614174000/v1/models",
            "/s/<redacted>/v1/models",
            id="rel-uuid-redacted",
        ),
        pytest.param(
            "https://proxy.example/s/123e4567-e89b-12d3-a456-426614174000/v1/models",
            "https://proxy.example/s/<redacted>/v1/models",
            id="abs-uuid-redacted",
        ),
        pytest.param(
            "/s/AbCdEfGhIjKlMnOpQr%3D%3D/v1/chat/completions",
            "/s/<redacted>/v1/chat/completions",
            id="rel-base64-redacted",
        ),
        pytest.param(
            "https://user:pass@proxy.example/s/AbCdEfGhIjKlMnOpQr%3D%3D/v1/chat/completions",
            "https://proxy.example/s/<redacted>/v1/chat/completions",
            id="abs-base64-redacted",
        ),
    ],
)
def test_public_target_redacts_path_secrets_and_keeps_model_names(raw, expected):
    from plugin.framework.client.http_security import public_target

    assert public_target(raw) == expected


def test_secrets_from_target_collects_path_secret_before_models():
    from plugin.framework.client.http_security import secrets_from_target

    secrets = secrets_from_target("https://proxy.example/s/AbCdEfGhIjKlMnOpQr%3D%3D/v1/models/claude-sonnet-4-20250514")
    assert "AbCdEfGhIjKlMnOpQr%3D%3D" in secrets
    assert "AbCdEfGhIjKlMnOpQr==" in secrets
    assert "claude-sonnet-4-20250514" not in secrets


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://proxy.example:1a34/s/123e4567-e89b-12d3-a456-426614174000/v1/models",
        "https://[::1/s/123e4567-e89b-12d3-a456-426614174000/v1/models",
    ],
)
def test_invalid_url_details_redact_path_secret(bad_url):
    from plugin.framework.client.http_security import public_target
    from plugin.framework.errors import NetworkError

    with pytest.raises(NetworkError) as exc_info:
        public_target(bad_url)
    assert exc_info.value.code == "INVALID_URL"
    assert "123e4567-e89b-12d3-a456-426614174000" not in exc_info.value.details["url"]
    assert "<redacted>" in exc_info.value.details["url"]


@pytest.mark.parametrize(
    ("bad_url", "secrets"),
    [
        ("https://user:hunter2@[::1/v1", ("hunter2",)),
        ("https://us[er:hunter2/x9@host/v1", ("hunter2", "x9")),
        ("https://user:hunter2/x9@host/v1", ("hunter2", "x9")),
        ("https://user:hunter2@ho\u2100st/x", ("hunter2",)),
        ("https://[::1#token=hunter2", ("hunter2",)),
        ("https://us[er:hunter2#x@host/v1", ("hunter2",)),
        ("https://u:a]hunter2?x@h/v1", ("hunter2",)),
        ("https://[::1/v1?e=a@b&key=hunter2", ("hunter2",)),
    ],
)
def test_invalid_url_details_strip_userinfo(bad_url, secrets):
    from plugin.framework.client.http_security import log_target, origin_and_path
    from plugin.framework.errors import NetworkError

    with pytest.raises(NetworkError) as exc_info:
        origin_and_path(bad_url)
    assert exc_info.value.code == "INVALID_URL"
    assert exc_info.value.__suppress_context__ is True
    shown = log_target(bad_url)
    for secret in secrets:
        assert secret not in exc_info.value.details["url"]
        assert secret not in shown


def test_invalid_url_strips_userinfo_without_scheme():
    from plugin.framework.client.http_security import origin_and_path
    from plugin.framework.errors import NetworkError

    with pytest.raises(NetworkError) as exc_info:
        origin_and_path("//user:hunter2@[::1/x")
    assert exc_info.value.code == "INVALID_URL"
    assert "hunter2" not in exc_info.value.details["url"]


def test_invalid_url_traceback_hides_userinfo():
    import traceback
    from plugin.framework.client.http_security import origin_and_path
    from plugin.framework.errors import NetworkError

    # Split so pytest's source-line traceback frame does not echo the secret.
    bad_url = "https://user:hunter2" + "@ho\u2100st/x"
    with pytest.raises(NetworkError) as exc_info:
        origin_and_path(bad_url)
    tb = "".join(traceback.format_exception(exc_info.value))
    assert "hunter2" not in tb


def test_log_target():
    from plugin.framework.client.http_security import log_target, public_target

    valid = "https://u:pw@h.example/v1/models?k=secret"
    assert log_target(valid) == public_target(valid)
    assert (
        log_target("http://u:pw@h.example:1a34/s/123e4567-e89b-12d3-a456-426614174000/v1")
        == "http://h.example/s/<redacted>/v1"
    )
    assert log_target("http://[::1]:1234/v1") == "http://[::1]:1234/v1"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "/"),
        ("/v1#S", "/v1"),
        ("/v1?k=S#f", "/v1"),
        ("/v1?r=https://a/b", "/v1"),
        ("/v1/models/claude@2024:raw", "/v1/models/claude@2024:raw"),
        ("//u:pw@h/v1", "h/v1"),
        ("//h/v1", "//h/v1"),
        ("u:pw@h/v1?x=1", "h/v1"),
        ("u:p/w@h", "h"),
        ("u:p?w@h", "/"),
        ("https:user:pw@h", "h"),
        ("u:pw@h/v1?n=https://x/y", "h/v1"),
        ("localhost:1234", "localhost:1234"),
        ("123e4567-e89b-12d3-a456-426614174000/v1", "<redacted>/v1"),
    ],
)
def test_public_target_without_scheme(raw: str, expected: str):
    from plugin.framework.client.http_security import public_target

    assert public_target(raw) == expected
