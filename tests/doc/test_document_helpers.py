"""resolve_locator must not turn an unresolved writer locator into paragraph 0."""

import sys
from unittest.mock import MagicMock

import pytest

from plugin.doc.document_helpers import DocumentService, resolve_locator
from plugin.framework.errors import ToolExecutionError
from plugin.tests.testing_utils import ElementStub, WriterDocStub


def _outline_doc():
    return WriterDocStub(
        [
            ElementStub("Preamble"),
            ElementStub("Alpha", outline_level=1),
            ElementStub("body"),
            ElementStub("Beta", outline_level=1),
        ]
    )


class _Bookmarks:
    def __init__(self, names, anchors=None):
        self._names = list(names)
        self._anchors = anchors or {}

    def hasByName(self, name):
        return name in self._names

    def getByName(self, name):
        bookmark = MagicMock()
        bookmark.getAnchor.return_value = self._anchors[name]
        return bookmark

    def getElementNames(self):
        return list(self._names)


class _BookmarkDoc(WriterDocStub):
    def __init__(self, elements, bookmarks):
        super().__init__(elements)
        self._bookmarks = bookmarks

    def getBookmarks(self):
        return self._bookmarks


class _PageDoc(WriterDocStub):
    def __init__(self):
        super().__init__([ElementStub("a"), ElementStub("b"), ElementStub("c")])
        self.locked = 0
        self.unlocked = 0
        self.vc = MagicMock()

    def getCurrentController(self):
        controller = MagicMock()
        controller.getViewCursor.return_value = self.vc
        return controller

    def lockControllers(self):
        self.locked += 1

    def unlockControllers(self):
        self.unlocked += 1


def test_paragraph_and_heading_ordinals_still_resolve():
    doc = _outline_doc()
    assert resolve_locator(doc, "paragraph:3")["para_index"] == 3
    assert resolve_locator(doc, "heading:1")["para_index"] == 1
    assert resolve_locator(doc, "heading:2")["para_index"] == 3


def test_heading_text_resolves_past_paragraph_zero():
    doc = _outline_doc()
    hit = resolve_locator(doc, "heading_text:Beta")
    assert hit["para_index"] == 3
    assert DocumentService().resolve_locator(doc, "heading_text:Alpha")["para_index"] == 1


def test_missing_heading_text_is_an_error():
    doc = _outline_doc()
    with pytest.raises(ToolExecutionError, match="No heading matching 'Missing'"):
        resolve_locator(doc, "heading_text:Missing")


def test_missing_bookmark_uses_writer_resolver_hint():
    bookmarks = _Bookmarks(["_mcp_live"])
    doc = _BookmarkDoc([ElementStub("Alpha", outline_level=1)], bookmarks)
    with pytest.raises(ToolExecutionError, match="Bookmark '_mcp_gone' not found") as raised:
        resolve_locator(doc, "bookmark:_mcp_gone")
    message = str(raised.value)
    assert "heading_text:" in message
    assert "_mcp_live" in message


def test_live_bookmark_still_resolves(monkeypatch):
    anchor = object()
    bookmarks = _Bookmarks(["here"], {"here": anchor})
    doc = _BookmarkDoc([ElementStub("Alpha", outline_level=1)], bookmarks)
    seen = {}

    def fake_find(found_anchor, _ranges, _text=None):
        seen["anchor"] = found_anchor
        return 2

    monkeypatch.setattr("plugin.doc.document_helpers._find_paragraph_for_range", fake_find)
    assert resolve_locator(doc, "bookmark:here")["para_index"] == 2
    assert seen["anchor"] is anchor


def test_section_locator_dispatches_to_writer_resolver(monkeypatch):
    anchor = object()
    section = MagicMock()
    section.getAnchor.return_value = anchor
    sections = MagicMock()
    sections.hasByName.side_effect = lambda name: name == "Notes"
    sections.getByName.return_value = section
    doc = WriterDocStub([ElementStub("a"), ElementStub("b")])
    doc.getTextSections = lambda: sections
    seen = {}

    def fake_find(found_anchor, _ranges, _text=None):
        seen["anchor"] = found_anchor
        return 4

    monkeypatch.setattr("plugin.doc.document_helpers._find_paragraph_for_range", fake_find)
    hit = resolve_locator(doc, "section:Notes")
    assert hit["para_index"] == 4
    assert hit["section_name"] == "Notes"
    assert seen["anchor"] is anchor

    sections.hasByName.side_effect = lambda _name: False
    with pytest.raises(ToolExecutionError, match="Section 'Gone' not found"):
        resolve_locator(doc, "section:Gone")


def test_page_locator_dispatches_and_bad_page_errors(monkeypatch):
    monkeypatch.setattr(
        "plugin.doc.document_helpers._find_paragraph_for_range",
        lambda *_args, **_kwargs: 6,
    )
    doc = _PageDoc()
    hit = resolve_locator(doc, "page:3")
    assert hit["para_index"] == 6
    doc.vc.jumpToPage.assert_called_once_with(3)
    assert doc.locked == 1
    assert doc.unlocked == 1

    with pytest.raises(ToolExecutionError, match="page:abc"):
        resolve_locator(doc, "page:abc")


def test_unstructured_and_unknown_locators_error():
    doc = _outline_doc()
    with pytest.raises(ToolExecutionError, match="Cannot resolve locator 'Beta'"):
        resolve_locator(doc, "Beta")
    with pytest.raises(ToolExecutionError, match="Unknown Writer locator type: 'nope'"):
        resolve_locator(doc, "nope:1")
    with pytest.raises(ToolExecutionError, match="heading:nope"):
        resolve_locator(doc, "heading:nope")
    with pytest.raises(ToolExecutionError, match="paragraph:abc"):
        resolve_locator(doc, "paragraph:abc")


def test_registered_writer_tree_is_the_resolver(monkeypatch):
    calls = {}

    class _Tree:
        def resolve_writer_locator(self, _doc, loc_type, loc_value):
            calls["args"] = (loc_type, loc_value)
            return {"para_index": 9, "section_name": loc_value}

    services = MagicMock()
    services.get.return_value = _Tree()
    main = type(sys)("plugin.main")
    main._services = services
    monkeypatch.setitem(sys.modules, "plugin.main", main)

    hit = resolve_locator(WriterDocStub([ElementStub("a")]), "section:Body")
    assert hit["para_index"] == 9
    assert calls["args"] == ("section", "Body")
    services.get.assert_called_with("writer_tree")
