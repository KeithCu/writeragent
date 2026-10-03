# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""MCP protocol document echo. No LibreOffice."""
from unittest.mock import MagicMock


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

    with patch("plugin.mcp.mcp_protocol._document_echo_payload",
               return_value={"name": "doc.odt", "uid": "uid-1"}) as mock_echo, \
         patch("plugin.mcp.mcp_protocol._attach_document_echo") as mock_attach:
        result = handler._execute_long_running("any_tool", {}, document_url="file:///doc.odt")

    assert result["status"] == "ok"
    assert result["document"] == {"name": "doc.odt", "uid": "uid-1"}
    assert mock_echo.call_count == 1
    assert mock_attach.call_count == 0


def test_client_arguments_cannot_set_bypass_thread_guard():
    """tools/call and /debug call_tool must not bind ToolRegistry.execute's privileged flag.

    The flag is keyword-only. Splatting a client ``bypass_thread_guard: true``
    skipped execute_safe and ran UNO off the main thread.
    """
    import threading

    from plugin.framework.tool import ToolBase, ToolRegistry
    from plugin.mcp.mcp_protocol import MCPProtocolHandler

    class _Probe(ToolBase):
        name = "probe_tool"
        description = "probe"
        parameters = {"type": "object", "properties": {"text": {"type": "string"}}}
        is_mutation = True

        def __init__(self):
            self.path = []

        def execute(self, ctx, **kwargs):
            self.path.append(("execute", kwargs.get("text"), "bypass_thread_guard" in kwargs))
            return {"status": "ok", "text": kwargs.get("text")}

        def execute_safe(self, ctx, **kwargs):
            self.path.append(("execute_safe", kwargs.get("text"), "bypass_thread_guard" in kwargs))
            return {"status": "ok", "text": kwargs.get("text")}

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

    probe = _Probe()
    registry = ToolRegistry(None)
    registry.register(probe)
    handler = MCPProtocolHandler(_FakeServices(registry))
    marshalled = []

    def _marshal(fn, *args, **kwargs):
        marshalled.append(threading.current_thread().name)
        return fn(*args, **kwargs)

    from unittest.mock import patch

    client_args = {"text": "hi", "bypass_thread_guard": True}
    with patch("plugin.framework.tool.execute_on_main_thread", side_effect=_marshal):
        long_result = handler._execute_long_running("probe_tool", client_args, document_url="file:///probe.odt")
        bp_result = handler._execute_with_backpressure("probe_tool", dict(client_args), document_url="file:///probe.odt")
        debug_result = handler._debug_call_tool("probe_tool", dict(client_args), document_url="file:///probe.odt")
        direct_result = handler._execute_tool_on_main("probe_tool", dict(client_args), document_url="file:///probe.odt")

    assert client_args["bypass_thread_guard"] is True  # caller dict is not mutated
    for result in (long_result, bp_result, debug_result, direct_result):
        assert result["status"] == "ok"
        assert result["text"] == "hi"
    assert probe.path == [("execute_safe", "hi", False)] * 4
    assert len(marshalled) == 4


def test_document_mutation_gate_does_not_block_the_main_thread():
    """A contended gate on the VCL thread raises BusyError instead of waiting 30s."""
    import time

    from plugin.mcp.mcp_protocol import BusyError, _document_mutation_gate, _get_document_mutation_gate

    key = "test-main-thread-gate-does-not-block"
    gate = _get_document_mutation_gate(key)
    assert gate.acquire(timeout=1.0)
    try:
        started = time.perf_counter()
        try:
            with _document_mutation_gate(key, enabled=True, timeout=30.0):
                raise AssertionError("gate should not have been acquired on the main thread")
        except BusyError as exc:
            assert "busy" in str(exc).lower()
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0, "main thread waited on the document gate for %.2fs" % elapsed
    finally:
        gate.release()


def test_document_mutation_gate_still_waits_off_the_main_thread():
    """HTTP workers keep the 30s wait. Only the VCL thread fails fast."""
    import threading
    import time

    from plugin.mcp.mcp_protocol import _document_mutation_gate, _get_document_mutation_gate

    key = "test-worker-thread-gate-waits"
    gate = _get_document_mutation_gate(key)
    assert gate.acquire(timeout=1.0)
    entered = threading.Event()
    errors = []

    def worker():
        try:
            with _document_mutation_gate(key, enabled=True, timeout=2.0):
                entered.set()
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (type(exc).__name__, exc))

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        time.sleep(0.1)
        assert not entered.is_set(), "worker should still be waiting on the gate"
        assert not errors
    finally:
        gate.release()
        thread.join(timeout=2)
    assert entered.is_set()
    assert not errors
    assert not thread.is_alive()
