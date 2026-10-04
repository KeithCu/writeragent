# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""MCP protocol document echo and tools/call thread-guard. No LibreOffice."""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.tool import ToolBase, ToolRegistry
from plugin.mcp.mcp_protocol import MCPProtocolHandler


def test_attach_document_echo_shape_and_absence():
    from plugin.mcp.mcp_protocol import _attach_document_echo

    doc = MagicMock()
    doc.URL = "file:///Users/x/Peti%C3%A7%C3%A3o%20Inicial.odt"
    doc.RuntimeUID = "42"
    result = {"status": "ok"}
    _attach_document_echo(result, doc)
    assert result["document"]["name"] == "Petição Inicial.odt"
    assert result["document"]["uid"]
    # No doc -> no field; existing field -> untouched.
    r2 = {"status": "ok"}
    _attach_document_echo(r2, None)
    assert "document" not in r2
    r3 = {"status": "ok", "document": {"name": "keep"}}
    _attach_document_echo(r3, doc)
    assert r3["document"]["name"] == "keep"


def test_long_running_precomputes_echo_without_post_execute_doc_access():
    """Echo is captured once inside _prepare_mcp_execution (main-thread marshal); the worker path must not
    call _attach_document_echo or re-read the proxied doc after the tool body runs."""
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

    with patch("plugin.mcp.mcp_protocol._document_echo_payload", return_value={"name": "doc.odt", "uid": "uid-1"}) as mock_echo, patch("plugin.mcp.mcp_protocol._attach_document_echo") as mock_attach:
        result = handler._execute_long_running("any_tool", {}, document_url="file:///doc.odt")

    assert result["status"] == "ok"
    assert result["document"] == {"name": "doc.odt", "uid": "uid-1"}
    assert mock_echo.call_count == 1
    assert mock_attach.call_count == 0


class _DisposedDoc:
    """getImplementationName raises DisposedException, so execute_safe reports DOCUMENT_DISPOSED."""

    def getURL(self):
        return "file:///gone.odt"

    def getImplementationName(self):
        raise type("DisposedException", (Exception,), {})("disposed")


class _LiveDoc:
    def getURL(self):
        return "file:///live.odt"


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
        free = dict(zip(fn.__code__.co_freevars, (cell.cell_contents for cell in fn.__closure__)))
        runner = free["runner"]
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
        free = dict(zip(fn.__code__.co_freevars, (cell.cell_contents for cell in fn.__closure__)))
        runner = free["runner"]
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
    import plugin.mcp.mcp_protocol as proto

    monkeypatch.setattr(proto, "_mcp_session_id", "sess-batch")
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


def test_handle_debug_post_blocks_tunneled_request(monkeypatch):
    from plugin.mcp.mcp_protocol import MCPProtocolHandler
    from tests.mcp.test_mcp_server import MockHandler

    services = MagicMock()
    handler = MCPProtocolHandler(services)

    # Test proxy header block
    req1 = MockHandler({"X-Forwarded-For": "1.2.3.4"})
    handler.handle_debug_post(req1)
    assert 403 in req1.sent_responses

    req2 = MockHandler({"Cf-Ray": "12345"})
    handler.handle_debug_post(req2)
    assert 403 in req2.sent_responses

    # Test active tunnel block
    req3 = MockHandler({})
    mock_tunnel = MagicMock()
    mock_tunnel.is_running = True
    monkeypatch.setattr("plugin.mcp._shared_tunnel", mock_tunnel)
    handler.handle_debug_post(req3)
    assert 403 in req3.sent_responses

    # Valid localhost request (no active tunnel)
    req4 = MockHandler({})
    mock_tunnel.is_running = False
    with patch.object(handler, "_read_body", return_value={}):
        handler.handle_debug_post(req4)
    assert 200 in req4.sent_responses


def test_http_server_stop_ends_sse_keepalive():
    """stop() must wake the SSE request thread instead of leaving it in select."""
    import socket
    import time

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

def test_mcp_execute_with_backpressure_captures_send_cancellation() -> None:
    from plugin.mcp.mcp_protocol import MCPProtocolHandler, _PreparedMcpCall
    from plugin.framework.queue_executor import SendCancellation, QueueExecutor

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

    with patch("plugin.framework.queue_executor.get_current_send_cancellation", return_value=scope):
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
    assert isinstance(result, dict)
    assert result["status"] == "error"
    assert result["code"] == "TOOL_EXECUTION_ERROR"


def test_named_async_tools_declare_positive_timeout():
    """Tools moved off the UI thread must satisfy the MCP async timeout gate."""
    from plugin.chatbot.writing import WriteDocumentSection
    from plugin.ppt_master.tools import ExportPresentationProject
    from plugin.writer.specialized.mail_merge import RunMerge

    for tool in (WriteDocumentSection(), ExportPresentationProject(), RunMerge()):
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
