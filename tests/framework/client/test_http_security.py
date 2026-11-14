# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.framework.client.http_security."""

from __future__ import annotations

from plugin.framework.client.http_security import apply_redirect


def test_secret_header_names_strips_cookie_and_proxy_authorization_on_redirect():
    """apply_redirect must strip cookie and proxy-authorization on cross-origin hops."""
    headers = {
        "Authorization": "Bearer secret-token",
        "Cookie": "session=abc123xyz",
        "Proxy-Authorization": "Basic proxycreds",
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
    assert new_headers.get("X-Custom-Header") == "keep-me"

    # Same-origin redirect preserves Cookie and Proxy-Authorization
    same_result = apply_redirect("GET", None, headers, "https://source.com/api", 302, "/new-path")
    assert same_result is not None
    _, _, same_headers, same_origin, _, _ = same_result
    assert same_origin == "https://source.com"
    assert "Cookie" in same_headers
    assert "Proxy-Authorization" in same_headers
