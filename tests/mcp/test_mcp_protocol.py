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
    """Real tool so execute_safe's disposed-document check runs. long_running defaults to True."""

    name = "sync_probe"
    description = "probe"
    parameters = {"type": "object", "properties": {"note": {"type": "string"}}}
    uno_services = None
    doc_types = None
    is_mutation = True
    long_running = True
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


def _payload(result):
    if isinstance(result, dict) and "content" in result:
        return json.loads(result["content"][0]["text"])
    return result


@pytest.mark.parametrize("bypass", [True, 1, "yes"])
def test_tools_call_cannot_skip_disposed_document_check(bypass):
    """A client bypass_thread_guard value must still go through execute_safe.

    What was wrong: the key bound to ToolRegistry.execute's keyword-only flag,
    which calls tool.execute and skips the disposed-document check.
    """
    tool = _SyncProbe()
    handler = _handler(_DisposedDoc(), tool)
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


def test_tools_call_keeps_real_arguments_when_stripping_bypass():
    tool = _SyncProbe()
    handler = _handler(_LiveDoc(), tool)
    result = handler._execute_long_running(
        "sync_probe",
        {"bypass_thread_guard": True, "note": "keep"},
        document_url="file:///live.odt",
    )
    assert result["status"] == "ok"
    assert result["note"] == "keep"
    assert tool.body_calls == [{"note": "keep"}]


def test_long_running_bypass_still_marshals_to_the_main_thread():
    """Sync long-running tools/call must not run the body on the HTTP worker.

    bypass_thread_guard=True used to call tool.execute on that worker.
    """
    tool = _SyncProbe()
    handler = _handler(_LiveDoc(), tool)
    marshalled = []

    def fake_marshal(fn):
        marshalled.append(threading.current_thread().name)
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


def test_backpressure_and_debug_bypass_still_marshals_to_the_main_thread():
    tool = _SyncProbe()
    tool.long_running = False
    handler = _handler(_LiveDoc(), tool)
    marshalled = []

    def fake_marshal(fn):
        marshalled.append("marshal")
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
