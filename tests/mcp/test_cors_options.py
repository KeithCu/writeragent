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
from plugin.mcp.cors_origins import set_allow_private_origins, set_extra_allowed_origins
from plugin.mcp.mcp_protocol import MCP_PROTOCOL_VERSION


def test_merge_allow_headers_includes_base_and_requested():
    allow = merge_allow_headers("content-type, mcp-protocol-version")
    lower = allow.lower()
    assert "content-type" in lower
    assert "mcp-protocol-version" in lower
    assert "x-document-url" in lower


def test_merge_allow_headers_title_case_protocol_version():
    allow = merge_allow_headers("Content-Type, Mcp-Protocol-Version")
    lower = allow.lower()
    assert "mcp-protocol-version" in lower
    assert "Mcp-Protocol-Version" in allow


def test_merge_allow_headers_dedupes_case_insensitive():
    allow = merge_allow_headers("MCP-PROTOCOL-VERSION, mcp-protocol-version")
    assert allow.lower().count("mcp-protocol-version") == 1
    assert "Mcp-Protocol-Version" in allow or "mcp-protocol-version" in allow


def test_merge_allow_headers_without_request():
    allow = merge_allow_headers(None)
    lower = allow.lower()
    assert "mcp-protocol-version" in lower
    assert "Mcp-Protocol-Version" in allow
    assert "x-document-url" in lower


def test_is_safe_origin_localhost():
    assert is_safe_origin("http://localhost:3000")
    assert not is_safe_origin("http://localhost.attacker.com")


def test_is_safe_origin_ipv6_and_ports():
    assert is_safe_origin("http://[::1]:3000")
    assert is_safe_origin("http://127.0.0.1:8765")
    assert not is_safe_origin("http://evil.localhost")


def _expose_headers(response) -> str:
    return response.headers.get("Access-Control-Expose-Headers", "")


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
        expose = _expose_headers(response).lower()
        assert "mcp-session-id" in expose
        assert "mcp-protocol-version" in expose


def test_options_mcp_allow_headers_title_case_preflight(mcp_server):
    req = urllib.request.Request(
        f"{mcp_server}/mcp",
        method="OPTIONS",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, Mcp-Protocol-Version",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 204
        allow_headers = response.headers.get("Access-Control-Allow-Headers", "")
        assert "mcp-protocol-version" in allow_headers.lower()


def test_options_mcp_expose_headers(mcp_server):
    req = urllib.request.Request(
        f"{mcp_server}/mcp",
        method="OPTIONS",
        headers={"Origin": "http://127.0.0.1:8080", "Access-Control-Request-Method": "POST"},
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        expose = _expose_headers(response)
        assert "Mcp-Session-Id" in expose
        assert "Mcp-Protocol-Version" in expose


def test_options_mcp_private_origin_without_explicit_list(mcp_server):
    """localai.local allowed via cors_allow_private_origins rule, not explicit list."""
    set_extra_allowed_origins([])
    set_allow_private_origins(True)
    req = urllib.request.Request(
        f"{mcp_server}/mcp",
        method="OPTIONS",
        headers={
            "Origin": "https://localai.local",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 204
        assert response.headers.get("Access-Control-Allow-Origin") == "https://localai.local"


def test_options_mcp_extra_allowed_origin(mcp_server):
    set_extra_allowed_origins(["https://localai.local"])
    try:
        req = urllib.request.Request(
            f"{mcp_server}/mcp",
            method="OPTIONS",
            headers={
                "Origin": "https://localai.local",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.status == 204
            assert response.headers.get("Access-Control-Allow-Origin") == "https://localai.local"
    finally:
        set_extra_allowed_origins([])


def test_options_mcp_unsafe_origin_no_allow_origin(mcp_server):
    req = urllib.request.Request(
        f"{mcp_server}/mcp",
        method="OPTIONS",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 204
        assert response.headers.get("Access-Control-Allow-Origin") is None


def test_options_mcp_no_origin_header(mcp_server):
    req = urllib.request.Request(
        f"{mcp_server}/mcp",
        method="OPTIONS",
        headers={"Access-Control-Request-Method": "POST"},
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 204
        assert response.headers.get("Access-Control-Allow-Origin") is None
        assert response.headers.get("Access-Control-Allow-Methods") is not None
        assert response.headers.get("Access-Control-Allow-Headers") is not None


def test_options_health_preflight(mcp_server):
    req = urllib.request.Request(
        f"{mcp_server}/health",
        method="OPTIONS",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 204
        assert "mcp-protocol-version" in response.headers.get("Access-Control-Allow-Headers", "").lower()


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
        expose = _expose_headers(response)
        assert "Mcp-Session-Id" in expose
        assert "Mcp-Protocol-Version" in expose
        assert response.headers.get("Mcp-Protocol-Version") == MCP_PROTOCOL_VERSION


def test_post_initialize_cors_expose_and_session(mcp_server):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
    }
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{mcp_server}/mcp", method="POST", data=data_bytes)
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", "http://127.0.0.1:8080")
    req.add_header("Mcp-Protocol-Version", MCP_PROTOCOL_VERSION)
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 200
        assert response.headers.get("Mcp-Session-Id")
        assert response.headers.get("Mcp-Protocol-Version") == MCP_PROTOCOL_VERSION
        expose = _expose_headers(response)
        assert "Mcp-Session-Id" in expose
        assert "Mcp-Protocol-Version" in expose


def test_preflight_then_post_tools_list(mcp_server):
    origin = "http://localhost:3000"
    preflight = urllib.request.Request(
        f"{mcp_server}/mcp",
        method="OPTIONS",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, Mcp-Protocol-Version, Mcp-Session-Id",
        },
    )
    with urllib.request.urlopen(preflight, timeout=5) as response:
        assert response.status == 204
        assert response.headers.get("Access-Control-Allow-Origin") == origin

    payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    data_bytes = json.dumps(payload).encode("utf-8")
    post = urllib.request.Request(f"{mcp_server}/mcp", method="POST", data=data_bytes)
    post.add_header("Content-Type", "application/json")
    post.add_header("Origin", origin)
    post.add_header("Mcp-Protocol-Version", MCP_PROTOCOL_VERSION)
    with urllib.request.urlopen(post, timeout=5) as response:
        assert response.status == 200
        data = json.loads(response.read().decode("utf-8"))
        assert data.get("jsonrpc") == "2.0"
        assert "result" in data


def test_post_unsupported_protocol_version(mcp_server):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{mcp_server}/mcp", method="POST", data=data_bytes)
    req.add_header("Content-Type", "application/json")
    req.add_header("Mcp-Protocol-Version", "2099-01-01")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    assert exc_info.value.code == 400
    body = json.loads(exc_info.value.read().decode("utf-8"))
    assert body.get("error", {}).get("code") == -32600
