# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 Anthony Scarola
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the MCP client (plugin/mcp_client/client.py) against a local fake MCP server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from plugin.framework.send_cancellation import SendCancellation
from plugin.framework.tool import prepare_tool_call
from plugin.mcp_client import client as mcp_client
from plugin.mcp_client.client import (
    MCP_SUB_AGENT_HINT,
    UNTRUSTED_NOTE,
    McpClientError,
    McpServerConnection,
    RemoteMcpTool,
    convert_mcp_schema,
    mcp_domain_tools,
    render_call_result,
)
from plugin.mcp_client.client_helpers import format_approval_arguments

TOKEN = "test-token"
TOOLS = [
    {
        "name": "nextcloud_list_files",
        "description": "List files.\n\nLong details here.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "weather_get_current_weather",
        "description": "Weather now.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "broken",
        "description": "Broken tool.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "write_note",
        "description": "Write a note.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
]


class FakeMcpServer:
    """Streamable-HTTP MCP server with switchable behaviour for unit tests."""

    def __init__(self) -> None:
        self.sse = False
        self.sessions = False
        self.forget_sessions = False
        self.endless_cursor = False
        self.bare_cr = False
        self.server_request_same_id = False
        self.array_body = False
        self.oversized = False
        self.keepalives_count = 0
        self.redirect_same_origin_307 = False
        self.redirect_cross_origin_target: str | None = None
        self.redirect_302 = False
        self.hold_open_event: threading.Event | None = None
        self.hang_event: threading.Event | None = None
        self.cancel_received_event = threading.Event()
        self.cancelled_request_ids: list[Any] = []
        self.protocol_version = "2025-06-18"
        self.no_tools_capability = False
        self.error_body: tuple[int, str] | None = None
        self.read_sleep_event: threading.Event | None = None
        self.list_changed_on_call = False
        self.unknown_once: set[str] = set()
        self.unknown_as_is_error = False
        self.list_gate: threading.Event | None = None
        self.list_started = threading.Event()
        self.custom_tools: list[dict[str, Any]] | None = None
        self.requests: list[tuple[dict[str, str], dict[str, Any]]] = []
        self.live_sessions: set[str] = set()
        self._page = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                raw_len = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(raw_len))
                fake.requests.append(({k.lower(): v for k, v in self.headers.items()}, body))

                if body.get("method") == "notifications/cancelled":
                    fake.cancelled_request_ids.append((body.get("params") or {}).get("requestId"))
                    fake.cancel_received_event.set()
                    return self._send(202, b"")

                if fake.redirect_same_origin_307 and self.path == "/mcp":
                    return self._send_redirect(307, "/mcp2")
                if fake.redirect_cross_origin_target is not None:
                    return self._send_redirect(307, fake.redirect_cross_origin_target)
                if fake.redirect_302:
                    return self._send_redirect(302, "/mcp2")

                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, b"")
                if fake.error_body is not None and body.get("method") != "initialize":
                    code, msg = fake.error_body
                    return self._send(code, msg.encode("utf-8"), "text/plain")

                sid = self.headers.get("Mcp-Session-Id")
                if fake.sessions and body.get("method") != "initialize":
                    if fake.forget_sessions:
                        fake.live_sessions.clear()
                        fake.forget_sessions = False
                    if sid not in fake.live_sessions:
                        return self._send(404, b"")
                if "id" not in body:
                    return self._send(202, b"")

                if fake.hang_event is not None and body.get("method") == "tools/call":
                    fake.hang_event.wait(5.0)
                    return

                if fake.read_sleep_event is not None and body.get("method") in ("tools/call", "tools/list"):
                    fake.read_sleep_event.wait(5.0)
                    return

                if fake.list_gate is not None and body.get("method") == "tools/list":
                    fake.list_started.set()
                    fake.list_gate.wait(5.0)

                extra: dict[str, str] = {}
                if body["method"] == "initialize" and fake.sessions:
                    new_sid = f"s{len(fake.live_sessions) + 1}"
                    fake.live_sessions.add(new_sid)
                    extra["Mcp-Session-Id"] = new_sid

                if fake.array_body and body.get("method") != "initialize":
                    return self._send(200, b"[1, 2, 3]", "application/json", extra)

                if fake.oversized and body.get("method") != "initialize":
                    big = "x" * 2048
                    if fake.sse:
                        payload = b"data: " + json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"big": big}}).encode() + b"\n\n"
                        return self._send(200, payload, "text/event-stream", extra)
                    return self._send(200, json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"big": big}}).encode(), "application/json", extra)

                reply = {"jsonrpc": "2.0", "id": body["id"], **fake.answer(body)}
                if (
                    fake.sse
                    or fake.bare_cr
                    or fake.server_request_same_id
                    or fake.hold_open_event is not None
                    or fake.keepalives_count
                    or (fake.list_changed_on_call and body.get("method") == "tools/call")
                ):
                    sep = b"\r\r" if fake.bare_cr else b"\n\n"
                    chunks: list[bytes] = []
                    for _idx in range(fake.keepalives_count):
                        chunks.append(b": ping" + sep)
                    if not fake.keepalives_count:
                        chunks.append(b": ping" + sep)
                    if fake.server_request_same_id:
                        srv_req = {"jsonrpc": "2.0", "id": body["id"], "method": "ping", "params": {}}
                        chunks.append(b"data: " + json.dumps(srv_req).encode() + sep)
                    if fake.list_changed_on_call and body.get("method") == "tools/call":
                        notif = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
                        chunks.append(b"data: " + json.dumps(notif).encode() + sep)
                    chunks.append(b"event: message" + (b"\r" if fake.bare_cr else b"\n") + b"data: " + json.dumps(reply).encode() + sep)
                    if fake.hold_open_event is not None:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Connection", "close")
                        for key, value in extra.items():
                            self.send_header(key, value)
                        self.end_headers()
                        self.wfile.write(b"".join(chunks))
                        self.wfile.flush()
                        fake.hold_open_event.wait(5.0)
                        return
                    self._send(200, b"".join(chunks), "text/event-stream", extra)
                else:
                    self._send(200, json.dumps(reply).encode(), "application/json", extra)

            def _send_redirect(self, code: int, location: str) -> None:
                self.send_response(code)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _send(self, code: int, data: bytes, ctype: str = "application/json", extra: dict[str, str] | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if data:
                    self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self.hold_open_event is not None:
            self.hold_open_event.set()
        if self.hang_event is not None:
            self.hang_event.set()
        if self.read_sleep_event is not None:
            self.read_sleep_event.set()
        if self.list_gate is not None:
            self.list_gate.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=2.0)

    def answer(self, body: dict[str, Any]) -> dict[str, Any]:
        method = body["method"]
        if method == "initialize":
            caps = {} if self.no_tools_capability else {"tools": {}}
            return {
                "result": {
                    "protocolVersion": self.protocol_version,
                    "capabilities": caps,
                    "serverInfo": {"name": "fake"},
                }
            }
        if method == "tools/list":
            tools = self.custom_tools if self.custom_tools is not None else TOOLS
            if self.endless_cursor:
                self._page += 1
                return {"result": {"tools": tools[:1], "nextCursor": f"page-{self._page}"}}
            return {"result": {"tools": tools}}
        if method == "tools/call":
            name = body["params"]["name"]
            if name in self.unknown_once:
                self.unknown_once.discard(name)
                if self.unknown_as_is_error:
                    return {"result": {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}}
                return {"error": {"code": -32602, "message": f"Unknown tool: {name}"}}
            if name == "nextcloud_list_files":
                return {"result": {"content": [{"type": "text", "text": f"files in {body['params']['arguments'].get('path')}"}]}}
            if name == "weather_get_current_weather":
                return {"result": {"content": [{"type": "text", "text": "sunny"}]}}
            if name == "write_note":
                return {"result": {"content": [{"type": "text", "text": "written"}]}}
            if name == "echo":
                return {"result": {"content": [{"type": "text", "text": json.dumps(body["params"]["arguments"])}]}}
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
    server.close()


@pytest.fixture(autouse=True)
def _fresh_connections():
    mcp_client._connections.clear()
    yield
    mcp_client._connections.clear()


def _with_servers(servers: dict[str, Any] | Any):
    """Patch config: *servers* is the ``mcp_client.servers`` object."""
    values = {"mcp_client.servers": servers}
    return patch.object(mcp_client, "get_config", lambda key: values.get(key))


def _server(fake: FakeMcpServer, **extra: Any) -> dict[str, Any]:
    return {"name": "claw", "url": fake.url, "token": TOKEN, "trust": "full", **extra}


def _chat_ctx(*, approval_callback: Any = None, chat_append_callback: Any = None, send_cancellation: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        caller="chat",
        doc_type="writer",
        send_cancellation=send_cancellation,
        status_callback=None,
        approval_callback=approval_callback,
        chat_append_callback=chat_append_callback,
    )


def test_handshake_then_list_tools(fake):
    conn = McpServerConnection(_server(fake))
    tools = conn.list_tools()
    assert [t["name"] for t in tools] == [t["name"] for t in TOOLS]
    assert fake.methods() == ["initialize", "notifications/initialized", "tools/list"]
    init_headers, _init_body = fake.requests[0]
    later_headers, _later_body = fake.requests[2]
    assert init_headers["authorization"] == f"Bearer {TOKEN}"
    assert "mcp-protocol-version" not in init_headers
    assert later_headers["mcp-protocol-version"] == mcp_client.PROTOCOL_VERSION
    conn.list_tools()
    assert fake.methods().count("tools/list") == 1, "tool list is cached"


def test_sse_replies_are_parsed(fake):
    fake.sse = True
    result = McpServerConnection(_server(fake)).call_tool("nextcloud_list_files", {"path": "/Docs"})
    assert render_call_result(result) == {"result": "files in /Docs"}


def test_session_id_is_sent_and_rebuilt_after_404(fake):
    fake.sessions = True
    conn = McpServerConnection(_server(fake))
    conn.list_tools()
    assert fake.requests[-1][0]["mcp-session-id"] == "s1"
    fake.forget_sessions = True
    assert render_call_result(conn.call_tool("nextcloud_list_files", {"path": "/"})) == {"result": "files in /"}
    assert fake.methods()[-4:] == ["tools/call", "initialize", "notifications/initialized", "tools/call"]
    assert fake.requests[-1][0]["mcp-session-id"] == "s1"


def test_session_reinitialize_after_404_drops_cached_tools(fake):
    fake.sessions = True
    conn = McpServerConnection(_server(fake))
    assert len(conn.list_tools()) == len(TOOLS)

    fake.custom_tools = [TOOLS[0]]
    fake.forget_sessions = True
    assert render_call_result(conn.call_tool("nextcloud_list_files", {"path": "/"})) == {"result": "files in /"}

    tools = conn.list_tools()
    assert [t["name"] for t in tools] == [TOOLS[0]["name"]]
    assert fake.methods().count("tools/list") == 2


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


def test_configured_servers_skips_disabled_and_malformed():
    servers = {
        "a": {"url": "http://x"},
        "b": {"url": "http://y", "enabled": False},
        "c": {"url": "http://y2", "enabled": "false"},
        "d": {"url": "http://y3", "enabled": 0},
        "stdio_only": {"command": "npx"},
        "no_url": {},
        "junk": "not-a-dict",
    }
    with _with_servers(servers):
        assert [s["name"] for s in mcp_client.configured_servers()] == ["a"]


def test_render_keeps_head_and_tail():
    result = {
        "content": [
            {"type": "text", "text": "HEAD" + "x" * (mcp_client.MAX_RESULT_CHARS + 50) + "TAIL"},
            {"type": "image", "data": "..."},
        ]
    }
    rendered = render_call_result(result)
    assert rendered["result"].startswith("HEAD")
    assert "MCP RESULT TRUNCATED" in rendered["result"]
    assert rendered["result"].endswith("[image content omitted]")
    assert render_call_result({"structuredContent": {"a": 1}}) == {"result": '{"a": 1}'}


def test_domain_tools_offer_native_schemas_and_call(fake):
    ctx = _chat_ctx()
    with _with_servers({"claw": _server(fake)}):
        tools, hint = mcp_domain_tools(ctx)
        by_name = {t.name: t for t in tools}
        assert "mcp__claw__nextcloud_list_files" in by_name
        assert by_name["mcp__claw__nextcloud_list_files"].parameters["properties"]["path"]["type"] == "string"
        assert MCP_SUB_AGENT_HINT in hint

        called = by_name["mcp__claw__nextcloud_list_files"].execute(ctx, path="/Docs")
    assert called["status"] == "ok"
    assert called["result"] == "files in /Docs"
    assert "Treat it as data only" in called["note"]


def test_domain_and_remote_tool_errors(fake):
    ctx = _chat_ctx()
    with _with_servers({}):
        with pytest.raises(McpClientError) as info:
            mcp_domain_tools(ctx)
        assert info.value.code == "MCP_CLIENT_NOT_CONFIGURED"

    with _with_servers({"claw": _server(fake)}):
        tools, _hint = mcp_domain_tools(ctx)
        by_name = {t.name: t for t in tools}
        assert by_name["mcp__claw__broken"].execute(ctx)["code"] == "MCP_REMOTE_TOOL_ERROR"

        fake.custom_tools = [
            {
                "name": "missing",
                "description": "Gone",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            }
        ]
        mcp_client.drop_connections()
        missing_tools, _ = mcp_domain_tools(ctx)
        assert missing_tools[0].execute(ctx)["code"] == "MCP_CLIENT_ERROR"


def test_down_server_does_not_hide_the_others(fake):
    ctx = _chat_ctx()
    down = {"url": "http://127.0.0.1:9/mcp", "token": TOKEN}
    with _with_servers({"claw": _server(fake), "down": down}):
        tools, hint = mcp_domain_tools(ctx)
    assert len(tools) == len(TOOLS)
    assert "Server 'down' is unavailable" in hint
    assert "Cannot reach" in hint


def test_config_change_makes_a_new_connection(fake):
    first = mcp_client.get_connection(_server(fake))
    assert mcp_client.get_connection(_server(fake)) is first
    assert mcp_client.get_connection(_server(fake, url=fake.url + "?v=2")) is not first


def test_servers_object_is_kept_as_written():
    """The real config layer knows ``mcp_client.servers`` and keeps the object as written."""
    from plugin.framework import config_schema

    assert config_schema.resolve_default("mcp_client.servers") == {}
    servers = {"a": {"url": "http://x", "token_keychain": "k"}}
    assert config_schema.validate({"mcp_client.servers": servers})["mcp_client.servers"] == servers


def _approval_ctx(answer: Any) -> tuple[SimpleNamespace, list[Any]]:
    """A ToolContext stand-in whose approval step returns *answer* and records what it was shown."""
    seen: list[Any] = []

    def approve(text: str, tool: str, args: Any) -> Any:
        seen.append((text, tool, args))
        return answer

    ctx = _chat_ctx(approval_callback=approve, chat_append_callback=seen.append)
    return ctx, seen


def _remote_tool(ctx: Any, name: str) -> Any:
    tools, _hint = mcp_domain_tools(ctx)
    return {t.name: t for t in tools}[name]


def test_confirm_on_accept_runs_the_call(fake):
    ctx, seen = _approval_ctx((True, None))
    with _with_servers({"claw": _server(fake, trust="untrusted")}):
        called = _remote_tool(ctx, "mcp__claw__nextcloud_list_files").execute(ctx, path="/Docs")
    assert called["status"] == "ok"
    assert "claw: nextcloud_list_files" in seen[0]
    assert seen[1] == (format_approval_arguments({"path": "/Docs"}), "mcp__claw__nextcloud_list_files", {"path": "/Docs"})


def test_confirm_on_reject_does_not_call_the_server(fake):
    ctx, unused_seen = _approval_ctx((False, None))
    with _with_servers({"claw": _server(fake, trust="untrusted")}):
        refused = _remote_tool(ctx, "mcp__claw__nextcloud_list_files").execute(ctx, path="/")
    assert refused["code"] == "MCP_CLIENT_CALL_REJECTED"
    assert "tools/call" not in fake.methods()


def test_confirm_change_sends_the_edited_arguments(fake):
    ctx, seen = _approval_ctx((True, '{"path": "/Other"}'))
    with _with_servers({"claw": _server(fake, trust="untrusted")}):
        called = _remote_tool(ctx, "mcp__claw__nextcloud_list_files").execute(ctx, path="/")
    assert called["result"] == "files in /Other"
    assert seen[-1] == f"[Changed to: {format_approval_arguments({'path': '/Other'})}]\n"


def test_confirm_change_to_non_json_does_not_call_the_server(fake):
    ctx, unused_seen = _approval_ctx((True, "only the Docs folder please"))
    with _with_servers({"claw": _server(fake, trust="untrusted")}):
        refused = _remote_tool(ctx, "mcp__claw__nextcloud_list_files").execute(ctx, path="/")
    assert refused["code"] == "MCP_CLIENT_CALL_CHANGED"
    assert "only the Docs folder please" in refused["message"]
    assert "tools/call" not in fake.methods()


def test_confirm_fails_closed_without_an_approval_step(fake):
    ctx = _chat_ctx()
    with _with_servers({"claw": _server(fake, trust="untrusted")}):
        refused = _remote_tool(ctx, "mcp__claw__nextcloud_list_files").execute(ctx, path="/")
    assert refused["code"] == "MCP_CLIENT_APPROVAL_UNAVAILABLE"
    assert "tools/call" not in fake.methods()


def test_readonly_tool_on_untrusted_server_still_asks(fake):
    ctx, seen = _approval_ctx((True, None))
    with _with_servers({"claw": _server(fake, trust="untrusted")}):
        called = _remote_tool(ctx, "mcp__claw__nextcloud_list_files").execute(ctx, path="/")
    assert called["status"] == "ok"
    assert len(seen) == 2


def test_unannotated_tool_on_trusted_server_asks(fake):
    ctx = _chat_ctx()
    with _with_servers({"claw": _server(fake, trust="full")}):
        refused = _remote_tool(ctx, "mcp__claw__write_note").execute(ctx, text="hi")
    assert refused["code"] == "MCP_CLIENT_APPROVAL_UNAVAILABLE"


def test_listing_tools_never_asks(fake):
    ctx, seen = _approval_ctx((False, None))
    with _with_servers({"claw": _server(fake, trust="untrusted")}):
        tools, _hint = mcp_domain_tools(ctx)
    assert len(tools) == len(TOOLS)
    assert seen == []


def test_call_reports_the_arguments_sent(fake):
    ctx = _chat_ctx()
    with _with_servers({"claw": _server(fake)}):
        tool = _remote_tool(ctx, "mcp__claw__nextcloud_list_files")
        empty = tool.execute(ctx)
        full = tool.execute(ctx, path="/Docs")
        no_args_tool = _remote_tool(ctx, "mcp__claw__weather_get_current_weather").execute(ctx)
    assert empty["arguments"] == {}
    assert full["arguments"] == {"path": "/Docs"}
    assert no_args_tool["arguments"] == {}


def test_runaway_cursor_capped(fake):
    fake.endless_cursor = True
    tools = McpServerConnection(_server(fake)).list_tools()
    assert fake.methods().count("tools/list") == 50
    assert len(tools) == 50


def test_sse_bare_cr_boundaries(fake):
    fake.bare_cr = True
    result = McpServerConnection(_server(fake)).call_tool("nextcloud_list_files", {"path": "/CR"})
    assert render_call_result(result) == {"result": "files in /CR"}


def test_server_request_with_colliding_id_is_not_the_reply(fake):
    fake.server_request_same_id = True
    result = McpServerConnection(_server(fake)).call_tool("nextcloud_list_files", {"path": "/ID"})
    assert render_call_result(result) == {"result": "files in /ID"}


def test_non_object_body_is_a_clean_error(fake):
    fake.array_body = True
    with pytest.raises(McpClientError, match="invalid JSON-RPC reply"):
        McpServerConnection(_server(fake)).list_tools()


def test_oversized_json_body_rejected(fake, monkeypatch):
    fake.oversized = True
    monkeypatch.setattr(mcp_client, "MAX_BODY_BYTES", 1024)
    with pytest.raises(McpClientError, match="exceeds 1024 bytes"):
        McpServerConnection(_server(fake)).list_tools()


def test_sse_event_over_cap_rejected(fake, monkeypatch):
    fake.oversized = True
    fake.sse = True
    monkeypatch.setattr(mcp_client, "MAX_BODY_BYTES", 1024)
    with pytest.raises(McpClientError, match="exceeds 1024 bytes"):
        McpServerConnection(_server(fake)).list_tools()


def test_sse_keepalives_are_not_cumulative(fake, monkeypatch):
    fake.keepalives_count = 300
    monkeypatch.setattr(mcp_client, "MAX_BODY_BYTES", 1024)
    tools = McpServerConnection(_server(fake)).list_tools()
    assert len(tools) == len(TOOLS)


def test_redirect_same_origin_307_followed(fake):
    fake.redirect_same_origin_307 = True
    tools = McpServerConnection(_server(fake)).list_tools()
    assert len(tools) == len(TOOLS)


def test_redirect_cross_origin_307_refused(fake):
    second = FakeMcpServer()
    try:
        fake.redirect_cross_origin_target = second.url
        with pytest.raises(McpClientError, match="redirected"):
            McpServerConnection(_server(fake)).list_tools()
        assert second.requests == []
    finally:
        second.close()


def test_redirect_302_refused(fake):
    fake.redirect_302 = True
    with pytest.raises(McpClientError, match="redirected"):
        McpServerConnection(_server(fake)).list_tools()


def test_returns_on_matching_event_without_waiting_for_close(fake):
    fake.hold_open_event = threading.Event()
    result = McpServerConnection(_server(fake)).call_tool("nextcloud_list_files", {"path": "/Stream"})
    assert render_call_result(result) == {"result": "files in /Stream"}
    assert not fake.hold_open_event.is_set()


def test_stop_aborts_call_and_sends_notifications_cancelled(fake):
    fake.hang_event = threading.Event()
    conn = McpServerConnection(_server(fake))
    conn.list_tools()
    cancel = SendCancellation()
    timer = threading.Timer(0.15, cancel.cancel)
    timer.start()
    try:
        with pytest.raises(McpClientError) as info:
            conn.call_tool("nextcloud_list_files", {"path": "/Hang"}, cancel=cancel)
    finally:
        timer.cancel()
        timer.join(timeout=2.0)
    assert info.value.code == "USER_STOPPED"
    assert fake.cancel_received_event.wait(2.0)
    assert fake.cancelled_request_ids == [3]
    fake.hang_event.set()
    fake.hang_event = None
    # Stop does not count toward the breaker: three new errors are still needed to open it.
    for _idx in range(3):
        assert conn.call_tool("broken", {})["isError"] is True
    with pytest.raises(McpClientError) as paused:
        conn.call_tool("broken", {})
    assert paused.value.code == "MCP_SERVER_PAUSED"


def test_negotiated_protocol_version_sent(fake):
    fake.protocol_version = "2025-03-26"
    McpServerConnection(_server(fake)).list_tools()
    assert fake.requests[2][0]["mcp-protocol-version"] == "2025-03-26"


def test_no_tools_capability_skips_tools_list(fake):
    fake.no_tools_capability = True
    assert McpServerConnection(_server(fake)).list_tools() == []
    assert "tools/list" not in fake.methods()


def test_http_error_includes_sanitized_body_head(fake):
    fake.error_body = (500, "database locked token=abc")
    with pytest.raises(McpClientError) as info:
        McpServerConnection(_server(fake)).list_tools()
    msg = str(info.value)
    assert "HTTP 500" in msg
    assert "database locked" in msg
    assert "[REDACTED]" in msg
    assert "abc" not in msg


def test_read_timeout_after_send(fake):
    conn = McpServerConnection(_server(fake))
    conn.list_tools()
    fake.read_sleep_event = threading.Event()
    conn.tool_timeout = 0.2
    with pytest.raises(McpClientError) as call_info:
        conn.call_tool("nextcloud_list_files", {"path": "/Slow"})
    assert call_info.value.code == "MCP_OUTCOME_UNCERTAIN"

    conn.connect_timeout = 0.2
    with pytest.raises(McpClientError, match="Cannot reach"):
        conn.list_tools()


def test_breaker_opens_after_three_transport_failures_says_unreachable():
    conn = McpServerConnection({"name": "down", "url": "http://127.0.0.1:9/mcp", "token": TOKEN})
    for _idx in range(3):
        with pytest.raises(McpClientError):
            conn.call_tool("x", {})
    with pytest.raises(McpClientError, match="unreachable") as info:
        conn.call_tool("x", {})
    assert info.value.code == "MCP_SERVER_PAUSED"


def test_breaker_opened_by_tool_errors_says_rejected(fake):
    conn = McpServerConnection(_server(fake))
    for _idx in range(3):
        res = conn.call_tool("broken", {})
        assert res["isError"] is True
    with pytest.raises(McpClientError, match="rejected") as info:
        conn.call_tool("broken", {})
    assert info.value.code == "MCP_SERVER_PAUSED"


def test_breaker_half_opens_after_cooldown(fake, monkeypatch):
    now = [100.0]
    # mcp_client.time is the stdlib module, so the transport connect deadline is also frozen here.
    monkeypatch.setattr(mcp_client.time, "monotonic", lambda: now[0])
    conn = McpServerConnection(_server(fake))
    for _idx in range(3):
        conn.call_tool("broken", {})
    with pytest.raises(McpClientError) as paused:
        conn.call_tool("nextcloud_list_files", {"path": "/"})
    assert paused.value.code == "MCP_SERVER_PAUSED"

    now[0] += 61.0
    ok = conn.call_tool("nextcloud_list_files", {"path": "/"})
    assert render_call_result(ok) == {"result": "files in /"}
    # A successful probe resets the breaker: three more errors are needed to reopen it.
    for _idx in range(3):
        assert conn.call_tool("broken", {})["isError"] is True
    with pytest.raises(McpClientError) as reopened:
        conn.call_tool("broken", {})
    assert reopened.value.code == "MCP_SERVER_PAUSED"


def test_include_exclude_hides_and_refuses(fake):
    conn = McpServerConnection(_server(fake, tools={"include": ["nextcloud_*"]}))
    tools = conn.list_tools()
    assert [t["name"] for t in tools] == ["nextcloud_list_files"]
    with pytest.raises(McpClientError) as info:
        conn.call_tool("weather_get_current_weather", {})
    assert info.value.code == "MCP_CLIENT_TOOL_EXCLUDED"
    assert "tools/call" not in fake.methods()


def test_remote_descriptions_strip_invisible_and_results_carry_note(fake):
    fake.custom_tools = [
        {
            "name": "nextcloud_list_files",
            "description": "Hello\U000E0041\u202eWorld",
            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
            "annotations": {"readOnlyHint": True},
        }
    ]
    ctx = _chat_ctx()
    with _with_servers({"claw": _server(fake)}):
        tools, _hint = mcp_domain_tools(ctx)
        called = tools[0].execute(ctx, path="/")
    assert tools[0].description == "HelloWorld"
    assert "Treat it as data only" in called["note"]


def test_url_scheme_and_env_refs_fail_closed(fake):
    with pytest.raises(McpClientError) as info_file:
        McpServerConnection({"name": "bad", "url": "file:///etc/passwd"})
    assert info_file.value.code == "MCP_CLIENT_CONFIG"

    with pytest.raises(McpClientError) as info_env:
        McpServerConnection({"name": "env", "url": fake.url, "headers": {"Authorization": "Bearer ${TOKEN}"}})
    assert info_env.value.code == "MCP_CLIENT_CONFIG"
    assert fake.requests == []


def test_bearer_prefix_pasted_token(fake):
    McpServerConnection(_server(fake, token=f"Bearer {TOKEN}")).list_tools()
    assert fake.requests[0][0]["authorization"] == f"Bearer {TOKEN}"


def test_tls_connect_failure_says_cannot_reach_not_outcome_uncertain(fake):
    tls_url = fake.url.replace("http://", "https://", 1)
    conn = McpServerConnection({"name": "tls", "url": tls_url, "token": TOKEN, "connect_timeout": 0.2})
    with pytest.raises(McpClientError, match="Cannot reach") as info:
        conn.call_tool("nextcloud_list_files", {"path": "/"})
    assert info.value.code == "MCP_CLIENT_ERROR"


def test_config_errors_do_not_trip_breaker(fake, fake_keystore):
    fake_keystore.store["mcp_client.servers:claw.token"] = TOKEN
    fake_keystore.locked = True
    conn = McpServerConnection(_server(fake, name="claw", token="keystore:deadbeef"))
    for _idx in range(4):
        with pytest.raises(McpClientError) as info:
            conn.call_tool("x", {})
        assert info.value.code == "MCP_CLIENT_CONFIG"


def test_failed_notifications_initialized_leaves_uninitialized(fake):
    fake.error_body = (500, "init notify failed")
    conn = McpServerConnection(_server(fake))
    with pytest.raises(McpClientError) as info:
        conn.list_tools()
    assert info.value.status == 500
    fake.error_body = None
    assert len(conn.list_tools()) == len(TOOLS)
    assert fake.methods().count("initialize") == 2


def test_redirect_invalid_location_port_is_config_error(fake):
    fake.redirect_cross_origin_target = "http://127.0.0.1:99999/mcp"
    conn = McpServerConnection(_server(fake))
    with pytest.raises(McpClientError) as info:
        conn.call_tool("nextcloud_list_files", {"path": "/"})
    assert info.value.code == "MCP_CLIENT_CONFIG"


def test_property_names_are_mapped_back_to_the_server(fake):
    fake.custom_tools = [
        {
            "name": "echo",
            "description": "Echo arguments",
            "inputSchema": {
                "type": "object",
                "properties": {"file path": {"type": "string"}},
                "required": ["file path"],
            },
            "annotations": {"readOnlyHint": True},
        }
    ]
    ctx = _chat_ctx()
    with _with_servers({"claw": _server(fake)}):
        tools, _hint = mcp_domain_tools(ctx)
        assert "file_path" in tools[0].parameters["properties"]
        called = tools[0].execute(ctx, file_path="/Docs/a.odt")
    assert called["status"] == "ok"
    assert json.loads(called["result"]) == {"file path": "/Docs/a.odt"}


def test_unconvertible_schema_skips_only_that_tool(fake):
    fake.custom_tools = [
        {
            "name": "recursive_tool",
            "description": "Bad recursive schema",
            "inputSchema": {
                "type": "object",
                "$defs": {"N": {"type": "object", "properties": {"next": {"$ref": "#/$defs/N"}}}},
                "properties": {"root": {"$ref": "#/$defs/N"}},
            },
        },
        {
            "name": "typeless_anyof",
            "description": "Bad anyOf without type",
            "inputSchema": {
                "type": "object",
                "properties": {"x": {"anyOf": [{"description": "no type"}]}},
            },
        },
        TOOLS[0],
    ]
    ctx = _chat_ctx()
    with _with_servers({"claw": _server(fake)}):
        tools, _hint = mcp_domain_tools(ctx)
    assert [t.name for t in tools] == ["mcp__claw__nextcloud_list_files"]


def test_name_collision_across_servers_skips_both(fake):
    ctx = _chat_ctx()
    with _with_servers({"my-home": _server(fake, name="my-home"), "my_home": _server(fake, name="my_home")}):
        with pytest.raises(McpClientError) as info:
            mcp_domain_tools(ctx)
        assert info.value.code == "MCP_CLIENT_NO_TOOLS"

    fake.custom_tools = [TOOLS[0], TOOLS[1]]
    mcp_client.drop_connections()
    second = FakeMcpServer()
    try:
        second.custom_tools = [TOOLS[0], TOOLS[3]]
        with _with_servers({"my-home": _server(fake, name="my-home"), "my_home": _server(second, name="my_home")}):
            tools, _hint = mcp_domain_tools(ctx)
        names = [t.name for t in tools]
        assert "mcp__my_home__nextcloud_list_files" not in names
        assert "mcp__my_home__weather_get_current_weather" in names
        assert "mcp__my_home__write_note" in names
    finally:
        second.close()


def test_domain_refuses_callers_other_than_chat(fake):
    for caller in ("mcp", "script"):
        ctx = SimpleNamespace(caller=caller, send_cancellation=None)
        with pytest.raises(McpClientError) as info:
            mcp_domain_tools(ctx)
        assert info.value.code == "MCP_DOMAIN_CHAT_ONLY"
    assert fake.requests == []


def test_stop_during_listing_is_user_stopped(fake):
    cancel = SendCancellation()
    cancel.cancel()
    ctx = _chat_ctx(send_cancellation=cancel)
    with _with_servers({"claw": _server(fake)}):
        with pytest.raises(McpClientError) as info:
            mcp_domain_tools(ctx)
        assert info.value.code == "USER_STOPPED"


def test_mcp_domain_offered_only_with_a_server():
    from unittest.mock import MagicMock

    from plugin.doc.specialized_base import DelegateToSubagent
    from plugin.framework.tool import ToolRegistry
    from plugin.writeragent_hooks import register_writeragent_tool_filters

    reg = ToolRegistry(MagicMock())
    reg.register(DelegateToSubagent())
    register_writeragent_tool_filters(reg)

    def _delegate_fn(schemas: list[dict[str, Any]]) -> dict[str, Any]:
        for s in schemas:
            if s.get("function", {}).get("name") == "delegate_to_subagent":
                return s["function"]
        raise AssertionError("delegate_to_subagent schema missing")

    with _with_servers({}):
        fn = _delegate_fn(reg.get_schemas("openai", doc_type="writer", ctx=object()))
        assert "mcp" not in fn["parameters"]["properties"]["domain"]["enum"]
        assert "mcp: " not in fn["description"]

    with _with_servers({"claw": {"url": "http://127.0.0.1:9/mcp"}}):
        fn = _delegate_fn(reg.get_schemas("openai", doc_type="writer", ctx=object()))
        assert "mcp" in fn["parameters"]["properties"]["domain"]["enum"]
        assert "mcp: " in fn["description"]


def test_delegate_to_subagent_runs_the_mcp_domain(fake):
    from unittest.mock import MagicMock

    from plugin.doc import specialized_base
    from plugin.doc.specialized_base import DelegateToSubagent
    from plugin.doc.specialized_catalog import NO_EDIT_TOOL_NOTE
    from plugin.framework.tool import ToolContext, ToolRegistry
    from plugin.writer.specialized_base import SpecializedWorkflowFinished

    reg = ToolRegistry(MagicMock())
    reg.register(SpecializedWorkflowFinished())
    services = {"tools": reg}
    ctx = ToolContext(doc=None, ctx=object(), doc_type="writer", services=services, caller="chat")

    captured_instructions: list[str] = []

    def _fake_run_inner(tctx: Any, smol_tools: list[Any], *, instructions: str, **kwargs: Any) -> str:
        captured_instructions.append(instructions)
        tool_map = {t.name: t for t in smol_tools}
        obs = tool_map["mcp__claw__nextcloud_list_files"](path="/Docs")
        assert "files in /Docs" in str(obs)
        return tool_map["workflow_finished"](answer="Files: /Docs")

    with (
        _with_servers({"claw": _server(fake)}),
        patch.object(specialized_base.main, "call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch.object(specialized_base, "run_inner_tool_agent", side_effect=_fake_run_inner),
    ):
        res = DelegateToSubagent().execute(ctx, domain="mcp", task="List /Docs")

    assert res["status"] == "ok"
    assert "Files: /Docs" in str(res.get("answer") or res.get("result"))
    assert res["note"] == UNTRUSTED_NOTE
    assert NO_EDIT_TOOL_NOTE not in str(res.get("note", ""))
    assert any(req_body.get("method") == "tools/call" and req_body["params"]["arguments"] == {"path": "/Docs"} for _h, req_body in fake.requests)
    assert MCP_SUB_AGENT_HINT in captured_instructions[0]


def test_wire_schemas_seen_by_model_and_arrays_have_items(fake):
    from plugin.chatbot.smol_agent import SmolToolAdapter
    from plugin.contrib.smolagents.models import get_tool_json_schema

    fake.custom_tools = [
        {
            "name": "str_or_list",
            "inputSchema": {
                "type": "object",
                "properties": {"q": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}},
            },
        },
        {
            "name": "str_list_or_null",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "r": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                            {"type": "null"},
                        ],
                        "default": None,
                    }
                },
            },
        },
        {
            "name": "object_or_string",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "f": {
                        "anyOf": [
                            {
                                "type": "object",
                                "properties": {"a": {"type": "string"}},
                                "required": ["a"],
                            },
                            {"type": "string"},
                        ],
                        "description": "F",
                    }
                },
                "required": ["f"],
            },
        },
        {
            "name": "nested_union",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "rows": {
                        "type": "array",
                        "items": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                    }
                },
                "required": ["rows"],
            },
        },
        {
            "name": "two_models",
            "inputSchema": {
                "type": "object",
                "properties": {"m": {"anyOf": [{"type": "object", "properties": {"a": {"type": "string"}}}, {"type": "object", "properties": {"b": {"type": "integer"}}}]}},
            },
        },
        {
            "name": "one_of_prop",
            "inputSchema": {"type": "object", "properties": {"x": {"oneOf": [{"type": "string"}, {"type": "integer"}]}}},
        },
        {
            "name": "all_of_prop",
            "inputSchema": {"type": "object", "properties": {"x": {"allOf": [{"type": "object", "properties": {"a": {"type": "string"}}}]}}},
        },
        {
            "name": "typeless_prop",
            "inputSchema": {"type": "object", "properties": {"x": {"description": "any"}}},
        },
        {
            "name": "bare_array",
            "inputSchema": {"type": "object", "properties": {"arr": {"type": "array"}}},
        },
        {
            "name": "good_tool",
            "description": "Search items",
            "inputSchema": {
                "type": "object",
                "$defs": {"Item": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
                "properties": {
                    "items_list": {"type": "array", "items": {"$ref": "#/$defs/Item"}, "description": "Items"},
                    "limit": {"anyOf": [{"type": "string"}, {"type": "integer"}], "description": "Limit"},
                    "opt_num": {"anyOf": [{"type": "integer"}, {"type": "null"}], "description": "Opt"},
                },
                "required": ["items_list"],
            },
        },
    ]
    ctx = _chat_ctx()
    with _with_servers({"claw": _server(fake)}):
        tools, _hint = mcp_domain_tools(ctx)

    by_name = {t.name: t for t in tools}
    assert list(by_name) == [
        "mcp__claw__str_or_list",
        "mcp__claw__str_list_or_null",
        "mcp__claw__object_or_string",
        "mcp__claw__nested_union",
        "mcp__claw__two_models",
        "mcp__claw__one_of_prop",
        "mcp__claw__good_tool",
    ]
    wire_by_name = {
        name: get_tool_json_schema(SmolToolAdapter(tool, ctx, inputs_style="specialized"))
        for name, tool in by_name.items()
    }
    assert wire_by_name["mcp__claw__str_or_list"]["function"]["parameters"]["properties"] == {
        "q": {"type": ["string", "array"], "items": {"type": "string"}, "description": ""}
    }
    assert wire_by_name["mcp__claw__str_list_or_null"]["function"]["parameters"]["properties"] == {
        "r": {"type": ["string", "array"], "items": {"type": "string"}, "default": None, "description": ""}
    }
    obj_or_str_params = wire_by_name["mcp__claw__object_or_string"]["function"]["parameters"]
    assert obj_or_str_params["properties"] == {
        "f": {
            "type": ["object", "string"],
            "properties": {"a": {"type": "string"}},
            "required": ["a"],
            "description": "F",
        }
    }
    assert obj_or_str_params["required"] == ["f"]
    assert wire_by_name["mcp__claw__nested_union"]["function"]["parameters"]["properties"] == {
        "rows": {
            "type": "array",
            "items": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
            "description": "",
        }
    }
    assert wire_by_name["mcp__claw__two_models"]["function"]["parameters"]["properties"] == {
        "m": {"type": "object", "properties": {"a": {"type": ["string", "null"]}}, "description": ""}
    }
    assert wire_by_name["mcp__claw__one_of_prop"]["function"]["parameters"]["properties"] == {
        "x": {"type": ["string", "integer", "null"], "description": ""}
    }
    assert wire_by_name["mcp__claw__good_tool"] == {
        "type": "function",
        "function": {
            "name": "mcp__claw__good_tool",
            "description": "Search items",
            "parameters": {
                "type": "object",
                "properties": {
                    "items_list": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"id": {"type": "integer"}},
                            "required": ["id"],
                        },
                        "description": "Items",
                    },
                    "limit": {
                        "type": ["string", "integer", "null"],
                        "description": "Limit",
                    },
                    "opt_num": {
                        "type": ["integer", "null"],
                        "description": "Opt",
                    },
                },
                "required": ["items_list"],
            },
        },
    }


def test_deepwiki_ask_wiki_question_is_offered(fake):
    from plugin.chatbot.smol_agent import SmolToolAdapter
    from plugin.contrib.smolagents.models import get_tool_json_schema

    repo_desc = "GitHub repository or list of repositories (max 10) in owner/repo format."
    fake.custom_tools = [
        {
            "name": "ask_wiki_question",
            "description": "Ask a question about a GitHub repository",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repoName": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": repo_desc,
                    },
                    "question": {"type": "string", "description": "Question to ask"},
                },
                "required": ["repoName", "question"],
                "additionalProperties": False,
            },
        }
    ]
    ctx = _chat_ctx()
    with _with_servers({"deepwiki": _server(fake, name="deepwiki")}):
        tools, _hint = mcp_domain_tools(ctx)

    assert [t.name for t in tools] == ["mcp__deepwiki__ask_wiki_question"]
    tool = tools[0]
    wire = get_tool_json_schema(SmolToolAdapter(tool, ctx, inputs_style="specialized"))
    params = wire["function"]["parameters"]
    assert params["properties"]["repoName"] == {
        "type": ["string", "array"],
        "items": {"type": "string"},
        "description": repo_desc,
    }
    assert params["required"] == ["repoName", "question"]
    assert tool.validate(repoName=["a/b"], question="q") == (True, None)
    assert tool.validate(repoName="a/b", question="q") == (True, None)
def test_down_server_error_is_capped_in_hint(fake):
    fake.error_body = (500, "x" * 600)
    ctx = _chat_ctx()
    second = FakeMcpServer()
    try:
        with _with_servers({"down": _server(fake, name="down"), "ok": _server(second, name="ok")}):
            _tools, hint = mcp_domain_tools(ctx)
        assert "Server 'down' is unavailable:" in hint
        assert "ERROR TRUNCATED" in hint
    finally:
        second.close()


def test_calc_mcp_delegation_skips_spreadsheet_context():
    from plugin.doc.specialized_base import _sub_agent_hints

    ctx = SimpleNamespace(doc=object(), ctx=object())
    with patch("plugin.calc.analyzer.get_calc_context_for_chat") as mock_calc_ctx:
        hints = _sub_agent_hints(ctx, "mcp", "Calc", "")
    mock_calc_ctx.assert_not_called()
    assert "[SPREADSHEET CONTEXT]" not in hints


def test_second_delegation_reuses_the_tool_list(fake):
    ctx = _chat_ctx()
    with _with_servers({"claw": _server(fake)}):
        first_tools, _ = mcp_domain_tools(ctx)
        second_tools, _ = mcp_domain_tools(ctx)
    assert [t.name for t in first_tools] == [t.name for t in second_tools]
    assert fake.methods().count("tools/list") == 1
    assert fake.methods().count("initialize") == 1


def test_changed_server_settings_list_again(fake):
    ctx = _chat_ctx()
    with _with_servers({"claw": _server(fake, timeout=30)}):
        mcp_domain_tools(ctx)
    assert fake.methods().count("tools/list") == 1

    with _with_servers({"claw": _server(fake, timeout=60)}):
        mcp_domain_tools(ctx)
    assert fake.methods().count("tools/list") == 2

    mcp_client.drop_connections()
    with _with_servers({"claw": _server(fake, timeout=60)}):
        mcp_domain_tools(ctx)
    assert fake.methods().count("tools/list") == 3


def test_list_changed_in_a_reply_drops_the_list(fake):
    conn = McpServerConnection(_server(fake))
    assert len(conn.list_tools()) == len(TOOLS)

    fake.list_changed_on_call = True
    result = conn.call_tool("nextcloud_list_files", {"path": "/Docs"})
    assert render_call_result(result) == {"result": "files in /Docs"}

    fake.custom_tools = [TOOLS[0]]
    tools = conn.list_tools()
    assert [t["name"] for t in tools] == [TOOLS[0]["name"]]
    assert fake.methods().count("tools/list") == 2


@pytest.mark.parametrize("as_is_error", [False, True])
def test_unknown_tool_relists_once_then_retries_once(fake, as_is_error):
    conn = McpServerConnection(_server(fake))
    conn.list_tools()

    fake.unknown_once = {"nextcloud_list_files"}
    fake.unknown_as_is_error = as_is_error
    result = conn.call_tool("nextcloud_list_files", {"path": "/Docs"})
    assert render_call_result(result) == {"result": "files in /Docs"}
    assert fake.methods()[-3:] == ["tools/call", "tools/list", "tools/call"]


def test_unknown_tool_gone_from_the_new_list_is_not_retried(fake):
    gone = {
        "name": "gone",
        "description": "Removed",
        "inputSchema": {"type": "object", "properties": {}},
    }
    fake.custom_tools = [TOOLS[0], gone]
    conn = McpServerConnection(_server(fake))
    conn.list_tools()

    fake.custom_tools = [TOOLS[0]]
    with pytest.raises(McpClientError, match="Unknown tool: gone"):
        conn.call_tool("gone", {})
    assert fake.methods()[-2:] == ["tools/call", "tools/list"]

    tools = conn.list_tools()
    assert [t["name"] for t in tools] == [TOOLS[0]["name"]]
    assert fake.methods().count("tools/list") == 2


def test_two_turns_listing_at_once_fetch_once(fake):
    fake.list_gate = threading.Event()
    conn = McpServerConnection(_server(fake))
    results: list[list[dict[str, Any]]] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(conn.list_tools())
        except BaseException as exc:  # pragma: no cover - test failure diagnostic
            errors.append(exc)

    thread_a = threading.Thread(target=worker)
    thread_b = threading.Thread(target=worker)
    thread_a.start()
    assert fake.list_started.wait(2.0)
    thread_b.start()
    fake.list_gate.set()
    thread_a.join(timeout=2.0)
    thread_b.join(timeout=2.0)

    assert not errors
    assert len(results) == 2
    assert results[0] is results[1]
    assert fake.methods().count("tools/list") == 1


def test_failed_listing_keeps_no_entry(fake):
    fake.error_body = (500, "boom")
    conn = McpServerConnection(_server(fake))
    with pytest.raises(McpClientError):
        conn.list_tools()

    fake.error_body = None
    tools = conn.list_tools()
    assert [t["name"] for t in tools] == [t["name"] for t in TOOLS]


def test_approval_card_masks_secrets_but_sends_real_values(fake):
    fake.custom_tools = [
        {
            "name": "echo",
            "description": "Echo args.",
            "inputSchema": {
                "type": "object",
                "properties": {"password": {"type": "string"}, "user": {"type": "string"}},
                "required": ["password", "user"],
            },
        }
    ]
    ctx, seen = _approval_ctx((True, None))
    with _with_servers({"claw": _server(fake, trust="untrusted")}):
        tool = _remote_tool(ctx, "mcp__claw__echo")
        out = tool.execute(ctx, password="my-secret-password", user="alice")

    assert out["status"] == "ok"
    chat_card = seen[0]
    callback_text, _tool_name, _callback_args = seen[1]
    assert "<redacted>" in chat_card
    assert "my-secret-password" not in chat_card
    assert "<redacted>" in callback_text
    assert "my-secret-password" not in callback_text
    call_req = [body for _hdrs, body in fake.requests if body.get("method") == "tools/call"][0]
    assert call_req["params"]["arguments"] == {"password": "my-secret-password", "user": "alice"}
    assert out["arguments"] == {"password": "<redacted>", "user": "alice"}


def test_change_keeps_masked_values_real(fake):
    fake.custom_tools = [
        {
            "name": "echo",
            "description": "Echo args.",
            "inputSchema": {
                "type": "object",
                "properties": {"password": {"type": "string"}, "user": {"type": "string"}},
            },
        }
    ]
    edited_json = json.dumps({"password": "<redacted>", "user": "bob"})
    ctx, seen = _approval_ctx((True, edited_json))
    with _with_servers({"claw": _server(fake, trust="untrusted")}):
        tool = _remote_tool(ctx, "mcp__claw__echo")
        out = tool.execute(ctx, password="old-secret-password", user="alice")

    assert out["status"] == "ok"
    call_req = [body for _hdrs, body in fake.requests if body.get("method") == "tools/call"][0]
    assert call_req["params"]["arguments"] == {"password": "old-secret-password", "user": "bob"}
    changed_line = [s for s in seen if isinstance(s, str) and s.startswith("[Changed to:")][0]
    assert "<redacted>" in changed_line
    assert "old-secret-password" not in changed_line


def test_changed_to_line_is_masked(fake):
    fake.custom_tools = [
        {
            "name": "echo",
            "description": "Echo args.",
            "inputSchema": {
                "type": "object",
                "properties": {"password": {"type": "string"}, "user": {"type": "string"}},
            },
        }
    ]
    edited_json = json.dumps({"password": "new-secret-password", "user": "bob"})
    ctx, seen = _approval_ctx((True, edited_json))
    with _with_servers({"claw": _server(fake, trust="untrusted")}):
        tool = _remote_tool(ctx, "mcp__claw__echo")
        out = tool.execute(ctx, password="old-secret-password", user="alice")

    assert out["status"] == "ok"
    changed_line = [s for s in seen if isinstance(s, str) and s.startswith("[Changed to:")][0]
    assert "<redacted>" in changed_line
    assert "new-secret-password" not in changed_line


def test_error_text_scrubs_the_token(fake):
    fake.error_body = (401, f"unauthorized for {TOKEN}")
    conn = McpServerConnection(_server(fake))
    with pytest.raises(McpClientError) as info:
        conn.list_tools()
    assert TOKEN not in str(info.value)


@pytest.mark.parametrize("header_name", ["X-Auth-Token", "Ocp-Apim-Subscription-Key"])
def test_secret_header_values_are_redacted(fake, header_name):
    header_secret = "hdr-secret-xyz-999"
    fake.error_body = (500, f"rejected {header_secret}")
    conn = McpServerConnection(_server(fake, headers={header_name: header_secret}))
    with pytest.raises(McpClientError) as info:
        conn.list_tools()
    assert info.value.status == 500
    assert "<redacted>" in str(info.value)
    assert header_secret not in str(info.value)


def test_ref_token_resolves_from_keystore(fake, fake_keystore):
    from plugin.framework import keystore

    keystore.set_secret("mcp_client.servers:claw.token", TOKEN)
    conn = McpServerConnection(_server(fake, name="claw", token="keystore:deadbeef"))
    conn.list_tools()
    assert fake.requests[0][0]["authorization"] == f"Bearer {TOKEN}"


def test_plain_token_works_without_store(fake):
    from plugin.framework import keystore

    keystore.reset_for_tests(None)
    try:
        conn = McpServerConnection(_server(fake, name="claw", token=TOKEN))
        conn.list_tools()
        assert fake.requests[0][0]["authorization"] == f"Bearer {TOKEN}"
    finally:
        keystore.reset_for_tests(None)


def test_unreadable_token_ref_names_server_and_keyring(fake, fake_keystore):
    from plugin.framework import keystore

    # Locked keyring case
    fake_keystore.store["mcp_client.servers:claw.token"] = TOKEN
    fake_keystore.locked = True
    conn = McpServerConnection(_server(fake, name="claw", token="keystore:deadbeef"))
    with pytest.raises(McpClientError) as locked_info:
        conn.list_tools()
    assert locked_info.value.code == "MCP_CLIENT_CONFIG"
    assert "claw" in str(locked_info.value)
    assert "keyring" in str(locked_info.value).lower()
    assert fake.requests == []

    # Fresh store where the item is missing
    fake_keystore.locked = False
    fake_keystore.store.clear()
    keystore.reset_for_tests(fake_keystore)
    conn2 = McpServerConnection(_server(fake, name="claw", token="keystore:deadbeef"))
    with pytest.raises(McpClientError) as missing_info:
        conn2.list_tools()
    assert missing_info.value.code == "MCP_CLIENT_CONFIG"
    assert "claw" in str(missing_info.value)
    assert "keyring" in str(missing_info.value).lower()
    assert fake.requests == []


def test_probe_then_ok_reuses_the_listed_tools(fake):
    entry = _server(fake, name="claw")
    conn = mcp_client.probe_server(entry)
    reqs_after_probe = len(fake.requests)
    with _with_servers({"claw": entry}):
        mcp_client.drop_connections(adopt=[conn])
        got = mcp_client.get_connection(mcp_client.configured_servers()[0])
        assert got is conn
        got.list_tools()
        assert len(fake.requests) == reqs_after_probe


def test_ok_does_not_adopt_a_server_no_longer_configured(fake):
    entry = _server(fake, name="claw")
    conn = mcp_client.probe_server(entry)
    with _with_servers({}):
        mcp_client.drop_connections(adopt=[conn])
        assert mcp_client.get_connection(entry) is not conn


def test_mcp_domain_hidden_from_writeragent_mcp_server(fake):
    from unittest.mock import MagicMock

    from plugin.doc.specialized_base import DelegateToSubagent
    from plugin.doc.specialized_catalog import get_specialized_domain_catalog
    from plugin.framework.tool import ToolRegistry
    from plugin.writeragent_hooks import register_writeragent_tool_filters

    reg = ToolRegistry(MagicMock())
    reg.register(DelegateToSubagent())
    register_writeragent_tool_filters(reg)

    with _with_servers({"claw": _server(fake, name="claw")}):
        for services in (
            None,
            frozenset({"com.sun.star.sheet.SpreadsheetDocument"}),
            frozenset({"com.sun.star.drawing.DrawingDocument"}),
        ):
            kwargs: dict[str, Any] = (
                {"filter_doc_type": False}
                if services is None
                else {"uno_services_supported": services}
            )
            mcp_schemas = reg.get_schemas("mcp", **kwargs)
            schema = mcp_schemas[0]
            domain_prop = schema["inputSchema"]["properties"]["domain"]
            assert "mcp" not in domain_prop["enum"]
            assert "mcp:" not in domain_prop.get("description", "")
            assert "mcp" not in schema.get("description", "")

        openai_schemas = reg.get_schemas("openai", filter_doc_type=False, ctx=object())
        openai_enum = openai_schemas[0]["function"]["parameters"]["properties"]["domain"]["enum"]
        assert "mcp" in openai_enum

        catalog = get_specialized_domain_catalog(agent_label=None, for_discovery=True)
        assert "mcp" not in [e["domain"] for e in catalog]


def test_headers_strip_whitespace_from_values(fake):
    conn = McpServerConnection(_server(fake, headers={"X-Custom": "  val \n", "X-Auth-Token": "\tsecret  "}))
    headers = conn._headers()
    assert headers["X-Custom"] == "val"
    assert headers["X-Auth-Token"] == "secret"


def test_remote_tool_call_does_not_fill_remote_defaults():
    spec = {
        "name": "list",
        "description": "d",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    }
    schema = convert_mcp_schema("srv", spec)
    tool = RemoteMcpTool(McpServerConnection({"name": "srv", "url": "http://localhost"}), spec, schema)
    prepared = prepare_tool_call(tool, SimpleNamespace(doc_type=None, caller=None), tool.name or "", {})
    assert prepared.error is None
    assert prepared.kwargs == {}


def test_old_tool_does_not_replace_the_live_connection(fake):
    ctx = _chat_ctx()
    with _with_servers({"claw": {"url": fake.url, "token": TOKEN, "trust": "full", "timeout": 30}}):
        tools_old, _ = mcp_domain_tools(ctx)
    with _with_servers({"claw": {"url": fake.url, "token": TOKEN, "trust": "full", "timeout": 60}}):
        mcp_domain_tools(ctx)
    live_conn = mcp_client._connections["claw"]
    old_read_tool = next(t for t in tools_old if t.name == "mcp__claw__nextcloud_list_files")
    result = old_read_tool.execute(ctx, path="/Docs")
    assert result["status"] == "ok"
    assert mcp_client._connections["claw"] is live_conn
    assert fake.methods().count("initialize") == 2


@pytest.mark.parametrize(
    ("extra", "secret_parts"),
    [
        ({"headers": {"X-Api-Key": "supersecret\npart2secret"}}, ("supersecret", "part2secret")),
        ({"headers": {"X-Api-Key": "supersecret\r\npart2secret"}}, ("supersecret", "part2secret")),
        ({"token": "abctoken1\nxdef 123rest"}, ("abctoken1", "xdef", "123rest")),
    ],
)
def test_header_with_line_break_is_a_config_error_without_the_value(fake, extra, secret_parts):
    conn = McpServerConnection({"name": "claw", "url": fake.url, "trust": "full", **extra})
    for _ in range(4):
        with pytest.raises(McpClientError) as exc_info:
            conn.call_tool("nextcloud_list_files", {"path": "/Docs"})
        assert exc_info.value.code == "MCP_CLIENT_CONFIG"
        msg = str(exc_info.value)
        for part in secret_parts:
            assert part not in msg
    assert fake.requests == []


def test_escaped_secret_in_an_error_body_is_redacted(fake):
    cookie = 'sid="qwertyui-9"'
    fake.error_body = (500, json.dumps({"error": f"bad cookie {cookie}"}))
    conn = McpServerConnection(_server(fake, headers={"Cookie": cookie}))
    with pytest.raises(McpClientError) as exc_info:
        conn.list_tools()
    assert exc_info.value.status == 500
    assert "<redacted>" in str(exc_info.value)
    assert "qwertyui-9" not in str(exc_info.value)


def test_bare_credential_of_an_authorization_header_is_redacted(fake):
    fake.error_body = (500, f"bad auth {TOKEN}")
    conn = McpServerConnection({"name": "claw", "url": fake.url, "headers": {"Authorization": f"Bearer {TOKEN}"}})
    with pytest.raises(McpClientError) as exc_info:
        conn.list_tools()
    assert exc_info.value.status == 500
    assert "<redacted>" in str(exc_info.value)
    assert TOKEN not in str(exc_info.value)


def test_url_query_secret_is_redacted(fake):
    fake.error_body = (500, "bad raw a%2Fb%2Bcdef and decoded a/b+cdef")
    conn = McpServerConnection({"name": "claw", "url": f"{fake.url}?token=a%2Fb%2Bcdef", "token": TOKEN})
    with pytest.raises(McpClientError) as exc_info:
        conn.list_tools()
    assert exc_info.value.status == 500
    assert "<redacted>" in str(exc_info.value)
    assert "a%2Fb%2Bcdef" not in str(exc_info.value)
    assert "a/b+cdef" not in str(exc_info.value)


def test_secret_with_double_space_or_tab_is_redacted(fake):
    secret = "ab  cd\tefgh"
    fake.error_body = (500, f"rejected {secret}")
    conn = McpServerConnection(_server(fake, headers={"X-Api-Key": secret}))
    with pytest.raises(McpClientError) as exc_info:
        conn.list_tools()
    assert exc_info.value.status == 500
    assert "<redacted>" in str(exc_info.value)
    assert secret not in str(exc_info.value)


def test_long_secret_is_redacted_before_the_body_is_cut(fake):
    secret = "S" * 1500
    fake.error_body = (500, f"bad {secret}")
    conn = McpServerConnection(_server(fake, headers={"X-Api-Key": secret}))
    with pytest.raises(McpClientError) as exc_info:
        conn.list_tools()
    assert exc_info.value.status == 500
    assert "<redacted>" in str(exc_info.value)
    assert "SSSS" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("header_name", "secret_parts"),
    [
        ("X-Api-Key: abcsecret", ("abcsecret",)),
        ("X-Api-Key\nsec", ("sec",)),
        ("X Api abcsecret", ("abcsecret",)),
        ("X-Api-Key ", ()),
        ("", ()),
        ("X-ésecret", ("X-ésecret",)),
    ],
)
def test_invalid_header_name_is_a_config_error_without_the_name(fake, header_name, secret_parts):
    with pytest.raises(McpClientError) as exc_info:
        McpServerConnection(_server(fake, headers={header_name: "val"}))
    assert exc_info.value.code == "MCP_CLIENT_CONFIG"
    msg = str(exc_info.value)
    for part in secret_parts:
        assert part not in msg
    assert fake.requests == []
