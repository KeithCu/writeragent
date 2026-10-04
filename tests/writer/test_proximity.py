"""Proximity flat-list cache must follow the heading tree it is handed."""

from unittest.mock import MagicMock

import pytest

from plugin.doc.document_helpers import DocumentService
from plugin.framework.errors import ToolExecutionError
from plugin.tests.testing_utils import ElementStub, WriterDocStub
from plugin.writer.navigation import NavHeading
from plugin.writer.proximity import ProximityService
from plugin.writer.tree import TreeService


def _root(*pairs):
    return {
        "children": [
            {"text": text, "para_index": para, "level": 1, "children": []}
            for text, para in pairs
        ]
    }


def _proximity(doc_key="uid:1"):
    services = MagicMock()
    services.document.doc_key.return_value = doc_key
    services.events.subscribe = MagicMock()
    return ProximityService(services)


def test_same_tree_and_character_count_reuses_flat_list():
    prox = _proximity()
    doc = MagicMock()
    doc.CharacterCount = 10
    root = _root(("Alpha", 1), ("Beta", 3))
    first = prox._flatten_tree(root, doc)
    assert prox._flatten_tree(root, doc) is first
    assert [entry["node"]["text"] for entry in first] == ["Alpha", "Beta"]


def test_moved_character_count_rebuilds_flat_list():
    prox = _proximity()
    doc = MagicMock()
    doc.CharacterCount = 10
    old = _root(("Alpha", 1), ("Beta", 3))
    first = prox._flatten_tree(old, doc)
    doc.CharacterCount = 18
    rebuilt = _root(("Alpha", 0), ("Gamma", 1), ("Beta", 2))
    second = prox._flatten_tree(rebuilt, doc)
    assert second is not first
    assert [entry["node"]["para_index"] for entry in second] == [0, 1, 2]


def test_new_tree_object_is_not_ignored_when_count_is_unchanged():
    """A rebuilt root with the same CharacterCount still replaces the flat list."""
    prox = _proximity()
    doc = MagicMock()
    doc.CharacterCount = 10
    prox._flatten_tree(_root(("Alpha", 1)), doc)
    second = prox._flatten_tree(_root(("Gamma", 4)), doc)
    assert second[0]["node"]["text"] == "Gamma"
    assert second[0]["node"]["para_index"] == 4


def test_no_character_count_keeps_listener_cache_until_the_tree_object_changes():
    prox = _proximity()
    doc = MagicMock()
    root = _root(("Alpha", 1))
    first = prox._flatten_tree(root, doc)
    assert prox._flatten_tree(root, doc) is first
    second = prox._flatten_tree(_root(("Beta", 2)), doc)
    assert second[0]["node"]["text"] == "Beta"


def test_invalidate_by_key_drops_fingerprint_and_root():
    prox = _proximity()
    doc = MagicMock()
    doc.CharacterCount = 10
    prox._flatten_tree(_root(("Alpha", 1)), doc)
    prox._on_cache_invalidated(key="uid:1")
    assert prox._flat_cache == {}
    assert prox._flat_fp == {}
    assert prox._flat_root == {}
    fresh = prox._flatten_tree(_root(("Beta", 5)), doc)
    assert fresh[0]["node"]["para_index"] == 5


def test_unknown_doc_key_is_not_cached():
    from plugin.doc.document_helpers import UNKNOWN_DOC_KEY

    prox = _proximity(UNKNOWN_DOC_KEY)
    doc = MagicMock()
    doc.CharacterCount = 4
    flat = prox._flatten_tree(_root(("Alpha", 2)), doc)
    assert flat[0]["node"]["para_index"] == 2
    assert prox._flat_cache == {}


def _nav_stack(elements):
    doc = WriterDocStub(elements)
    doc.CharacterCount = 10
    services = MagicMock()
    doc_svc = DocumentService()
    doc_svc.doc_key = lambda _doc: "uid:edit"
    services.document = doc_svc
    bookmarks = MagicMock()
    bookmarks.ensure_heading_bookmarks.return_value = {}
    bookmarks.get_mcp_bookmark_map.return_value = {}
    services.writer_bookmarks = bookmarks
    services.events.subscribe = MagicMock()
    services.writer_tree = TreeService(services)
    return doc, ProximityService(services)


def test_navigate_heading_text_does_not_start_at_paragraph_zero():
    doc, prox = _nav_stack(
        [
            ElementStub("Preamble"),
            ElementStub("Alpha", outline_level=1),
            ElementStub("body"),
            ElementStub("Beta", outline_level=1),
        ]
    )
    result = prox.navigate_heading(doc, "heading_text:Alpha", "next")
    assert result["heading"]["text"] == "Beta"
    assert result["heading"]["para_index"] == 3


def test_navigate_next_follows_tree_rebuilt_after_character_count_moves():
    elements = [
        ElementStub("Preamble"),
        ElementStub("Alpha", outline_level=1),
        ElementStub("body"),
        ElementStub("Beta", outline_level=1),
    ]
    doc, prox = _nav_stack(elements)
    first = prox.navigate_heading(doc, "heading_text:Alpha", "next")
    assert first["heading"]["text"] == "Beta"

    elements[:] = [
        ElementStub("Alpha", outline_level=1),
        ElementStub("Gamma", outline_level=1),
        ElementStub("Beta", outline_level=1),
    ]
    doc.CharacterCount = 18
    second = prox.navigate_heading(doc, "heading_text:Alpha", "next")
    assert second["heading"]["text"] == "Gamma"
    assert second["heading"]["para_index"] == 1


def test_nav_heading_tool_reports_unresolved_locator():
    doc, prox = _nav_stack([ElementStub("Alpha", outline_level=1)])
    ctx = MagicMock()
    ctx.doc = doc
    ctx.services.writer_proximity = prox
    result = NavHeading().execute(ctx, locator="heading_text:Missing", direction="next")
    assert result["status"] == "error"
    assert "No heading matching 'Missing'" in result["message"]


def test_navigate_missing_bookmark_raises():
    doc, prox = _nav_stack([ElementStub("Alpha", outline_level=1)])

    class _Bookmarks:
        def hasByName(self, _name):
            return False

        def getElementNames(self):
            return ["_mcp_live"]

    doc.getBookmarks = lambda: _Bookmarks()
    with pytest.raises(ToolExecutionError, match="Bookmark '_mcp_stale' not found"):
        prox.navigate_heading(doc, "bookmark:_mcp_stale", "next")


def test_navigate_stale_bookmark_does_not_use_paragraph_zero():
    doc, prox = _nav_stack(
        [
            ElementStub("Alpha", outline_level=1),
            ElementStub("Beta", outline_level=1),
        ]
    )

    class _Anchor:
        def getStart(self):
            raise RuntimeError("stale anchor")

        def getString(self):
            return ""

    class _Mark:
        def getAnchor(self):
            return _Anchor()

    class _Bookmarks:
        def hasByName(self, _name):
            return True

        def getByName(self, _name):
            return _Mark()

        def getElementNames(self):
            return ["_mcp_stale"]

    doc.getBookmarks = lambda: _Bookmarks()
    with pytest.raises(ToolExecutionError, match="Bookmark '_mcp_stale' anchor is not in the document"):
        prox.navigate_heading(doc, "bookmark:_mcp_stale", "next")
