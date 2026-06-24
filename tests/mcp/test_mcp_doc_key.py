# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless tests for the MCP mutation-gate key.

The per-document mutation gate must give the SAME key whether a document is addressed by its file
URL or by its RuntimeUID, so two concurrent mutating MCP calls on that document serialize on one
lock instead of racing on two.
"""
from plugin.doc.document_helpers import _normalize_doc_url
from plugin.mcp.mcp_protocol import _ACTIVE_DOCUMENT_SENTINEL, _resolve_mcp_doc_key


class Doc:
    def __init__(self, uid="", url=""):
        self._uid = uid
        self._url = url

    @property
    def RuntimeUID(self):
        return self._uid

    def getURL(self):
        return self._url


def test_same_doc_by_url_and_by_uid_map_to_one_key():
    # The invariant: addressing one resolved document by URL or by uid -> identical gate key.
    doc = Doc(uid=42, url="file:///docs/a.odt")
    k_url = _resolve_mcp_doc_key("file:///docs/a.odt", doc)
    k_uid = _resolve_mcp_doc_key("42", doc)
    assert k_url == k_uid == "uid:42"


def test_saved_doc_without_uid_keys_on_url():
    doc = Doc(uid="", url="file:///docs/a.odt")
    assert _resolve_mcp_doc_key("file:///docs/a.odt", doc) == "url:" + _normalize_doc_url("file:///docs/a.odt")


def test_unsaved_active_doc_keys_on_uid_not_sentinel():
    # An unsaved doc has no URL but still has a uid -> addressable by a stable key, not the sentinel.
    doc = Doc(uid=7, url="")
    assert _resolve_mcp_doc_key(None, doc) == "uid:7"


def test_unresolved_doc_falls_back_to_request_url():
    assert _resolve_mcp_doc_key("file:///docs/x.odt", None) == "url:" + _normalize_doc_url("file:///docs/x.odt")


def test_unresolved_uid_request_cannot_collide_with_resolved_uid_key():
    # A request literally "uid:7" that fails to resolve must NOT share a gate with the real doc 7.
    real = Doc(uid=7, url="")
    assert _resolve_mcp_doc_key("uid:7", None) != _resolve_mcp_doc_key(None, real)


def test_no_doc_no_url_is_active_sentinel():
    assert _resolve_mcp_doc_key(None, None) == _ACTIVE_DOCUMENT_SENTINEL


def test_two_different_docs_do_not_collide():
    a = Doc(uid=1, url="file:///docs/a.odt")
    b = Doc(uid=2, url="file:///docs/b.odt")
    assert _resolve_mcp_doc_key("file:///docs/a.odt", a) != _resolve_mcp_doc_key("2", b)
