# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""MCP protocol document echo and tools/call thread-guard. No LibreOffice."""

import json
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.tool import ToolBase, ToolRegistry
from plugin.mcp.mcp_protocol import MCPProtocolHandler


def test_precomputed_echo_shape_and_absence():
    from plugin.mcp.mcp_protocol import _attach_precomputed_echo, _document_echo_payload

    doc = MagicMock()
    doc.URL = "file:///Users/x/Peti%C3%A7%C3%A3o%20Inicial.odt"
    doc.RuntimeUID = "42"
    result: dict[str, Any] = {"status": "ok"}
    _attach_precomputed_echo(result, _document_echo_payload(doc))
    assert result["document"]["name"] == "Petição Inicial.odt"
    assert result["document"]["uid"]
    # No doc -> no field; existing field -> untouched.
    r2 = {"status": "ok"}
    _attach_precomputed_echo(r2, _document_echo_payload(None))
    assert "document" not in r2
    r3 = {"status": "ok", "document": {"name": "keep"}}
    _attach_precomputed_echo(r3, _document_echo_payload(doc))
    assert r3["document"]["name"] == "keep"


def test_document_echo_payload_logs_and_returns_none(caplog):
    import logging
    from plugin.mcp.mcp_protocol import _document_echo_payload

    class _DocWithFailingURL:
        @property
        def URL(self):
            raise RuntimeError("simulated document URL access failure")

    caplog.set_level(logging.ERROR, logger="writeragent.mcp.protocol")
    assert _document_echo_payload(_DocWithFailingURL()) is None
    record = next(r for r in caplog.records if "MCP document echo failed" in r.message)
    assert record.exc_info is not None
    assert "simulated document URL access failure" in str(record.exc_info[1])


def test_long_running_precomputes_echo_without_post_execute_doc_access():
    """Echo is captured once inside _prepare_mcp_execution (main-thread marshal); the worker path attaches
    the precomputed echo and does not read the document again after the tool body runs."""
    from unittest.mock import patch

    from plugin.mcp.mcp_protocol import MCPProtocolHandler

    class _Registry:
        def get(self, name):
            class _Tool:
                is_mutation = False
                requires_document = True

                def detects_mutation(self):
                    return False

                def requires_document_lock(self, arguments=None):
                    return False

            return _Tool()

        def execute(self, name, context, **kwargs):
            assert "bypass_thread_guard" not in kwargs
            return {"status": "ok"}

    class _FakeMainThread:
        def execute(self, fn, *args, **kwargs):
            return fn(*args)

    class _FakeDocSvc:
        def resolve_document_by_url(self, url):
            doc = type("Doc", (), {"URL": url, "RuntimeUID": "uid-1", "getURL": lambda self: url})()
            return (doc, "writer")

        def get_active_document(self):
            return None

        def detect_doc_type(self, doc):
            return "writer"

    class _FakeServices:
        def __init__(self, registry):
            self.tools = registry
            self.document = _FakeDocSvc()

        def get(self, key):
            return _FakeMainThread() if key == "main_thread" else None

    registry = _Registry()
    handler = MCPProtocolHandler(_FakeServices(registry))

    with patch("plugin.mcp.mcp_protocol._document_echo_payload", return_value={"name": "doc.odt", "uid": "uid-1"}) as mock_echo:
        result = handler._execute_long_running("any_tool", {}, document_url="file:///doc.odt")

    assert result["status"] == "ok"
    assert result["document"] == {"name": "doc.odt", "uid": "uid-1"}
    assert mock_echo.call_count == 1


class _DisposedDoc:
    """getImplementationName raises DisposedException, so execute_safe reports DOCUMENT_DISPOSED."""

    def getURL(self):
        return "file:///gone.odt"

    def getImplementationName(self):
        raise type("DisposedException", (Exception,), {})("disposed")

    def supportsService(self, service_name):
        return True


class _LiveDoc:
    def getURL(self):
        return "file:///live.odt"

    def supportsService(self, service_name):
        return True


class _DocSvc:
    def __init__(self, doc):
        self._doc = doc

    def resolve_document_by_url(self, url):
        return (self._doc, "writer")

    def get_active_document(self):
        return self._doc

    def detect_doc_type(self, doc):
        return "writer"

    def open_documents(self):
        return [self._doc] if self._doc is not None else []


class _InlineMain:
    def execute(self, fn, *args, **kwargs):
        return fn(*args)


class _Services:
    def __init__(self, tools, doc):
        self.tools = tools
        self.document = _DocSvc(doc)
        self.config = MagicMock()
        self.config.get.side_effect = lambda key, default=None: default

    def get(self, key):
        return _InlineMain() if key == "main_thread" else None


class _SyncProbe(ToolBase):
    """Real tool so execute_safe's disposed-document check runs."""

    name = "sync_probe"
    description = "probe"
    parameters = {"type": "object", "properties": {"note": {"type": "string"}}}
    uno_services = None
    doc_types = None
    is_mutation = True
    long_running = False
    requires_document = True

    def __init__(self):
        self.body_calls: list[dict] = []

    def execute(self, ctx, **kwargs):
        self.body_calls.append(dict(kwargs))
        return {"status": "ok", "note": kwargs.get("note")}


def _handler(doc, tool):
    registry = ToolRegistry(MagicMock())
    registry.register(tool)
    return MCPProtocolHandler(_Services(registry, doc))


def _watch_execute_safe(handler, *, record_safe: bool = True):
    """Fail if MCP passes bypass_thread_guard. Optionally count execute_safe entries.

    record_safe=False leaves tool.execute_safe as the real method so callers can
    identify it as the registry runner (the marshal tests).
    """
    registry = handler.tool_registry
    real_execute = registry.execute
    seen: list[dict] = []

    def execute(tool_name, ctx, *args, **kwargs):
        assert args == ()
        assert "bypass_thread_guard" not in kwargs
        tool = registry.get(tool_name)
        safe_calls: list[dict] = []
        original_safe = None
        if record_safe and tool is not None:
            original_safe = tool.execute_safe

            def execute_safe(ctx, **tool_kwargs):
                safe_calls.append(dict(tool_kwargs))
                return original_safe(ctx, **tool_kwargs)

            tool.execute_safe = execute_safe
        try:
            return real_execute(tool_name, ctx, *args, **kwargs)
        finally:
            seen.append({"kwargs": dict(kwargs), "execute_safe": len(safe_calls)})
            if original_safe is not None and tool is not None:
                tool.execute_safe = original_safe

    registry.execute = execute
    return seen


def _payload(result):
    if isinstance(result, dict) and "content" in result:
        return json.loads(result["content"][0]["text"])
    return result


def test_tools_call_rejects_unadvertised_tiers():
    class FakeTool(ToolBase):
        name = "secret_tool"
        tier = "specialized"
        description = "secret"
        parameters = {}
        def execute(self, ctx, **kwargs):
            return "done"

    class FakeChatTool(ToolBase):
        name = "chat_tool"
        tier = "chat"
        description = "chat"
        parameters = {}
        def execute(self, ctx, **kwargs):
            return "done"

    # Fake services so we can mock config
    class FakeServices:
        def __init__(self, mode, tools=None):
            self.mode = mode
            self.tools = tools
            class FakeConfig:
                def get(inner_self, key, default):
                    if key == "mcp.tool_exposure_mode":
                        return self.mode
                    return default
            self.config = FakeConfig()
            self.document = _DocSvc(_LiveDoc())

        def get(self, key):
            if key == "main_thread":
                return _InlineMain()
            return None

    registry = ToolRegistry(FakeServices("delegate"))
    registry._tools["secret_tool"] = FakeTool()
    registry._tools["chat_tool"] = FakeChatTool()

    # 1. delegate mode: rejects 'specialized' and 'chat'
    handler_delegate = MCPProtocolHandler(FakeServices("delegate", tools=registry))
    handler_delegate.tool_registry = registry

    res1 = handler_delegate._mcp_tools_call({"name": "secret_tool", "arguments": {}})
    assert res1.get("isError") is True
    assert _payload(res1)["code"] == "UNKNOWN_TOOL"

    res2 = handler_delegate._mcp_tools_call({"name": "chat_tool", "arguments": {}})
    assert res2.get("isError") is True
    assert _payload(res2)["code"] == "UNKNOWN_TOOL"

    # 2. direct_flat mode: allows 'specialized', rejects 'chat'
    handler_flat = MCPProtocolHandler(FakeServices("direct_flat", tools=registry))
    handler_flat.tool_registry = registry
    handler_flat.event_bus = MagicMock()

    res3 = handler_flat._mcp_tools_call({"name": "secret_tool", "arguments": {}})
    # Call is accepted and starts processing (it returns None, but event bus gets called)
    assert not isinstance(res3, dict) or res3.get("isError") is not True

    res4 = handler_flat._mcp_tools_call({"name": "chat_tool", "arguments": {}})
    assert res4.get("isError") is True
    assert _payload(res4)["code"] == "UNKNOWN_TOOL"


@pytest.mark.parametrize("bypass", [True, 1, "yes"])
def test_tools_call_cannot_skip_disposed_document_check(bypass):
    """A client bypass_thread_guard value must still go through execute_safe.

    What was wrong: the key bound to ToolRegistry.execute's keyword-only flag,
    which calls tool.execute and skips the disposed-document check.
    MCP must drop the key and must not pass the keyword at all.
    """
    tool = _SyncProbe()
    handler = _handler(_DisposedDoc(), tool)
    seen = _watch_execute_safe(handler)
    args = {"bypass_thread_guard": bypass, "note": "keep"}

    direct = handler._execute_long_running("sync_probe", dict(args), document_url="file:///gone.odt")
    assert direct["code"] == "DOCUMENT_DISPOSED"
    assert direct["status"] == "error"

    wrapped = handler._mcp_tools_call({"name": "sync_probe", "arguments": dict(args)})
    assert wrapped["isError"] is True
    assert _payload(wrapped)["code"] == "DOCUMENT_DISPOSED"

    tool.long_running = False
    debug = handler._debug_call_tool("sync_probe", dict(args), document_url="file:///gone.odt")
    backpressure = handler._execute_with_backpressure("sync_probe", dict(args), document_url="file:///gone.odt")
    assert debug["code"] == "DOCUMENT_DISPOSED"
    assert backpressure["code"] == "DOCUMENT_DISPOSED"
    assert tool.body_calls == []
    assert seen and all(call["execute_safe"] == 1 for call in seen)
    assert all("bypass_thread_guard" not in call["kwargs"] for call in seen)
    assert all(call["kwargs"].get("note") == "keep" for call in seen)


def test_tools_call_keeps_real_arguments_when_stripping_bypass():
    tool = _SyncProbe()
    handler = _handler(_LiveDoc(), tool)
    seen = _watch_execute_safe(handler)
    result = handler._execute_long_running("sync_probe", {"bypass_thread_guard": True, "note": "keep"}, document_url="file:///live.odt")
    assert result["status"] == "ok"
    assert result["note"] == "keep"
    assert tool.body_calls == [{"note": "keep"}]
    assert seen == [{"kwargs": {"note": "keep"}, "execute_safe": 1}]


def _extract_runner(fn):
    """Extract the underlying runner callable from the closure passed to execute_on_main_thread."""
    if not hasattr(fn, "__closure__") or not fn.__closure__:
        return None
    free = dict(zip(fn.__code__.co_freevars, (cell.cell_contents for cell in fn.__closure__)))
    if "runner" in free:
        return free["runner"]
    for val in free.values():
        if callable(val) and hasattr(val, "__closure__") and val.__closure__:
            nested = _extract_runner(val)
            if nested is not None:
                return nested
    return None


def test_long_running_bypass_still_marshals_to_the_main_thread():
    """Sync tools/call must marshal execute_safe, not call tool.execute on the worker.

    A client bypass value used to call tool.execute on that worker. MCP must not pass the keyword.
    """
    tool = _SyncProbe()
    handler = _handler(_LiveDoc(), tool)
    seen = _watch_execute_safe(handler, record_safe=False)
    marshalled = []

    def fake_marshal(fn):
        marshalled.append(threading.current_thread().name)
        runner = _extract_runner(fn)
        assert getattr(runner, "__func__", None) is ToolBase.execute_safe
        return {"status": "ok", "marshalled": True}

    box = {}

    def worker():
        box["result"] = handler._mcp_tools_call({"name": "sync_probe", "arguments": {"bypass_thread_guard": True, "note": "keep"}})

    with patch("plugin.framework.tool.execute_on_main_thread", side_effect=fake_marshal):
        thread = threading.Thread(target=worker, name="http-worker")
        thread.start()
        thread.join()

    assert marshalled == ["http-worker"]
    assert tool.body_calls == []
    assert _payload(box["result"])["marshalled"] is True
    assert seen and all("bypass_thread_guard" not in call["kwargs"] for call in seen)
    assert all(set(call["kwargs"]) <= {"note"} for call in seen)


def test_backpressure_and_debug_bypass_still_marshals_to_the_main_thread():
    tool = _SyncProbe()
    tool.long_running = False
    handler = _handler(_LiveDoc(), tool)
    seen = _watch_execute_safe(handler, record_safe=False)
    marshalled = []

    def fake_marshal(fn):
        marshalled.append("marshal")
        runner = _extract_runner(fn)
        assert getattr(runner, "__func__", None) is ToolBase.execute_safe
        return {"status": "ok", "marshalled": True}

    box = {}

    def worker():
        box["debug"] = handler._debug_call_tool("sync_probe", {"bypass_thread_guard": True, "note": "keep"}, document_url="file:///live.odt")
        box["direct"] = handler._execute_with_backpressure("sync_probe", {"bypass_thread_guard": True}, document_url="file:///live.odt")

    with patch("plugin.framework.tool.execute_on_main_thread", side_effect=fake_marshal):
        thread = threading.Thread(target=worker, name="http-worker")
        thread.start()
        thread.join()

    assert marshalled == ["marshal", "marshal"]
    assert tool.body_calls == []
    assert box["debug"]["marshalled"] is True
    assert box["direct"]["marshalled"] is True
    assert len(seen) == 2
    assert all("bypass_thread_guard" not in call["kwargs"] for call in seen)


class _NoHeaders:
    def get(self, name, default=None):
        del name
        return default


def test_notification_batch_includes_session_id(monkeypatch):
    """HTTP 202 for a notifications-only batch must refresh Mcp-Session-Id."""
    import plugin.mcp.mcp_session as mcp_session

    monkeypatch.setattr(mcp_session, "_mcp_session_id", "sess-batch")
    handler = MagicMock()
    handler.headers = _NoHeaders()
    sent: list[tuple[str, str]] = []
    handler.send_header.side_effect = lambda key, value: sent.append((key, value))
    services = MagicMock()
    services.tools = MagicMock()
    mcp = MCPProtocolHandler(services)
    batch = [{"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}]
    mcp._handle_mcp(batch, handler)
    handler.send_response.assert_called_with(202)
    assert ("Mcp-Session-Id", "sess-batch") in sent


def test_stale_batch_containing_initialize_is_404(monkeypatch):
    import plugin.mcp.mcp_session as mcp_session

    monkeypatch.setattr(mcp_session, "_mcp_session_id", "live-session")

    class _DeadHeaders:
        def get(self, name, default=None):
            if name == "Mcp-Session-Id":
                return "dead-session"
            return default

    handler = MagicMock()
    handler.headers = _DeadHeaders()
    services = MagicMock()
    mcp = MCPProtocolHandler(services)
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]
    with patch.object(MCPProtocolHandler, "_process_jsonrpc") as mock_process:
        mcp._handle_mcp(batch, handler)
        mock_process.assert_not_called()
    handler.send_response.assert_called_with(404)


def test_debug_endpoint_auth_matrix(monkeypatch):
    from plugin.mcp.mcp_protocol import MCPProtocolHandler
    from tests.mcp.test_mcp_server import MockHandler

    services = MagicMock()
    handler = MCPProtocolHandler(services, debug_token="secret-token-123")

    for method in ("GET", "POST"):
        # 1. Loopback without token -> 403
        req = MockHandler({})
        req.client_address = ("127.0.0.1", 12345)
        if method == "GET":
            status, _ = handler.handle_debug_info(req)
            assert status == 403
        else:
            with patch.object(handler, "_read_body", return_value={}):
                handler.handle_debug_post(req)
            assert 403 in req.sent_responses

        # 2. Loopback with token -> 200 even when _shared_tunnel.is_running and with X-Forwarded-For
        mock_tunnel = MagicMock()
        mock_tunnel.is_running = True
        monkeypatch.setattr("plugin.mcp._shared_tunnel", mock_tunnel)
        req = MockHandler({
            "X-WriterAgent-Debug-Token": "secret-token-123",
            "X-Forwarded-For": "1.2.3.4",
        })
        req.client_address = ("127.0.0.1", 12345)
        if method == "GET":
            status, _ = handler.handle_debug_info(req)
            assert status == 200
        else:
            with patch.object(handler, "_read_body", return_value={}):
                handler.handle_debug_post(req)
            assert 200 in req.sent_responses

        # 3. Non-loopback peer with token -> 403
        req = MockHandler({"X-WriterAgent-Debug-Token": "secret-token-123"})
        req.client_address = ("192.168.1.50", 12345)
        if method == "GET":
            status, _ = handler.handle_debug_info(req)
            assert status == 403
        else:
            with patch.object(handler, "_read_body", return_value={}):
                handler.handle_debug_post(req)
            assert 403 in req.sent_responses

        # 4. Request with Origin header -> 403 with valid token
        req = MockHandler({
            "X-WriterAgent-Debug-Token": "secret-token-123",
            "Origin": "http://localhost:3000",
        })
        req.client_address = ("127.0.0.1", 12345)
        if method == "GET":
            status, _ = handler.handle_debug_info(req)
            assert status == 403
        else:
            with patch.object(handler, "_read_body", return_value={}):
                handler.handle_debug_post(req)
            assert 403 in req.sent_responses


@pytest.mark.parametrize("payload", [[1, 2], 1, "x", True])
def test_handle_debug_post_rejects_non_object_json(payload):
    from tests.mcp.test_mcp_server import MockHandler

    services = MagicMock()
    handler = MCPProtocolHandler(services, debug_token="test-token")

    raw = json.dumps(payload).encode("utf-8")
    mock_req = MockHandler({
        "Content-Length": str(len(raw)),
        "X-WriterAgent-Debug-Token": "test-token",
    }, body=raw)
    mock_req.client_address = ("127.0.0.1", 12345)
    handler.handle_debug_post(mock_req)

    assert 400 in mock_req.sent_responses
    assert 500 not in mock_req.sent_responses
    assert json.loads(mock_req.wfile.getvalue().decode("utf-8")) == {"error": "JSON body must be an object"}


def test_http_server_stop_ends_sse_keepalive():
    """stop() must wake the SSE request thread instead of leaving it in select."""
    import socket

    from plugin.mcp.routes import HttpRouteRegistry
    from plugin.mcp.server import HttpServer

    def _free_port() -> int:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def _read_until(sock: socket.socket, needle: bytes, timeout: float = 2.0) -> bytes:
        sock.settimeout(timeout)
        data = b""
        deadline = time.monotonic() + timeout
        while needle not in data:
            if time.monotonic() > deadline:
                raise AssertionError("timed out waiting for %r in %r" % (needle, data[:300]))
            chunk = sock.recv(4096)
            if not chunk:
                raise AssertionError("eof before %r in %r" % (needle, data[:300]))
            data += chunk
        return data

    def _alive(tcp_server):
        with tcp_server._sse_lock:
            return [thread for thread in list(tcp_server._sse_threads) if thread.is_alive()]

    def _wait_alive(tcp_server, want: bool, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            alive = bool(_alive(tcp_server))
            if alive == want:
                return True
            time.sleep(0.02)
        return bool(_alive(tcp_server)) == want

    services = MagicMock()
    services.tools = MagicMock()
    protocol = MCPProtocolHandler(services)
    registry = HttpRouteRegistry()
    registry.add("GET", "/mcp", protocol.handle_mcp_sse, raw=True)

    srv = None
    client = None
    last_error = None
    for _attempt in range(3):
        candidate = HttpServer(route_registry=registry, port=_free_port(), host="127.0.0.1")
        try:
            candidate.start()
            srv = candidate
            break
        except OSError as exc:
            last_error = exc
            candidate.stop()
    if srv is None or srv._server is None:
        pytest.fail("HTTP server did not bind: %s" % last_error)
    try:
        with patch("plugin.mcp.server.SSE_MAX_STREAMS", 1):
            for _spin in range(40):
                try:
                    client = socket.create_connection(("127.0.0.1", srv.port), timeout=2)
                    break
                except ConnectionRefusedError:
                    time.sleep(0.05)
            assert client is not None
            client.sendall(b"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\nConnection: keep-alive\r\n\r\n")
            _read_until(client, b": keepalive")
            assert _wait_alive(srv._server, True)

            client2 = socket.create_connection(("127.0.0.1", srv.port), timeout=2)
            try:
                client2.sendall(b"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n")
                resp = _read_until(client2, b"405")
                assert b"405" in resp
                assert b"Allow: POST, DELETE, OPTIONS" in resp
            finally:
                client2.close()

            assert len(_alive(srv._server)) == 1

            srv.stop()
            assert _wait_alive(srv._server, False), "SSE keepalive thread still alive after HttpServer.stop"
            client.settimeout(2)
            try:
                client.recv(4096)
            except socket.timeout:
                raise AssertionError("client socket still open after HttpServer.stop") from None
    finally:
        if client is not None:
            client.close()
        if srv is not None:
            srv.stop()


def test_sse_keepalive_readable_socket_uses_plain_recv():
    services = MagicMock()
    protocol = MCPProtocolHandler(services)

    sock = MagicMock()
    recv_calls = []
    recv_returns = [b"x", b""]

    def _recv(*args):
        recv_calls.append(args)
        if len(args) > 1 and args[1] != 0:
            raise ValueError("non-zero flags not allowed")
        if recv_returns:
            return recv_returns.pop(0)
        return b""

    sock.recv.side_effect = _recv

    http = MagicMock()
    http.connection = sock
    http.server = object()

    stop_event = threading.Event()

    with (
        patch("plugin.mcp.mcp_protocol.note_sse_keepalive", return_value=stop_event),
        patch("plugin.mcp.mcp_protocol.forget_sse_keepalive") as mock_forget,
        patch("plugin.mcp.mcp_protocol.select.select", return_value=([sock], [], [])),
        patch("plugin.mcp.mcp_protocol.log.debug") as mock_log_debug,
    ):
        protocol._run_sse_keepalive_loop(http, interval=30)

    mock_forget.assert_called_once()
    mock_log_debug.assert_not_called()
    assert sock.recv.call_count == 2
    assert recv_calls == [(4096,), (4096,)]


def test_mcp_execute_with_backpressure_captures_send_cancellation() -> None:
    from plugin.mcp.mcp_protocol import MCPProtocolHandler, _PreparedMcpCall
    from plugin.framework.queue_executor import QueueExecutor
    from plugin.framework.send_cancellation import SendCancellation

    executor = MagicMock(spec=QueueExecutor)
    # mock _PreparedMcpCall so it bypasses isinstance check and we don't need real docs
    prepared = MagicMock(spec=_PreparedMcpCall)
    prepared.doc_key = "test_key"
    prepared.needs_gate = False

    # execute returns prepared then something else
    executor.execute.side_effect = [prepared, "final_result"]

    services = MagicMock()
    handler = MCPProtocolHandler(services)
    handler.queue_executor = executor
    handler.tool_registry = MagicMock()
    handler.event_bus = MagicMock()

    scope = SendCancellation()

    with patch("plugin.framework.send_cancellation.get_current_send_cancellation", return_value=scope):
        res = handler._execute_with_backpressure("my_tool", {})

    assert res == "final_result"
    assert executor.execute.call_count == 2

    # first call should be _prepare_mcp_execution with bound_scope=scope and send_cancellation=scope
    prepare_call = executor.execute.call_args_list[0]
    assert prepare_call.args[0] == handler._prepare_mcp_execution
    assert prepare_call.kwargs.get("bound_scope") is scope
    assert prepare_call.kwargs.get("send_cancellation") is scope

def test_async_long_running_uses_execute_safe_without_bypass_keyword():
    """is_async tools are the long-running path and still use execute_safe."""

    class _AsyncProbe(ToolBase):
        name = "async_probe"
        description = "probe"
        parameters = {"type": "object", "properties": {"note": {"type": "string"}}}
        uno_services = None
        doc_types = None
        requires_document = True
        long_running = True
        timeout = 30

        def __init__(self):
            self.body_calls: list[dict] = []

        def is_async(self):
            return True

        def execute(self, ctx, **kwargs):
            self.body_calls.append(dict(kwargs))
            return {"status": "ok", "note": kwargs.get("note")}

    tool = _AsyncProbe()
    handler = _handler(_LiveDoc(), tool)
    seen = _watch_execute_safe(handler)
    marshalled = []

    def fail_marshal(fn):
        marshalled.append(fn)
        raise AssertionError("is_async MCP tool must not be marshalled to the main thread")

    with patch("plugin.framework.tool.execute_on_main_thread", side_effect=fail_marshal):
        result = handler._execute_long_running("async_probe", {"bypass_thread_guard": False, "note": "keep"}, document_url="file:///live.odt")

    assert marshalled == []
    assert result["status"] == "ok"
    assert result["note"] == "keep"
    assert tool.body_calls == [{"note": "keep"}]
    assert seen == [{"kwargs": {"note": "keep"}, "execute_safe": 1}]


def test_long_running_flag_without_is_async_stays_on_backpressure():
    """long_running=True is not a second path that passes bypass_thread_guard."""
    tool = _SyncProbe()
    tool.long_running = True
    handler = _handler(_LiveDoc(), tool)
    with patch.object(handler, "_execute_long_running", side_effect=AssertionError("long_running flag must not select the worker path")) as mock_lr, patch.object(handler, "_execute_with_backpressure", return_value={"status": "ok"}) as mock_bp:
        handler._mcp_tools_call({"name": "sync_probe", "arguments": {"bypass_thread_guard": True}})
    assert mock_bp.called
    assert not mock_lr.called


def test_async_tools_are_long_running():
    from plugin.framework.tool import ToolBase, ToolRegistry
    from plugin.mcp.mcp_protocol import MCPProtocolHandler
    from unittest.mock import patch, MagicMock

    class MockAsyncTool(ToolBase):
        name = "async_mock"
        timeout = 10

        def execute(self, ctx, **kwargs):
            return {"status": "ok"}

        def is_async(self):
            return True

    registry = ToolRegistry(MagicMock())
    registry._tools["async_mock"] = MockAsyncTool()
    handler = MCPProtocolHandler(MagicMock(tool_registry=registry))
    handler.tool_registry = registry
    with patch.object(handler, "_execute_long_running", return_value={"status": "ok"}) as mock_lr:
        handler._mcp_tools_call({"name": "async_mock", "arguments": {}})
        assert mock_lr.called


def test_document_url_not_popped_when_declared():
    from plugin.framework.tool import ToolBase, ToolRegistry
    from plugin.mcp.mcp_protocol import MCPProtocolHandler
    from unittest.mock import patch, MagicMock

    class MockDocUrlTool(ToolBase):
        name = "doc_url_mock"
        parameters = {"properties": {"document_url": {"type": "string"}}}

        def execute(self, ctx, **kwargs):
            return {"status": "ok"}

    registry = ToolRegistry(MagicMock())
    registry._tools["doc_url_mock"] = MockDocUrlTool()
    handler = MCPProtocolHandler(MagicMock(tool_registry=registry))
    handler.tool_registry = registry
    with patch.object(handler, "_execute_with_backpressure", return_value={"status": "ok"}) as mock_bp:
        handler._mcp_tools_call({"name": "doc_url_mock", "arguments": {"document_url": "test"}})
        assert mock_bp.call_args[0][1] == {"document_url": "test"}


def test_timeout_zero_rejects_async_tool():
    from plugin.framework.tool import ToolBase, ToolRegistry
    from plugin.mcp.mcp_protocol import MCPProtocolHandler
    from unittest.mock import MagicMock

    class MockAsyncZeroTimeoutTool(ToolBase):
        name = "async_zero_timeout"
        timeout = 0

        def execute(self, ctx, **kwargs):
            return {"status": "ok"}

        def is_async(self):
            return True

    registry = ToolRegistry(MagicMock())
    registry._tools["async_zero_timeout"] = MockAsyncZeroTimeoutTool()
    handler = MCPProtocolHandler(MagicMock(tool_registry=registry))
    handler.tool_registry = registry
    result = handler._prepare_mcp_execution("async_zero_timeout", {}, None)
    assert isinstance(result, dict)
    assert result["status"] == "error"
    assert result["code"] == "TOOL_EXECUTION_ERROR"


def test_async_tool_without_timeout_runs():
    from plugin.framework.tool import ToolBase, ToolRegistry
    from plugin.mcp.mcp_protocol import MCPProtocolHandler
    from unittest.mock import MagicMock

    class MockAsyncNoTimeoutTool(ToolBase):
        name = "async_no_timeout"

        def execute(self, ctx, **kwargs):
            return {"status": "ok"}

        def is_async(self):
            return True

    registry = ToolRegistry(MagicMock())
    registry._tools["async_no_timeout"] = MockAsyncNoTimeoutTool()
    handler = MCPProtocolHandler(MagicMock(tool_registry=registry))
    handler.tool_registry = registry
    result = handler._prepare_mcp_execution("async_no_timeout", {}, None)
    assert not isinstance(result, dict) or result.get("status") != "error"


def test_named_async_tools_declare_positive_timeout():
    """Async tools must declare a positive MCP timeout.

    RunMerge and ApplyStyle stay synchronous (they mutate UNO on the LibreOffice
    main thread), so this check does not require them to be async. Tools that
    remain async still must declare timeout > 0.
    """
    from plugin.chatbot.writing import WriteDocumentSection
    from plugin.ppt_master.tools import ExportPresentationProject

    for tool in (WriteDocumentSection(), ExportPresentationProject()):
        assert tool.is_async() is True
        assert getattr(tool, "timeout", 0) > 0



def test_is_async_uses_long_running_path():
    """Tools with is_async() exactly True run via _execute_long_running.

    long_running=False must not keep an async tool on the backpressure path.
    MagicMock is not async; only an actual True selects this path.
    """
    from unittest.mock import patch

    class _AsyncProbe(ToolBase):
        name = "async_probe"
        description = "probe"
        parameters = {"type": "object", "properties": {}}
        is_mutation = False
        long_running = False
        requires_document = False
        timeout = 30

        def is_async(self) -> bool:
            return True

        def execute(self, ctx, **kwargs):
            return {"status": "ok"}

    tool = _AsyncProbe()
    handler = _handler(None, tool)

    with patch.object(handler, "_execute_long_running", return_value={"status": "ok", "from_long_running": True}) as mock_long:
        with patch.object(handler, "_execute_with_backpressure") as mock_backpressure:
            result = handler._mcp_tools_call({"name": "async_probe", "arguments": {}})

            mock_long.assert_called_once_with("async_probe", {}, document_url=None, req_id=None)
            mock_backpressure.assert_not_called()

            payload = _payload(result)
            assert payload.get("from_long_running") is True


def test_initialize_with_supported_older_version_passes(mcp_server, monkeypatch):
    """Test 1: initialize with 2025-06-18 then a POST with that MCP-Protocol-Version header passes."""
    import urllib.request
    import json

    # Init
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
    }
    req = urllib.request.Request(f"{mcp_server}/mcp", method="POST", data=json.dumps(init_payload).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 200
        res = json.loads(response.read().decode())
        assert res["result"]["protocolVersion"] == "2025-06-18"

    # POST
    post_payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "ping"
    }
    req2 = urllib.request.Request(f"{mcp_server}/mcp", method="POST", data=json.dumps(post_payload).encode("utf-8"))
    req2.add_header("Content-Type", "application/json")
    req2.add_header("Mcp-Protocol-Version", "2025-06-18")
    with urllib.request.urlopen(req2, timeout=5) as response2:
        assert response2.status == 200

def test_initialize_with_unsupported_version_returns_server_version(mcp_server, monkeypatch):
    """Test 2: initialize with foo returns the server version."""
    import urllib.request
    import json
    from plugin.mcp.wire_types import MCP_PROTOCOL_VERSION

    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "foo", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
    }
    req = urllib.request.Request(f"{mcp_server}/mcp", method="POST", data=json.dumps(init_payload).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 200
        res = json.loads(response.read().decode())
        assert res["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION


def test_post_does_not_register_sse_keepalive(mcp_server, monkeypatch):
    """POST /sse and /mcp must NOT register in note_sse_keepalive (only SSE stream threads are joined in stop)."""
    import urllib.request
    import json

    events = []

    import plugin.mcp.mcp_protocol as prot
    orig_note = prot.note_sse_keepalive

    def fake_note(server, sock):
        events.append("note")
        return orig_note(server, sock)

    monkeypatch.setattr(prot, "note_sse_keepalive", fake_note)

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "ping"
    }
    req = urllib.request.Request(f"{mcp_server}/sse", method="POST", data=json.dumps(payload).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 200

    # POST handlers must not register keepalives
    assert "note" not in events


def test_initialize_non_string_protocol_version_returns_invalid_params():
    """Non-string protocolVersion (e.g. list, dict, int) must return JSON-RPC INVALID_PARAMS (-32602), not HTTP 500."""
    from unittest.mock import MagicMock
    from plugin.mcp.mcp_protocol import MCPProtocolHandler
    from plugin.mcp.wire_types import INVALID_PARAMS

    services = MagicMock()
    protocol = MCPProtocolHandler(services)

    for bad_version in ([1, 2], {"ver": "1"}, 123, True):
        msg = {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "initialize",
            "params": {"protocolVersion": bad_version, "capabilities": {}}
        }
        res = protocol._process_jsonrpc(msg)
        assert res is not None
        status, response = res
        assert status == 400
        assert "error" in response
        assert response["error"]["code"] == INVALID_PARAMS
        assert "string" in response["error"]["message"].lower()


def test_protocol_handler_version_is_extension_version():
    from unittest.mock import MagicMock
    from plugin.mcp.mcp_protocol import MCPProtocolHandler
    from plugin.version import EXTENSION_VERSION

    handler = MCPProtocolHandler(MagicMock())
    assert handler.version == EXTENSION_VERSION
    init_res = handler._mcp_initialize({"protocolVersion": "2024-11-05"})
    assert init_res["serverInfo"]["version"] == EXTENSION_VERSION


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"jsonrpc": "2.0", "id": 7, "method": "", "params": {}}, 7),
        ({"jsonrpc": "2.0", "id": 3, "method": "ping", "params": []}, 3),
        ({"jsonrpc": "1.0", "id": "abc", "method": "ping"}, "abc"),
        ({"jsonrpc": "2.0", "id": True, "method": "ping", "params": {}}, None),
    ],
)
def test_process_jsonrpc_echoes_detectable_id(message, expected):
    from plugin.mcp import wire_types

    status, body = MCPProtocolHandler(MagicMock())._process_jsonrpc(message)
    assert status == 400
    assert body["id"] == expected
    assert body["error"]["code"] == wire_types.INVALID_REQUEST


def test_tool_exposure_mode_config_error_propagates():
    from unittest.mock import MagicMock
    from plugin.mcp.mcp_protocol import MCPProtocolHandler

    services = MagicMock()
    services.config.get.side_effect = RuntimeError("config")
    handler = MCPProtocolHandler(services)
    with pytest.raises(RuntimeError, match="config"):
        handler._tool_exposure_mode()


def test_real_active_document_does_not_swallow_supports_service():
    from unittest.mock import MagicMock
    from plugin.mcp.mcp_protocol import _real_active_document

    doc = MagicMock()
    doc.supportsService.side_effect = RuntimeError("disposed")
    svc = MagicMock()
    svc.get_active_document.return_value = doc
    with pytest.raises(RuntimeError, match="disposed"):
        _real_active_document(svc)

    doc.supportsService.side_effect = None
    doc.supportsService.return_value = False
    assert _real_active_document(svc) is None


def test_resolve_exception_is_not_document_not_found():
    from unittest.mock import MagicMock
    from plugin.framework.errors import DocumentDisposedError
    from plugin.mcp.mcp_protocol import MCPProtocolHandler

    services = MagicMock()
    tool = MagicMock()
    tool.requires_document = True
    services.tools.get.return_value = tool
    services.document.resolve_document_by_url.side_effect = DocumentDisposedError("closed", object_type="Document")
    handler = MCPProtocolHandler(services)
    with pytest.raises(DocumentDisposedError):
        handler._prepare_mcp_execution("apply_document_content", {}, document_url="file:///gone.odt")


def test_unresolved_document_url_is_still_document_not_found():
    from unittest.mock import MagicMock
    from plugin.mcp.mcp_protocol import MCPProtocolHandler

    services = MagicMock()
    tool = MagicMock()
    tool.requires_document = True
    services.tools.get.return_value = tool
    services.document.resolve_document_by_url.return_value = (None, None)
    handler = MCPProtocolHandler(services)
    result = handler._prepare_mcp_execution("apply_document_content", {}, document_url="file:///missing.odt")
    assert result["code"] == "DOCUMENT_NOT_FOUND"
    assert "missing.odt" in result["message"]


def test_open_documents_failure_is_not_no_document_open():
    from unittest.mock import MagicMock
    from plugin.mcp.mcp_protocol import MCPProtocolHandler

    services = MagicMock()
    tool = MagicMock()
    tool.requires_document = True
    services.tools.get.return_value = tool
    services.document.get_active_document.return_value = None
    services.document.open_documents.side_effect = RuntimeError("desktop")
    handler = MCPProtocolHandler(services)
    with pytest.raises(RuntimeError, match="desktop"):
        handler._prepare_mcp_execution("t", {})


def test_draw_page_index_failure_propagates():
    from unittest.mock import MagicMock, patch
    from plugin.mcp.mcp_protocol import MCPProtocolHandler

    services = MagicMock()
    tool = MagicMock()
    tool.requires_document = True
    services.tools.get.return_value = tool
    services.document.resolve_document_by_url.return_value = (MagicMock(), "draw")
    handler = MCPProtocolHandler(services)
    with patch("plugin.draw.bridge.DrawBridge", side_effect=RuntimeError("no page")):
        with pytest.raises(RuntimeError, match="no page"):
            handler._prepare_mcp_execution("t", {}, document_url="file:///a.odg")


def test_sse_keepalive_unexpected_error_is_logged(caplog):
    import logging
    import threading
    from unittest.mock import MagicMock, patch
    from plugin.mcp.mcp_protocol import MCPProtocolHandler

    protocol = MCPProtocolHandler(MagicMock())
    http = MagicMock()
    http.wfile.write.side_effect = RuntimeError("bug")
    http.connection = MagicMock()
    http.server = object()
    stop_event = threading.Event()
    with (
        patch("plugin.mcp.mcp_protocol.note_sse_keepalive", return_value=stop_event),
        patch("plugin.mcp.mcp_protocol.forget_sse_keepalive") as mock_forget,
        caplog.at_level(logging.ERROR, logger="writeragent.mcp.protocol"),
    ):
        protocol._run_sse_keepalive_loop(http, interval=30)
    mock_forget.assert_called_once()
    assert any(r.exc_info and "SSE keepalive loop exception" in r.getMessage() for r in caplog.records)

