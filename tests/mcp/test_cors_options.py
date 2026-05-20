# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import json
import urllib.request

import pytest

from plugin.mcp.cors import is_safe_origin, merge_allow_headers


def test_merge_allow_headers_includes_base_and_requested():
    allow = merge_allow_headers("content-type, mcp-protocol-version")
    lower = allow.lower()
    assert "content-type" in lower
    assert "mcp-protocol-version" in lower
    assert "x-document-url" in lower


def test_merge_allow_headers_without_request():
    allow = merge_allow_headers(None)
    lower = allow.lower()
    assert "mcp-protocol-version" in lower
    assert "x-document-url" in lower


def test_is_safe_origin_localhost():
    assert is_safe_origin("http://localhost:3000")
    assert not is_safe_origin("http://localhost.attacker.com")


def test_options_mcp_returns_204_empty_body(mcp_server):
    """OPTIONS /mcp is a valid CORS preflight with no response body."""
    req = urllib.request.Request(
        f"{mcp_server}/mcp",
        method="OPTIONS",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, mcp-protocol-version",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 204
        assert response.read() == b""
        allow_headers = response.headers.get("Access-Control-Allow-Headers", "")
        assert "mcp-protocol-version" in allow_headers.lower()
        assert response.headers.get("Access-Control-Max-Age") == "86400"
        assert response.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"


def test_post_mcp_cors_includes_x_document_url(mcp_server):
    """POST /mcp responses use the same Allow-Headers list as OPTIONS (includes X-Document-URL)."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{mcp_server}/mcp", method="POST", data=data_bytes)
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", "http://127.0.0.1:8080")
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 200
        allow_headers = response.headers.get("Access-Control-Allow-Headers", "")
        assert "x-document-url" in allow_headers.lower()
        assert "mcp-protocol-version" in allow_headers.lower()
