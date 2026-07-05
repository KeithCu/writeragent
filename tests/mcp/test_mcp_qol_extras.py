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


def test_long_running_context_precomputes_echo_off_the_worker_thread():
    """The echo payload must be computed on the MAIN thread (inside _get_context): reading
    doc.URL/RuntimeUID from the HTTP worker trips the UNO thread guard on proxied docs."""
    import inspect

    from plugin.mcp import mcp_protocol as mp

    src = inspect.getsource(mp.MCPProtocolHandler._execute_long_running)
    assert "_document_echo_payload(doc)" in src.split("queue_executor.execute")[0], \
        "echo must be captured inside _get_context (main thread), before the worker resumes"
    assert "_attach_document_echo" not in src, \
        "the worker thread must not touch the proxied doc after execution"
