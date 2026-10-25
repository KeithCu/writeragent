# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import json
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest

import plugin.mcp.mcp_session as mcp_session
from plugin.framework.deal_shim import DEAL_MAX_ORIGIN
from plugin.mcp.cors import (
    get_allow_private_origins,
    get_extra_allowed_origins,
    is_private_browser_origin,
    is_safe_host,
    is_safe_origin,
    merge_allow_headers,
    normalize_cors_origin,
    normalize_origins_list,
    origin_is_forbidden,
    reject_forbidden_host,
    reload_cors_policy_from_config,
    send_cors_headers,
    set_allow_private_origins,
    set_extra_allowed_origins,
)
from plugin.mcp.wire_types import MCP_PROTOCOL_VERSION


def setup_function():
    set_extra_allowed_origins([])
    set_allow_private_origins(True)


def teardown_function():
    set_extra_allowed_origins([])
    set_allow_private_origins(True)


def test_normalize_cors_origin_strips_slash():
    assert normalize_cors_origin("https://localai.local/") == "https://localai.local"


def test_normalize_cors_origin_lowercases_and_strips_ports_paths():
    assert normalize_cors_origin("https://App.Example.com") == "https://app.example.com"
    assert normalize_cors_origin("http://HOST:8080/some/path") == "http://host:8080"
    assert normalize_cors_origin("http://host:80") == "http://host"
    assert normalize_cors_origin("https://host:443") == "https://host"
    assert normalize_cors_origin("http://[::1]:8080/foo") == "http://[::1]:8080"


def test_extra_allowed_origins_case_insensitive():
    set_extra_allowed_origins(["https://App.Example.COM"])
    assert is_safe_origin("https://app.example.com")
    assert is_safe_origin("https://APP.EXAMPLE.COM")
    assert is_safe_origin("https://App.Example.com/subpath")


def test_is_safe_host_loopback():
    assert is_safe_host("localhost")
    assert is_safe_host("localhost:18765")
    assert is_safe_host("127.0.0.1")
    assert is_safe_host("127.0.0.1:8080")
    assert is_safe_host("[::1]")
    assert is_safe_host("[::1]:18765")


def test_is_safe_host_rejects_malicious_and_empty():
    assert not is_safe_host("attacker.com")
    assert not is_safe_host("attacker.com:18765")
    assert not is_safe_host("evil.localhost")
    assert not is_safe_host("")
    assert not is_safe_host(None)
    assert not is_safe_host("localhost:notaport")
    assert not is_safe_host("[::1")


def test_is_safe_host_configured_tunnel_host(monkeypatch):
    monkeypatch.setattr("plugin.mcp.cors.get_configured_tunnel_host", lambda: "tunnel.trycloudflare.com")
    assert is_safe_host("tunnel.trycloudflare.com")
    assert is_safe_host("tunnel.trycloudflare.com:8443")
    assert not is_safe_host("other.com")
    assert not is_safe_host("attacker.com")


def test_reject_forbidden_host():
    class DummyHandler:
        def __init__(self, headers):
            self.headers = headers
            self.sent_responses = []
            self.sent_headers = []
            self._response_started = False

        def send_response(self, code):
            self.sent_responses.append(code)

        def send_header(self, k, v):
            self.sent_headers.append((k, v))

        def end_headers(self):
            pass

    safe = DummyHandler({"Host": "127.0.0.1:18765"})
    assert not reject_forbidden_host(safe)
    assert safe.sent_responses == []

    forbidden = DummyHandler({"Host": "attacker.com"})
    assert reject_forbidden_host(forbidden)
    assert forbidden.sent_responses == [403]
    assert forbidden._response_started is True

    missing = DummyHandler({})
    assert reject_forbidden_host(missing)
    assert missing.sent_responses == [403]


def test_normalize_cors_origin_rejects_invalid():
    assert normalize_cors_origin("localai.local") is None
    assert normalize_cors_origin("") is None
    assert normalize_cors_origin("http://host:99999") is None
    assert normalize_cors_origin("http://host:abc") is None
    assert normalize_cors_origin("http://host:-1") is None
    assert normalize_cors_origin("http://host:65536") is None
    assert normalize_cors_origin("http://[::1]:99999") is None
    assert normalize_cors_origin("http://host:0") == "http://host:0"
    assert normalize_cors_origin("http://host:65535") == "http://host:65535"


def test_normalize_origins_list_dedupes():
    assert normalize_origins_list(["https://a.com", "https://a.com/"]) == ["https://a.com"]


def test_is_private_browser_origin_suffixes():
    assert is_private_browser_origin("https://localai.local")
    assert is_private_browser_origin("http://nas.lan:8080")
    assert is_private_browser_origin("https://app.home.arpa")
    assert is_private_browser_origin("https://tool.internal")
    assert is_private_browser_origin("https://tool.intern")


def test_is_private_browser_origin_private_ip():
    assert is_private_browser_origin("http://192.168.1.50:3000")
    assert is_private_browser_origin("http://10.0.0.5:8080")


def test_is_private_browser_origin_rejects_public():
    assert not is_private_browser_origin("https://evil.com")
    assert not is_private_browser_origin("https://evil.localhost")
    assert not is_private_browser_origin("https://localai.local.com")
    assert not is_private_browser_origin("http://localhost.attacker.com")
    assert not is_private_browser_origin("https://intern.evil.com")


def test_is_private_browser_origin_rejects_malformed_ipv6_brackets():
    assert not is_private_browser_origin("https://[::1].evil.net")


def test_is_safe_origin_rejects_malformed_ipv6_brackets():
    set_allow_private_origins(True)
    assert not is_safe_origin("https://[::1].evil.net")


def test_is_safe_origin_private_when_enabled():
    set_allow_private_origins(True)
    assert is_safe_origin("https://localai.local")
    assert is_safe_origin("http://192.168.0.2:8123")


def test_is_safe_origin_private_when_disabled():
    set_allow_private_origins(False)
    assert not is_safe_origin("https://localai.local")
    assert is_safe_origin("http://localhost:3000")


def test_is_safe_origin_explicit_list_when_private_disabled():
    set_allow_private_origins(False)
    set_extra_allowed_origins(["https://app.company.com"])
    assert is_safe_origin("https://app.company.com")
    assert not is_safe_origin("https://localai.local")


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
    assert is_safe_origin("http://127.0.0.1:18765")
    assert not is_safe_origin("http://evil.localhost")


class _OriginHandler:
    def __init__(self, origin):
        self.headers = {} if origin is None else {"Origin": origin}


def test_origin_is_forbidden_missing_and_loopback():
    assert origin_is_forbidden(_OriginHandler(None)) is False
    assert origin_is_forbidden(_OriginHandler("")) is False
    assert origin_is_forbidden(_OriginHandler("http://localhost:3000")) is False
    assert origin_is_forbidden(_OriginHandler("http://127.0.0.1:8080")) is False


def test_origin_is_forbidden_public_and_junk():
    assert origin_is_forbidden(_OriginHandler("http://example.com")) is True
    assert origin_is_forbidden(_OriginHandler("https://evil.example")) is True
    # Alphabet / length fail _deal_origin_ok — must not raise into is_safe_origin.
    assert origin_is_forbidden(_OriginHandler("http://example.com/?x")) is True
    assert origin_is_forbidden(_OriginHandler("h" * (DEAL_MAX_ORIGIN + 1))) is True
    assert origin_is_forbidden(_OriginHandler("http://host:99999")) is True
    assert origin_is_forbidden(_OriginHandler("http://host:abc")) is True
    assert origin_is_forbidden(_OriginHandler("http://host:-1")) is True


def test_bad_port_origin_is_unsafe_and_cors_headers_do_not_raise():
    assert is_safe_origin("http://host:99999") is False
    assert is_private_browser_origin("http://host:99999") is False

    class DummyHandler:
        def __init__(self, headers):
            self.headers = headers
            self.sent_headers = []

        def send_header(self, k, v):
            self.sent_headers.append((k, v))

    handler = DummyHandler({"Origin": "http://host:99999"})
    send_cors_headers(handler, preflight=False)
    headers_dict = dict(handler.sent_headers)
    assert "Access-Control-Allow-Origin" not in headers_dict


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


def _assert_forbidden_origin(err: urllib.error.HTTPError) -> None:
    assert err.code == 403
    assert read_http_error_body(err) == b""
    assert err.headers.get("Access-Control-Allow-Origin") is None
    assert err.headers.get("Access-Control-Allow-Methods") is None


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("http://example.com", id="test_options_mcp_unsafe_origin_no_allow_origin"),
        pytest.param("http://example.com/?x", id="test_options_mcp_junk_origin_is_403_not_500"),
        pytest.param("http://host:99999", id="test_options_mcp_bad_port_origin_is_403"),
    ],
)
def test_options_mcp_unsafe_origin_no_allow_origin(mcp_server, value):
    req = urllib.request.Request(
        f"{mcp_server}/mcp",
        method="OPTIONS",
        headers={
            "Origin": value,
            "Access-Control-Request-Method": "POST",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    _assert_forbidden_origin(exc_info.value)

def test_post_mcp_unsafe_origin_is_403_without_jsonrpc(mcp_server):
    before = mcp_session._mcp_session_id
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "evil", "version": "0"}},
    }
    req = urllib.request.Request(
        f"{mcp_server}/mcp",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", "https://evil.example")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    err = exc_info.value
    _assert_forbidden_origin(err)
    assert err.headers.get("Mcp-Session-Id") is None
    assert mcp_session._mcp_session_id == before


def test_post_mcp_no_origin_still_succeeds(mcp_server):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    req = urllib.request.Request(
        f"{mcp_server}/mcp",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
    )
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 200
        body = json.loads(response.read().decode("utf-8"))
        assert body.get("result") == {}


def test_get_health_unsafe_origin_is_403(mcp_server):
    req = urllib.request.Request(
        f"{mcp_server}/health",
        method="GET",
        headers={"Origin": "https://evil.example"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    _assert_forbidden_origin(exc_info.value)


def test_post_debug_unsafe_origin_is_403(mcp_server):
    req = urllib.request.Request(
        f"{mcp_server}/debug",
        method="POST",
        data=json.dumps({"action": "services"}).encode("utf-8"),
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", "https://evil.example")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    _assert_forbidden_origin(exc_info.value)


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


# Darwin urllib: RST after a short 400 (errno 54). Linux 104 / Windows 10054.
_HTTP_BODY_RESET_ERRNOS = frozenset({54, 104, 10054})


def read_http_error_body(err: urllib.error.HTTPError) -> bytes:
    """Read an HTTPError body; a peer RST after status 400 must not fail the test."""
    try:
        return err.read() or b""
    except (ConnectionResetError, BrokenPipeError, TimeoutError):
        return b""
    except OSError as exc:
        if getattr(exc, "errno", None) in _HTTP_BODY_RESET_ERRNOS:
            return b""
        raise


def test_read_http_error_body_survives_darwin_reset():
    """Status 400 is enough; Darwin-style RST while reading the body is not a regression."""
    err = urllib.error.HTTPError("http://127.0.0.1/mcp", 400, "Bad Request", hdrs={}, fp=None)

    def _raise_reset() -> bytes:
        raise ConnectionResetError(54, "Connection reset by peer")

    err.read = _raise_reset  # type: ignore[method-assign]
    assert err.code == 400
    assert read_http_error_body(err) == b""


def test_read_http_error_body_returns_json_when_available():
    payload = b'{"jsonrpc":"2.0","error":{"code":-32600}}'
    err = urllib.error.HTTPError("http://127.0.0.1/mcp", 400, "Bad Request", hdrs={}, fp=None)
    err.read = lambda: payload  # type: ignore[method-assign]
    assert json.loads(read_http_error_body(err).decode("utf-8"))["error"]["code"] == -32600


def test_post_unsupported_protocol_version(mcp_server):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{mcp_server}/mcp", method="POST", data=data_bytes)
    req.add_header("Content-Type", "application/json")
    req.add_header("Mcp-Protocol-Version", "2099-01-01")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    err = exc_info.value
    assert err.code == 400
    raw = read_http_error_body(err)
    if raw:
        body = json.loads(raw.decode("utf-8"))
        assert body.get("error", {}).get("code") == -32600


def test_dns_rebinding_protection_rejects_untrusted_host_header(mcp_server):
    """DNS-rebinding protection: requests with untrusted Host header must return 403 Forbidden."""
    parsed = urllib.parse.urlparse(mcp_server)

    # Untrusted Host header
    req = urllib.request.Request(
        f"{mcp_server}/health",
        method="GET",
        headers={"Host": "evil.attacker.com"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    assert exc_info.value.code == 403

    # Valid loopback Host header with port
    req_good = urllib.request.Request(
        f"{mcp_server}/health",
        method="GET",
        headers={"Host": f"localhost:{parsed.port}"},
    )
    with urllib.request.urlopen(req_good, timeout=5) as resp:
        assert resp.status == 200


def test_dns_rebinding_protection_options_preflight(mcp_server):
    """OPTIONS preflight with untrusted Host header must return 403 Forbidden."""
    req = urllib.request.Request(
        f"{mcp_server}/mcp",
        method="OPTIONS",
        headers={
            "Host": "evil.attacker.com",
            "Access-Control-Request-Method": "POST",
            "Origin": "http://localhost:3000",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    assert exc_info.value.code == 403


def test_dns_rebinding_protection_allows_configured_tunnel_host_live(mcp_server, monkeypatch):
    """Configured tunnel host must be permitted through the Host header check on live server."""
    monkeypatch.setattr("plugin.mcp.cors.get_configured_tunnel_host", lambda: "tunnel.example.com")
    req = urllib.request.Request(
        f"{mcp_server}/health",
        method="GET",
        headers={"Host": "tunnel.example.com"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200

    # Untrusted host still rejected even with tunnel host configured
    req_bad = urllib.request.Request(
        f"{mcp_server}/health",
        method="GET",
        headers={"Host": "evil.attacker.com"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req_bad, timeout=5)
    assert exc_info.value.code == 403


def test_is_safe_host_allows_configured_bind_host():
    from plugin.mcp.cors import is_safe_host

    assert is_safe_host("mybox.local:8766", bind_host="mybox.local")
    assert is_safe_host("192.168.1.5:8766", bind_host="192.168.1.5")
    assert not is_safe_host("evil.example:8766", bind_host="mybox.local")
    assert not is_safe_host("evil.example:8766", bind_host="0.0.0.0")


def test_get_configured_tunnel_host_reads_shared_tunnel_public_url(monkeypatch):
    import plugin.mcp as mcp_pkg
    from plugin.mcp.cors import get_configured_tunnel_host

    class _Tunnel:
        public_url = "https://abc.trycloudflare.com"

    monkeypatch.setattr(mcp_pkg, "_shared_tunnel", _Tunnel(), raising=False)
    assert get_configured_tunnel_host() == "abc.trycloudflare.com"
    monkeypatch.setattr(mcp_pkg, "_shared_tunnel", None, raising=False)
    assert get_configured_tunnel_host() is None


def test_drain_request_body_reads_up_to_content_length():
    from io import BytesIO
    from plugin.mcp.cors import _drain_request_body

    class _FakeHandler:
        def __init__(self, content: bytes):
            self.headers = {"Content-Length": str(len(content))}
            self.rfile = BytesIO(content)

    handler = _FakeHandler(b"hello world")
    _drain_request_body(handler)
    assert handler.rfile.read() == b""

    # 100 KiB body is drained (> 64 KiB cap, <= MCP_HTTP_MAX_BODY_BYTES).
    large_handler = _FakeHandler(b"x" * 102400)
    _drain_request_body(large_handler)
    assert large_handler.rfile.read() == b""


def test_reload_cors_config_failure_propagates():
    services = MagicMock()
    services.config.proxy_for.side_effect = RuntimeError("config")
    set_allow_private_origins(True)
    set_extra_allowed_origins(["https://evil.example"])
    with pytest.raises(RuntimeError, match="config"):
        reload_cors_policy_from_config(services)
    assert get_allow_private_origins() is True
    assert "https://evil.example" in get_extra_allowed_origins()


def test_reload_cors_missing_private_key_stays_on():
    services = MagicMock()
    proxy = MagicMock()

    def _get(key, default=None):
        return None

    proxy.get.side_effect = _get
    services.config.proxy_for.return_value = proxy
    reload_cors_policy_from_config(services)
    assert get_allow_private_origins() is True


def test_drain_request_body_swallows_valueerror_and_oserror():
    from plugin.mcp.cors import _drain_request_body

    class _BadLength:
        headers = {"Content-Length": "nope"}
        rfile = None

    _drain_request_body(_BadLength())

    class _Reset:
        headers = {"Content-Length": "4"}

        class _R:
            def read(self, n):
                raise OSError("reset")

        rfile = _R()

    _drain_request_body(_Reset())


def test_drain_request_body_other_error_propagates():
    from plugin.mcp.cors import _drain_request_body

    class _Bug:
        headers = {"Content-Length": "4"}

        class _R:
            def read(self, n):
                raise RuntimeError("bug")

        rfile = _R()

    with pytest.raises(RuntimeError, match="bug"):
        _drain_request_body(_Bug())


def test_tunnel_host_lookup_does_not_swallow(monkeypatch):
    import plugin.mcp as mcp_pkg
    from plugin.mcp.cors import get_configured_tunnel_host

    class _Boom:
        @property
        def public_url(self):
            raise RuntimeError("broken tunnel")

    monkeypatch.setattr(mcp_pkg, "_shared_tunnel", _Boom(), raising=False)
    with pytest.raises(RuntimeError, match="broken tunnel"):
        get_configured_tunnel_host()


def test_normalize_origins_list_bad_and_empty_inputs():
    # Bad or empty origins config values must not raise PreContractError.
    assert normalize_origins_list({}) == []
    assert normalize_origins_list(123) == []
    assert normalize_origins_list(False) == []
    assert normalize_origins_list("") == []
    assert normalize_origins_list([]) == []
    assert normalize_origins_list(None) == []
    assert normalize_origins_list(["https://example.com", 123, ""]) == ["https://example.com"]


@pytest.mark.parametrize(
    "bad_or_empty",
    [
        {},
        "",
        [],
        None,
        123,
        False,
        {"not": "a list"},
        ["https://valid.example.com", "", 42, "not a url"],
    ],
)
def test_reload_cors_bad_or_empty_origins_does_not_crash(bad_or_empty):
    services = MagicMock()
    proxy = MagicMock()
    proxy.get.side_effect = lambda key, default=None: bad_or_empty if key == "cors_allowed_origins" else None
    services.config.proxy_for.return_value = proxy

    # Bad or empty config values must not crash module reload or raise PreContractError.
    reload_cors_policy_from_config(services)
    if bad_or_empty == ["https://valid.example.com", "", 42, "not a url"]:
        assert "https://valid.example.com" in get_extra_allowed_origins()
    else:
        assert get_extra_allowed_origins() == frozenset()


