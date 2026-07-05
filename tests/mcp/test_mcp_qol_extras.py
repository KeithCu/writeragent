# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""MCP-experience QoL extras: proxy-safe is_active, the per-result document echo, and the
multi-document guidance topic. No LibreOffice required."""
from unittest.mock import MagicMock

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()


def test_is_same_document_uses_uid_not_identity():
    from plugin.doc.document_research import _is_same_document

    class Doc:
        def __init__(self, uid, url=""):
            self.RuntimeUID = uid
            self.URL = url

    # Distinct proxy-like objects, same uid -> same document (object identity was always False).
    assert _is_same_document(Doc("u1"), Doc("u1")) is True
    assert _is_same_document(Doc("u1"), Doc("u2")) is False
    assert _is_same_document(None, Doc("u1")) is False
    assert _is_same_document(Doc("u1"), None) is False
    # No uid on either side -> fall back to URL equality (empty URL never matches).
    a, b = Doc(None, "file:///x.odt"), Doc(None, "file:///x.odt")
    assert _is_same_document(a, b) is True
    assert _is_same_document(Doc(None, ""), Doc(None, "")) is False


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


def test_multidoc_guidance_reaches_mcp_topic():
    from plugin.framework.agent_manual import get_section, normalize_topic

    sec = get_section("concurrency", "writer")
    assert "document_url" in sec and "list_open_documents" in sec
    assert normalize_topic("multi-document") == "concurrency"
    assert normalize_topic("document_url") == "concurrency"


def test_long_running_precomputes_echo_without_post_execute_doc_access():
    """Echo is captured once inside _get_context (main-thread marshal); the worker path must not
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
