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


class _PosPara:
    def __init__(self, text, start, end):
        self.text = text
        self.start = start
        self.end = end

    def getString(self):
        return self.text

    def supportsService(self, service):
        return service == "com.sun.star.text.Paragraph"

    def getStart(self):
        return self.start

    def getEnd(self):
        return self.end


class _PosText:
    def __init__(self, paras):
        self.paras = paras

    def createEnumeration(self):
        return _Enum(self.paras)

    def compareRegionStarts(self, left, right):
        if left < right:
            return 1
        if left > right:
            return -1
        return 0


class _Enum:
    def __init__(self, items):
        self.items = list(items)
        self.idx = 0

    def hasMoreElements(self):
        return self.idx < len(self.items)

    def nextElement(self):
        item = self.items[self.idx]
        self.idx += 1
        return item


class _PosAnchor:
    def __init__(self, pos):
        self.pos = pos

    def getStart(self):
        return self.pos

    def getEnd(self):
        return self.pos

    def getString(self):
        return ""


class _PosDoc:
    def __init__(self, paras, name, pos):
        self._paras = paras
        self._text = _PosText(paras)
        self._name = name
        self._anchor = _PosAnchor(pos)

    def getText(self):
        return self._text

    def getBookmarks(self):
        return self

    def hasByName(self, name):
        return name == self._name

    def getByName(self, name):
        bookmark = MagicMock()
        bookmark.getAnchor.return_value = self._anchor
        return bookmark

    def getElementNames(self):
        return [self._name]


def test_live_bookmark_uses_the_tree_resolver(monkeypatch):
    """A bookmark that still exists must not skip TreeService.

    The old short-circuit called find_paragraph_for_range itself and returned
    its fallback 0. This document's anchor would be that 0 if the short-circuit
    still ran.
    """
    calls = {}

    class _Tree:
        def resolve_writer_locator(self, _doc, loc_type, loc_value):
            calls["args"] = (loc_type, loc_value)
            return {"para_index": 4}

    services = MagicMock()
    services.get.return_value = _Tree()
    main = type(sys)("plugin.main")
    main._services = services
    monkeypatch.setitem(sys.modules, "plugin.main", main)

    bookmarks = _Bookmarks(["here"], {"here": object()})
    doc = _BookmarkDoc([ElementStub("Alpha", outline_level=1)], bookmarks)
    hit = resolve_locator(doc, "bookmark:here")
    assert hit["para_index"] == 4
    assert calls["args"] == ("bookmark", "here")


def test_bookmark_on_first_paragraph_still_resolves():
    paras = [_PosPara("Alpha", 0, 10), _PosPara("Beta", 10, 20)]
    assert resolve_locator(_PosDoc(paras, "here", 0), "bookmark:here")["para_index"] == 0
    assert resolve_locator(_PosDoc(paras, "here", 15), "bookmark:here")["para_index"] == 1


def test_bookmark_past_the_end_is_not_paragraph_zero():
    paras = [_PosPara("Alpha", 0, 10), _PosPara("Beta", 10, 20)]
    with pytest.raises(ToolExecutionError, match="Bookmark 'here' anchor is not in the document"):
        resolve_locator(_PosDoc(paras, "here", 500), "bookmark:here")


def test_stale_bookmark_anchor_is_not_paragraph_zero():
    class _Broken:
        def getStart(self):
            raise RuntimeError("stale anchor")

        def getString(self):
            return ""

    class _Marks:
        def hasByName(self, name):
            return name == "_mcp_stale"

        def getByName(self, _name):
            bookmark = MagicMock()
            bookmark.getAnchor.return_value = _Broken()
            return bookmark

        def getElementNames(self):
            return ["_mcp_stale"]

    doc = _BookmarkDoc(
        [ElementStub("Alpha", outline_level=1), ElementStub("Beta", outline_level=1)],
        _Marks(),
    )
    with pytest.raises(ToolExecutionError, match="Bookmark '_mcp_stale' anchor is not in the document"):
        resolve_locator(doc, "bookmark:_mcp_stale")
