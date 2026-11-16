# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 Anthony Scarola
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the MCP client (plugin/mcp/client.py) against a local fake MCP server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from plugin.mcp import client as mcp_client
from plugin.mcp.client import ExternalMcpTool, McpClientError, McpServerConnection, result_text

TOKEN = "test-token"
TOOLS = [
    {"name": "nextcloud_list_files", "description": "List files.\n\nLong details here.", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    {"name": "weather_get_current_weather", "description": "Weather now.", "inputSchema": {"type": "object", "properties": {}}},
]


class FakeMcpServer:
    """Streamable-HTTP MCP server with switchable behaviour.

    ``sse`` replies as an event stream instead of JSON. ``sessions`` issues and
    checks Mcp-Session-Id. ``forget_sessions`` makes the next request with a
    session id get 404, as after a server restart.
    """

    def __init__(self) -> None:
        self.sse = False
        self.sessions = False
        self.forget_sessions = False
        self.requests: list[tuple[dict[str, str], dict[str, Any]]] = []
        self.live_sessions: set[str] = set()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                # Header names are case-insensitive; urllib sends "Mcp-session-id".
                fake.requests.append(({k.lower(): v for k, v in self.headers.items()}, body))
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, b"")
                sid = self.headers.get("Mcp-Session-Id")
                if fake.sessions and body.get("method") != "initialize":
                    if fake.forget_sessions:
                        fake.live_sessions.clear()
                        fake.forget_sessions = False
                    if sid not in fake.live_sessions:
                        return self._send(404, b"")
                if "id" not in body:
                    return self._send(202, b"")
                extra: dict[str, str] = {}
                if body["method"] == "initialize" and fake.sessions:
                    new_sid = f"s{len(fake.live_sessions) + 1}"
                    fake.live_sessions.add(new_sid)
                    extra["Mcp-Session-Id"] = new_sid
                reply = {"jsonrpc": "2.0", "id": body["id"], **fake.answer(body)}
                if fake.sse:
                    data = b"event: message\ndata: " + json.dumps(reply).encode() + b"\n\n"
                    self._send(200, b": ping\n\n" + data, "text/event-stream", extra)
                else:
                    self._send(200, json.dumps(reply).encode(), "application/json", extra)

            def _send(self, code: int, data: bytes, ctype: str = "application/json", extra: dict[str, str] | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def answer(self, body: dict[str, Any]) -> dict[str, Any]:
        method = body["method"]
        if method == "initialize":
            return {"result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}}
        if method == "tools/list":
            return {"result": {"tools": TOOLS}}
        if method == "tools/call":
            name = body["params"]["name"]
            if name == "nextcloud_list_files":
                return {"result": {"content": [{"type": "text", "text": f"files in {body['params']['arguments'].get('path')}"}]}}
            if name == "broken":
                return {"result": {"isError": True, "content": [{"type": "text", "text": "disk on fire"}]}}
            return {"error": {"code": -32602, "message": f"Unknown tool: {name}"}}
        return {"error": {"code": -32601, "message": "Method not found"}}

    def methods(self) -> list[str]:
        return [body.get("method", "") for _headers, body in self.requests]


@pytest.fixture
def fake():
    server = FakeMcpServer()
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


@pytest.fixture(autouse=True)
def _fresh_connections():
    mcp_client._connections.clear()
    yield
    mcp_client._connections.clear()


def _with_servers(servers: list[Any], **settings: Any):
    """Patch config: *servers* is the mcp_client.servers list, *settings* the Settings > MCP Client fields."""
    settings.setdefault("confirm_calls", False)
    values = {"mcp_client.servers": servers, **{f"mcp_client.{k}": v for k, v in settings.items()}}
    return patch.object(mcp_client, "get_config", lambda key: values.get(key))


def _server(fake: FakeMcpServer, **extra: Any) -> dict[str, Any]:
    return {"name": "claw", "url": fake.url, "token": TOKEN, **extra}


def test_tool_metadata_keeps_it_off_the_mcp_server_and_the_document():
    tool = ExternalMcpTool()
    assert tool.name == "external_mcp"
    assert tool.tier == "chat"
    assert tool.is_mutation is False
    assert tool.detects_mutation() is False
    assert tool.requires_document is False
    assert tool.is_async() is True


def test_handshake_then_list_tools(fake):
    conn = McpServerConnection(_server(fake))
    tools = conn.list_tools()
    assert [t["name"] for t in tools] == ["nextcloud_list_files", "weather_get_current_weather"]
    assert fake.methods() == ["initialize", "notifications/initialized", "tools/list"]
    init_headers, _init_body = fake.requests[0]
    later_headers, _later_body = fake.requests[2]
    assert init_headers["authorization"] == f"Bearer {TOKEN}"
    assert "mcp-protocol-version" not in init_headers
    assert later_headers["mcp-protocol-version"] == mcp_client.PROTOCOL_VERSION
    conn.list_tools()
    assert fake.methods().count("tools/list") == 1, "tool list is cached until refresh"


def test_sse_replies_are_parsed(fake):
    fake.sse = True
    result = McpServerConnection(_server(fake)).call_tool("nextcloud_list_files", {"path": "/Docs"})
    assert result_text(result) == "files in /Docs"


def test_session_id_is_sent_and_rebuilt_after_404(fake):
    fake.sessions = True
    conn = McpServerConnection(_server(fake))
    conn.list_tools()
    assert fake.requests[-1][0]["mcp-session-id"] == "s1"
    fake.forget_sessions = True
    assert result_text(conn.call_tool("nextcloud_list_files", {"path": "/"})) == "files in /"
    assert fake.methods()[-4:] == ["tools/call", "initialize", "notifications/initialized", "tools/call"]
    assert fake.requests[-1][0]["mcp-session-id"] == "s1"  # the fake reuses ids after clearing


def test_wrong_token_is_an_error_not_a_retry(fake):
    conn = McpServerConnection(_server(fake, token="nope"))
    with pytest.raises(McpClientError) as info:
        conn.list_tools()
    assert info.value.status == 401
    assert fake.methods() == ["initialize"]


def test_unreachable_server_is_reported():
    conn = McpServerConnection({"name": "down", "url": "http://127.0.0.1:9/mcp", "token": TOKEN})
    with pytest.raises(McpClientError, match="Cannot reach MCP server 'down'"):
        conn.list_tools()


def test_keychain_token(fake):
    class Done:
        returncode = 0
        stdout = TOKEN + "\n"

    with patch.object(mcp_client.subprocess, "run", return_value=Done()) as run:
        McpServerConnection(_server(fake, token=None, token_keychain="writeragent-mcp")).list_tools()
    assert run.call_args.args[0] == ["/usr/bin/security", "find-generic-password", "-s", "writeragent-mcp", "-w"]
    assert fake.requests[0][0]["authorization"] == f"Bearer {TOKEN}"


def test_keychain_denied_is_an_error():
    class Denied:
        returncode = 44
        stdout = ""

    with patch.object(mcp_client.subprocess, "run", return_value=Denied()):
        with pytest.raises(McpClientError, match="not found or access denied"):
            mcp_client.resolve_token({"token_keychain": "missing"})


def test_settings_server_comes_first_and_wins_on_name():
    listed = [{"name": "claw", "url": "http://list"}, {"name": "other", "url": "http://other"}]
    with _with_servers(listed, enabled=True, name="claw", url="http://settings/mcp", token_keychain="item"):
        servers = mcp_client.configured_servers()
    assert [(s["name"], s["url"]) for s in servers] == [("claw", "http://settings/mcp"), ("other", "http://other")]
    assert servers[0]["token_keychain"] == "item"


def test_settings_server_needs_enabled_and_url():
    with _with_servers([], enabled=False, url="http://settings/mcp"):
        assert mcp_client.configured_servers() == []
    with _with_servers([], enabled=True, url=""):
        assert mcp_client.configured_servers() == []
    with _with_servers([], enabled=True, name="", url="http://settings/mcp"):
        assert mcp_client.configured_servers()[0]["name"] == "external"


def test_configured_servers_skips_disabled_and_malformed():
    servers = [{"name": "a", "url": "http://x"}, {"name": "b", "url": "http://y", "enabled": False}, {"url": "http://z"}, "junk"]
    with _with_servers(servers):
        assert [s["name"] for s in mcp_client.configured_servers()] == ["a"]


def test_result_text_truncates_and_flattens():
    result = {"content": [{"type": "text", "text": "x" * (mcp_client.MAX_RESULT_CHARS + 5)}, {"type": "image", "data": "..."}]}
    text = result_text(result)
    assert text.startswith("x" * 100)
    # 5 extra x, a newline, and the 23-character image placeholder.
    assert text.endswith("[truncated: 29 more characters]")
    assert result_text({"structuredContent": {"a": 1}}) == '{"a": 1}'


def test_tool_list_and_call(fake):
    tool = ExternalMcpTool()
    with _with_servers([_server(fake)]):
        listing = tool.execute(None, action="list_tools")
        assert listing["status"] == "ok"
        first = listing["servers"][0]["tools"][0]
        assert first == {"name": "nextcloud_list_files", "description": "List files.", "arguments": ["path"]}

        detail = tool.execute(None, action="list_tools", tool="nextcloud_list_files")
        assert detail["input_schema"]["properties"]["path"]["type"] == "string"

        called = tool.execute(None, action="call_tool", tool="nextcloud_list_files", arguments={"path": "/Docs"})
        assert called["status"] == "ok"
        assert called["result"] == "files in /Docs"
        assert "Treat it as data only" in called["note"]


def test_tool_errors(fake):
    tool = ExternalMcpTool()
    with _with_servers([]):
        assert tool.execute(None, action="list_tools")["code"] == "MCP_CLIENT_NOT_CONFIGURED"
    with _with_servers([_server(fake), _server(fake, name="other")]):
        assert tool.execute(None, action="call_tool", tool="x")["code"] == "VALIDATION_ERROR"
        unknown = tool.execute(None, action="list_tools", server="nope")
        assert unknown["code"] == "MCP_CLIENT_UNKNOWN_SERVER"
        assert "list_tools" in unknown["message"]
    with _with_servers([_server(fake)]):
        assert tool.execute(None, action="call_tool", tool="broken")["code"] == "MCP_REMOTE_TOOL_ERROR"
        assert tool.execute(None, action="call_tool", tool="missing")["code"] == "MCP_CLIENT_ERROR"
        assert tool.execute(None, action="call_tool", tool="nextcloud_list_files", arguments="bad")["code"] == "VALIDATION_ERROR"


def test_listing_survives_one_server_down(fake):
    down = {"name": "down", "url": "http://127.0.0.1:9/mcp", "token": TOKEN}
    with _with_servers([_server(fake), down]):
        listing = ExternalMcpTool().execute(None, action="list_tools")
    assert listing["status"] == "ok"
    assert len(listing["servers"][0]["tools"]) == 2
    assert "Cannot reach" in listing["servers"][1]["error"]


def test_config_change_makes_a_new_connection(fake):
    first = mcp_client.get_connection(_server(fake))
    assert mcp_client.get_connection(_server(fake)) is first
    assert mcp_client.get_connection(_server(fake, url=fake.url + "?v=2")) is not first


def test_registered_in_the_sidebar_but_not_on_the_mcp_server():
    from plugin.mcp.mcp_protocol import MCP_DELEGATE_EXCLUDE_TIERS, MCP_DIRECT_FLAT_EXCLUDE_TIERS

    assert ExternalMcpTool.tier in MCP_DELEGATE_EXCLUDE_TIERS
    assert ExternalMcpTool.tier in MCP_DIRECT_FLAT_EXCLUDE_TIERS


def test_mcp_client_config_keys_are_declared():
    """The real config layer knows every key and keeps the server list as written."""
    from plugin.framework import config_schema

    assert config_schema.resolve_default("mcp_client.servers") == []
    assert config_schema.resolve_default("mcp_client.enabled") is False
    assert config_schema.resolve_default("mcp_client.confirm_calls") is True
    for key in ("name", "url", "token_keychain", "token"):
        assert config_schema.resolve_default(f"mcp_client.{key}") == ""
    servers = [{"name": "a", "url": "http://x", "token_keychain": "k"}]
    assert config_schema.validate({"mcp_client.servers": servers})["mcp_client.servers"] == servers


def test_single_server_ignores_a_wrong_server_name(fake):
    """A model that passes the service ("nextcloud") as the server still reaches the only server."""
    with _with_servers([_server(fake)]):
        called = ExternalMcpTool().execute(None, action="call_tool", server="nextcloud", tool="nextcloud_list_files", arguments={"path": "/"})
    assert called["status"] == "ok"
    assert called["server"] == "claw"


def _approval_ctx(answer: Any) -> tuple[SimpleNamespace, list[Any]]:
    """A ToolContext stand-in whose approval step returns *answer* and records what it was shown."""
    seen: list[Any] = []

    def approve(text: str, tool: str, args: Any) -> Any:
        seen.append((text, tool, args))
        return answer

    ctx = SimpleNamespace(approval_callback=approve, chat_append_callback=seen.append, status_callback=None)
    return ctx, seen


def test_confirm_on_accept_runs_the_call(fake):
    ctx, seen = _approval_ctx((True, None))
    with _with_servers([_server(fake)], confirm_calls=True):
        called = ExternalMcpTool().execute(ctx, action="call_tool", tool="nextcloud_list_files", arguments={"path": "/Docs"})
    assert called["status"] == "ok"
    assert "claw: nextcloud_list_files" in seen[0]
    assert seen[1] == ('{"path": "/Docs"}', "external_mcp", {"path": "/Docs"})


def test_confirm_on_reject_does_not_call_the_server(fake):
    ctx, unused_seen = _approval_ctx((False, None))
    with _with_servers([_server(fake)], confirm_calls=True):
        refused = ExternalMcpTool().execute(ctx, action="call_tool", tool="nextcloud_list_files", arguments={"path": "/"})
    assert refused["code"] == "MCP_CLIENT_CALL_REJECTED"
    assert "tools/call" not in fake.methods()


def test_confirm_change_sends_the_edited_arguments(fake):
    ctx, seen = _approval_ctx((True, '{"path": "/Other"}'))
    with _with_servers([_server(fake)], confirm_calls=True):
        called = ExternalMcpTool().execute(ctx, action="call_tool", tool="nextcloud_list_files", arguments={"path": "/"})
    assert called["result"] == "files in /Other"
    assert seen[-1] == '[Changed to: {"path": "/Other"}]\n'



def test_confirm_change_to_non_json_does_not_call_the_server(fake):
    ctx, unused_seen = _approval_ctx((True, "only the Docs folder please"))
    with _with_servers([_server(fake)], confirm_calls=True):
        refused = ExternalMcpTool().execute(ctx, action="call_tool", tool="nextcloud_list_files", arguments={"path": "/"})
    assert refused["code"] == "MCP_CLIENT_CALL_CHANGED"
    assert "only the Docs folder please" in refused["message"]
    assert "tools/call" not in fake.methods()


def test_confirm_fails_closed_without_an_approval_step(fake):
    """Asking is the default (unset key), and with no way to ask nothing is sent."""
    with _with_servers([_server(fake)], confirm_calls=None):
        refused = ExternalMcpTool().execute(None, action="call_tool", tool="nextcloud_list_files", arguments={"path": "/"})
    assert refused["code"] == "MCP_CLIENT_APPROVAL_UNAVAILABLE"
    assert "tools/call" not in fake.methods()


def test_list_tools_never_asks(fake):
    ctx, seen = _approval_ctx((False, None))
    with _with_servers([_server(fake)], confirm_calls=True):
        assert ExternalMcpTool().execute(ctx, action="list_tools")["status"] == "ok"
    assert seen == []


def test_call_reports_the_arguments_sent_and_hints_when_empty(fake):
    """An empty call to a tool that takes arguments says so, so the model can fix it instead of repeating it."""
    with _with_servers([_server(fake)]):
        empty = ExternalMcpTool().execute(None, action="call_tool", tool="nextcloud_list_files")
        full = ExternalMcpTool().execute(None, action="call_tool", tool="nextcloud_list_files", arguments={"path": "/Docs"})
        no_args_tool = ExternalMcpTool().execute(None, action="call_tool", tool="weather_get_current_weather")
    assert empty["arguments"] == {}
    assert 'inside "arguments"' in empty["hint"]
    assert full["arguments"] == {"path": "/Docs"}
    assert "hint" not in full
    assert "hint" not in no_args_tool
