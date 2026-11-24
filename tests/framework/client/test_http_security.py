# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.framework.client.http_security."""

from __future__ import annotations

import pytest

from plugin.framework.client.http_security import apply_redirect, is_credential_name, secrets_from_target


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
    ],
)
def test_is_credential_name(name: str, expected: bool):
    assert is_credential_name(name) is expected


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
