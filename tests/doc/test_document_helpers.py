"""resolve_locator must not turn an unresolved writer locator into paragraph 0."""

from unittest.mock import MagicMock, patch

import pytest

import plugin.app as app
from plugin.doc.document_helpers import (
    DocumentService,
    _inject_selection_markers,
    _writer_selection_overlaps_windows,
    get_document_context_for_chat,
    resolve_locator,
)
from plugin.doc.text_helpers import read_writer_text_slice
from plugin.framework.errors import DocumentDisposedError, ToolExecutionError
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

    def getText(self):
        text = super().getText()
        # In live Writer, XText inherits from XTextRange and provides getStart().
        text.getStart = lambda: self.elements[0] if self.elements else MagicMock()
        return text

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
    with pytest.raises(ToolExecutionError, match=r"Heading index 9 out of range \(1\.\.2\) in 'heading:9'"):
        resolve_locator(doc, "heading:9")
    with pytest.raises(ToolExecutionError, match=r"Heading index 9 out of range \(1\.\.0\) in 'heading:1.9'"):
        resolve_locator(doc, "heading:1.9")


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

    def fake_find(found_anchor, _ranges, _text=None, entries=None):
        seen["anchor"] = found_anchor
        return 2

    monkeypatch.setattr("plugin.doc.document_helpers._paragraph_index_for_anchor", fake_find)
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

    def fake_find(found_anchor, _ranges, _text=None, entries=None):
        seen["anchor"] = found_anchor
        return 4

    monkeypatch.setattr("plugin.doc.document_helpers._paragraph_index_for_anchor", fake_find)
    hit = resolve_locator(doc, "section:Notes")
    assert hit["para_index"] == 4
    assert hit["section_name"] == "Notes"
    assert seen["anchor"] is anchor

    sections.hasByName.side_effect = lambda _name: False
    with pytest.raises(ToolExecutionError, match="Section 'Gone' not found"):
        resolve_locator(doc, "section:Gone")


def test_page_locator_dispatches_and_bad_page_errors(monkeypatch):
    monkeypatch.setattr(
        "plugin.doc.document_helpers._paragraph_index_for_anchor",
        lambda *_args, **_kwargs: 6,
    )
    doc = _PageDoc()
    doc.vc.getPage.return_value = 4
    hit = resolve_locator(doc, "page:3")
    assert hit["para_index"] == 6
    doc.vc.jumpToPage.assert_called_once_with(4)
    assert doc.locked == 1
    assert doc.unlocked == 1

    with pytest.raises(ToolExecutionError, match="page:abc"):
        resolve_locator(doc, "page:abc")

    doc.vc.getPage.return_value = 2
    with pytest.raises(ToolExecutionError, match="document has 2 page"):
        resolve_locator(doc, "page:3")


def test_page_locator_when_clone_fails_skips_body_move(monkeypatch):
    monkeypatch.setattr(
        "plugin.doc.document_helpers._paragraph_index_for_anchor",
        lambda *_args, **_kwargs: 6,
    )
    doc = _PageDoc()
    doc.vc.getPage.return_value = 4
    # When clone fails (e.g. view cursor in an exotic context that cannot clone),
    # with_view_cursor_left_body_locked intentionally skips moving the view cursor
    # to body start and does not lock controllers, but still executes the page jump.
    doc.vc.getText.side_effect = RuntimeError("cannot clone")
    hit = resolve_locator(doc, "page:3")
    assert hit["para_index"] == 6
    doc.vc.jumpToPage.assert_called_once_with(4)
    assert doc.locked == 0
    assert doc.unlocked == 0


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
    prev_services = app._services
    app._services = services
    try:
        hit = resolve_locator(WriterDocStub([ElementStub("a")]), "section:Body")
        assert hit["para_index"] == 9
        assert calls["args"] == ("section", "Body")
        services.get.assert_called_with("writer_tree")
    finally:
        app._services = prev_services


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
    prev_services = app._services
    app._services = services
    try:
        bookmarks = _Bookmarks(["here"], {"here": object()})
        doc = _BookmarkDoc([ElementStub("Alpha", outline_level=1)], bookmarks)
        hit = resolve_locator(doc, "bookmark:here")
        assert hit["para_index"] == 4
        assert calls["args"] == ("bookmark", "here")
    finally:
        app._services = prev_services


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


@patch.object(DocumentService, 'find_paragraph_element')
def test_get_page_for_paragraph_uses_find_element(mock_find):
    """Verify get_page_for_paragraph looks up the enumeration element instead of gotoNextParagraph."""
    from plugin.doc.document_helpers import DocumentService

    mock_doc = MagicMock()
    mock_text = MagicMock()
    mock_doc.getText.return_value = mock_text

    mock_controller = MagicMock()
    mock_doc.getCurrentController.return_value = mock_controller

    mock_vc = MagicMock()
    mock_controller.getViewCursor.return_value = mock_vc
    mock_vc.getPage.return_value = 5

    mock_element = MagicMock()
    mock_anchor = MagicMock()
    mock_element.getAnchor.return_value = mock_anchor

    ds = DocumentService()
    # Mock find_paragraph_element to return our specific element
    mock_find.return_value = mock_element

    page = ds.get_page_for_paragraph(mock_doc, 42)

    assert page == 5
    mock_vc.gotoRange.assert_any_call(mock_anchor, False)


@patch.object(DocumentService, 'find_paragraph_element')
def test_get_page_for_paragraph_handles_zero_page(mock_find):
    """Verify get_page_for_paragraph temporarily unlocks if getPage() returns 0 (table stall)."""
    from plugin.doc.document_helpers import DocumentService

    mock_doc = MagicMock()
    mock_text = MagicMock()
    mock_doc.getText.return_value = mock_text

    mock_controller = MagicMock()
    mock_doc.getCurrentController.return_value = mock_controller

    mock_vc = MagicMock()
    mock_controller.getViewCursor.return_value = mock_vc

    # First getPage returns 0, second returns 6
    mock_vc.getPage.side_effect = [0, 6]

    mock_saved_cursor = MagicMock()
    mock_text.createTextCursorByRange.return_value = mock_saved_cursor

    mock_element = MagicMock()
    mock_anchor = MagicMock()
    mock_element.getAnchor.return_value = mock_anchor

    ds = DocumentService()
    mock_find.return_value = mock_element

    page = ds.get_page_for_paragraph(mock_doc, 10)

    assert page == 6
    assert mock_vc.getPage.call_count == 2
    # Lock -> Unlock (temp) -> Lock (temp) -> Unlock (restore)
    assert mock_doc.lockControllers.call_count == 2
    assert mock_doc.unlockControllers.call_count == 2


@patch.object(DocumentService, 'find_paragraph_element')
def test_get_page_for_paragraph_missing_element_raises(mock_find):
    """Verify get_page_for_paragraph raises ToolExecutionError when paragraph not found."""
    import pytest
    from plugin.doc.document_helpers import DocumentService
    from plugin.framework.errors import ToolExecutionError

    mock_doc = MagicMock()
    mock_find.return_value = None

    ds = DocumentService()
    with pytest.raises(ToolExecutionError, match="Paragraph index 999 not found"):
        ds.get_page_for_paragraph(mock_doc, 999)


@patch.object(DocumentService, 'find_paragraph_element')
def test_get_page_for_paragraph_handles_nested_xtext_cursor(mock_find):
    """Verify get_page_for_paragraph succeeds when body createTextCursorByRange would fail."""
    from plugin.doc.document_helpers import DocumentService

    mock_doc = MagicMock()
    mock_body = MagicMock()
    # Body XText raises RuntimeException on nested cell range (#1422)
    mock_body.createTextCursorByRange.side_effect = RuntimeError(
        "End of content node doesn't have the proper start node"
    )
    mock_doc.getText.return_value = mock_body

    mock_controller = MagicMock()
    mock_doc.getCurrentController.return_value = mock_controller

    mock_cell_text = MagicMock()
    mock_cell_cursor = MagicMock()
    mock_cell_text.createTextCursorByRange.return_value = mock_cell_cursor

    mock_vc = MagicMock()
    mock_vc.getText.return_value = mock_cell_text
    mock_vc.getPage.return_value = 247
    mock_controller.getViewCursor.return_value = mock_vc

    mock_element = MagicMock()
    mock_anchor = MagicMock()
    mock_element.getAnchor.return_value = mock_anchor
    mock_find.return_value = mock_element

    ds = DocumentService()
    page = ds.get_page_for_paragraph(mock_doc, 45)

    assert page == 247
    # Body text was never asked to clone the nested range
    mock_body.createTextCursorByRange.assert_not_called()
    # vc.getText() cloned the range
    mock_cell_text.createTextCursorByRange.assert_called_once_with(mock_vc)


@patch.object(DocumentService, 'find_paragraph_element')
def test_get_page_for_paragraph_persistent_zero_raises(mock_find):
    """Verify get_page_for_paragraph raises ToolExecutionError if getPage remains 0 after retry."""
    import pytest
    from plugin.doc.document_helpers import DocumentService
    from plugin.framework.errors import ToolExecutionError

    mock_doc = MagicMock()
    mock_controller = MagicMock()
    mock_doc.getCurrentController.return_value = mock_controller
    mock_vc = MagicMock()
    mock_vc.getPage.return_value = 0
    mock_controller.getViewCursor.return_value = mock_vc

    mock_element = MagicMock()
    mock_element.getAnchor.return_value = MagicMock()
    mock_find.return_value = mock_element

    ds = DocumentService()
    with pytest.raises(ToolExecutionError, match="getPage returned 0"):
        ds.get_page_for_paragraph(mock_doc, 5)


def test_get_page_count_handles_nested_xtext_cursor():
    """Verify get_page_count succeeds even if body createTextCursorByRange would raise."""
    from plugin.doc.document_helpers import DocumentService

    mock_doc = MagicMock()
    mock_body = MagicMock()
    mock_body.createTextCursorByRange.side_effect = RuntimeError(
        "End of content node doesn't have the proper start node"
    )
    mock_doc.getText.return_value = mock_body

    mock_controller = MagicMock()
    mock_doc.getCurrentController.return_value = mock_controller

    mock_cell_text = MagicMock()
    mock_vc = MagicMock()
    mock_vc.getText.return_value = mock_cell_text
    mock_vc.getPage.return_value = 274
    mock_controller.getViewCursor.return_value = mock_vc

    ds = DocumentService()
    count = ds.get_page_count(mock_doc)

    assert count == 274
    mock_body.createTextCursorByRange.assert_not_called()


def test_get_page_count_without_view_cursor_uses_statistics():
    """Verify get_page_count falls back to DocumentStatistics when controller lacks getViewCursor."""
    from types import SimpleNamespace
    from plugin.doc.document_helpers import DocumentService

    mock_doc = MagicMock()
    mock_doc.getCurrentController.return_value = SimpleNamespace()  # no getViewCursor
    mock_doc.isModified.return_value = False
    mock_doc.WordCount = 42
    mock_doc.getDocumentProperties.return_value = SimpleNamespace(
        DocumentStatistics=[
            SimpleNamespace(Name="WordCount", Value=42),
            SimpleNamespace(Name="PageCount", Value=3),
        ]
    )

    assert DocumentService().get_page_count(mock_doc) == 3
    assert [c.args for c in mock_doc.setModified.call_args_list] == [(True,), (False,)]

def test_get_document_context_for_chat_writer_disposed():
    model = MagicMock()
    model.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"

    with patch("plugin.doc.text_helpers.writer_char_count", side_effect=DocumentDisposedError("stale")):
        with pytest.raises(DocumentDisposedError):
            get_document_context_for_chat(model, 1000, ctx=MagicMock())


def test_excerpt_overlap_other_xtext_is_false():
    from plugin.doc.document_helpers import _writer_excerpt_overlaps_selection

    class _Text(MagicMock):
        pass

    class _Pos:
        def __init__(self, text):
            self._text = text

        def getText(self):
            return self._text

    body = _Text()
    model = MagicMock()
    model.getText.return_value = body
    with patch("plugin.doc.text_helpers.get_text_cursor_at_range", return_value=MagicMock()):
        assert _writer_excerpt_overlaps_selection(
            model, 0, 10, _Pos(_Text()), _Pos(_Text())) is False
    model.getText.return_value.compareRegionStarts.assert_not_called()


@patch("plugin.doc.text_helpers.get_string_without_tracked_deletions", return_value="visible text")
@patch("plugin.doc.text_helpers.get_text_cursor_at_range")
def test_read_writer_text_slice_uses_deletion_filter(mock_get_cursor, mock_without_deletions):
    cursor = MagicMock()
    mock_get_cursor.return_value = cursor

    result = read_writer_text_slice(MagicMock(), 0, 100)

    assert result == "visible text"
    cursor.getString.assert_not_called()


@pytest.mark.parametrize(
    "value, value_2, expected",
    [
        pytest.param(2, 5, "[START]\nab[SELECTION_START]cde[SELECTION_END]fghij\n[END]", id="test_inject_markers_into_excerpt_selection_inside"),
        pytest.param(20, 25, "[START]\nabcdefghij\n[END]", id="test_inject_markers_no_overlap"),
    ],
)
def test_inject_markers_into_excerpt_selection_inside(value, value_2, expected):
    excerpt = "abcdefghij"
    with patch(
        "plugin.doc.text_helpers.read_writer_text_slice",
        side_effect=lambda _model, start, length: excerpt[start:start + length],
    ):
        out = _inject_selection_markers(MagicMock(), excerpt, 0, 10, value, value_2, "[START]\n", "\n[END]")
    assert out == expected

@patch("plugin.doc.text_helpers.read_writer_text_slice")
@patch("plugin.doc.text_helpers.writer_char_count", return_value=20000)
@patch("plugin.framework.doc_type.get_document_type")
def test_get_document_context_reads_slices_not_full_doc(mock_doc_type, mock_char_count, mock_read_slice):
    from plugin.framework.doc_type import DocumentType

    mock_doc_type.return_value = DocumentType.WRITER
    mock_read_slice.side_effect = lambda _model, start, length: f"slice:{start}:{length}"

    get_document_context_for_chat(MagicMock(), max_context=8000, include_end=True, include_selection=False)

    assert mock_read_slice.call_count == 2
    assert mock_read_slice.call_args_list[0][0][1:] == (0, 4000)
    assert mock_read_slice.call_args_list[1][0][1:] == (16000, 4000)


@patch("plugin.doc.text_helpers.read_writer_text_slice")
@patch("plugin.doc.text_helpers.writer_char_count", return_value=100)
@patch("plugin.framework.doc_type.get_document_type")
def test_get_document_context_short_doc_single_slice(mock_doc_type, mock_char_count, mock_read_slice):
    from plugin.framework.doc_type import DocumentType

    mock_doc_type.return_value = DocumentType.WRITER
    mock_read_slice.return_value = "short doc text"
    model = MagicMock()

    ctx = get_document_context_for_chat(model, max_context=8000, include_end=True, include_selection=False)

    assert "short doc text" in ctx
    mock_read_slice.assert_called_once_with(model, 0, 100)


@patch("plugin.doc.text_helpers.read_writer_text_slice")
@patch("plugin.doc.text_helpers.writer_char_count", return_value=8)
@patch("plugin.framework.doc_type.get_document_type")
def test_get_document_context_fitting_doc_is_one_slice(mock_doc_type, mock_char_count, mock_read_slice):
    """A document longer than half the budget but still within it is one slice.

    The old half-budget split overlapped the head and tail and repeated the middle.
    """
    from plugin.framework.doc_type import DocumentType

    mock_doc_type.return_value = DocumentType.WRITER
    body = "abcdefgh"
    mock_read_slice.side_effect = lambda _model, start, length: body[start : start + length]
    model = MagicMock()

    ctx = get_document_context_for_chat(model, max_context=10, include_end=True, include_selection=False)

    mock_read_slice.assert_called_once_with(model, 0, 8)
    assert body in ctx
    assert ctx.count("def") == 1
    assert "middle of document omitted" not in ctx


@patch("plugin.doc.text_helpers.read_writer_text_slice")
@patch("plugin.doc.text_helpers.writer_char_count", return_value=10)
@patch("plugin.framework.doc_type.get_document_type")
def test_get_document_context_exact_budget_is_one_slice(mock_doc_type, mock_char_count, mock_read_slice):
    from plugin.framework.doc_type import DocumentType

    mock_doc_type.return_value = DocumentType.WRITER
    mock_read_slice.return_value = "0123456789"
    model = MagicMock()

    ctx = get_document_context_for_chat(model, max_context=10, include_end=True, include_selection=False)

    mock_read_slice.assert_called_once_with(model, 0, 10)
    assert "middle of document omitted" not in ctx
    assert "0123456789" in ctx


@patch("plugin.doc.text_helpers.read_writer_text_slice")
@patch("plugin.doc.text_helpers.writer_char_count", return_value=16)
@patch("plugin.framework.doc_type.get_document_type")
def test_get_document_context_splits_only_past_budget(mock_doc_type, mock_char_count, mock_read_slice):
    from plugin.framework.doc_type import DocumentType

    mock_doc_type.return_value = DocumentType.WRITER
    body = "0123456789abcdef"
    mock_read_slice.side_effect = lambda _model, start, length: body[start : start + length]
    model = MagicMock()

    ctx = get_document_context_for_chat(model, max_context=10, include_end=True, include_selection=False)

    assert mock_read_slice.call_count == 2
    assert mock_read_slice.call_args_list[0][0][1:] == (0, 5)
    assert mock_read_slice.call_args_list[1][0][1:] == (11, 5)
    assert "01234" in ctx
    assert "bcdef" in ctx
    assert "56789a" not in ctx
    assert "middle of document omitted" in ctx


def test_writer_selection_overlaps_windows_true_when_ranges_overlap():
    model = MagicMock()
    text = MagicMock()
    model.getText.return_value = text
    exc_cursor = MagicMock()
    exc_cursor.getStart.return_value = "exc_start"
    exc_cursor.getEnd.return_value = "exc_end"
    text.compareRegionStarts.return_value = -1

    with patch("plugin.doc.text_helpers.get_text_cursor_at_range", return_value=exc_cursor):
        assert _writer_selection_overlaps_windows(model, [(0, 4000)], "sel_start", "sel_end") is True


def test_writer_selection_overlaps_windows_false_when_before_excerpt():
    model = MagicMock()
    text = MagicMock()
    model.getText.return_value = text
    exc_cursor = MagicMock()
    exc_cursor.getStart.return_value = "exc_start"
    exc_cursor.getEnd.return_value = "exc_end"
    text.compareRegionStarts.side_effect = lambda a, b: 1 if a == "sel_end" and b == "exc_start" else 0

    with patch("plugin.doc.text_helpers.get_text_cursor_at_range", return_value=exc_cursor):
        assert _writer_selection_overlaps_windows(model, [(100, 200)], "sel_start", "sel_end") is False


@patch("plugin.draw.bridge.get_draw_context_for_chat", return_value="impress summary")
@patch("plugin.framework.doc_type.get_document_type")
def test_get_document_context_draw_uses_bridge(mock_doc_type, mock_draw):
    from plugin.framework.doc_type import DocumentType

    mock_doc_type.return_value = DocumentType.IMPRESS
    model = MagicMock()
    ctx = MagicMock()
    assert get_document_context_for_chat(model, max_context=8000, ctx=ctx) == "impress summary"


@patch("plugin.doc.text_helpers.get_document_length", return_value=2439)
@patch("plugin.doc.text_helpers.read_writer_text_slice", return_value="short doc text")
@patch("plugin.doc.text_helpers.writer_char_count", return_value=3405)
@patch("plugin.framework.doc_type.get_document_type")
def test_get_document_context_shows_the_visible_length(mock_doc_type, mock_char_count, mock_read_slice, mock_len):
    """The excerpt windows use cursor steps, but the header shows the visible length that
    get_content reports as document_length (they disagreed in review mode)."""
    from plugin.framework.doc_type import DocumentType

    mock_doc_type.return_value = DocumentType.WRITER
    model = MagicMock()
    ctx = get_document_context_for_chat(model, max_context=8000, include_end=True, include_selection=False)
    assert "Document length: 2439 characters." in ctx
    mock_read_slice.assert_called_once_with(model, 0, 3405)


def test_inject_selection_markers_uses_visible_offsets():
    from plugin.doc.document_helpers import _inject_selection_markers

    visible = "Hello World"

    def cursor_to_visible(offset: int) -> int:
        return min(offset, 6) if offset <= 6 else offset - 3

    def read_slice(_model, start: int, length: int) -> str:
        return visible[cursor_to_visible(start) : cursor_to_visible(start + length)]

    with patch("plugin.doc.text_helpers.read_writer_text_slice", side_effect=read_slice):
        out = _inject_selection_markers(MagicMock(), visible, 0, 14, 9, 14, "[START]\n", "\n[END]")

    assert out == "[START]\nHello [SELECTION_START]World[SELECTION_END]\n[END]"


def test_get_document_context_for_chat_selection_markers_use_visible_offsets():
    from plugin.framework.doc_type import DocumentType

    visible = "Hello World"

    def cursor_to_visible(offset: int) -> int:
        return min(offset, 6) if offset <= 6 else offset - 3

    def read_slice(_model, start: int, length: int) -> str:
        return visible[cursor_to_visible(start) : cursor_to_visible(start + length)]

    with (
        patch("plugin.framework.doc_type.get_document_type", return_value=DocumentType.WRITER),
        patch("plugin.doc.text_helpers.writer_char_count", return_value=14),
        patch("plugin.doc.text_helpers.get_document_length", return_value=11),
        patch("plugin.doc.text_helpers.read_writer_text_slice", side_effect=read_slice),
        patch("plugin.doc.text_helpers.get_writer_selection_positions", return_value=(MagicMock(), "start", "end")),
        patch("plugin.doc.text_helpers.get_selection_range", return_value=(9, 14)),
        patch("plugin.doc.document_helpers._writer_selection_overlaps_windows", return_value=True),
        patch("plugin.doc.document_helpers._writer_has_math_ole", return_value=False),
    ):
        out = get_document_context_for_chat(MagicMock(), max_context=8000, include_selection=True)

    assert "Hello [SELECTION_START]World[SELECTION_END]" in out


def test_document_context_end_document_markers():
    from plugin.framework.client.shim_helpers import snapshot_text
    from plugin.framework.doc_type import DocumentType

    model = MagicMock()
    with (
        patch("plugin.framework.doc_type.get_document_type", return_value=DocumentType.WRITER),
        patch("plugin.doc.text_helpers.writer_char_count", return_value=100),
        patch("plugin.doc.text_helpers.read_writer_text_slice", return_value="short body"),
    ):
        ctx_fitting = get_document_context_for_chat(model, max_context=8000, include_end=True, include_selection=False)

    assert ctx_fitting.count("[END DOCUMENT]") == 0
    assert snapshot_text(ctx_fitting).count("[END DOCUMENT]") == 1

    with (
        patch("plugin.framework.doc_type.get_document_type", return_value=DocumentType.WRITER),
        patch("plugin.doc.text_helpers.writer_char_count", return_value=20000),
        patch("plugin.doc.text_helpers.read_writer_text_slice", side_effect=lambda _m, start, length: f"slice_{start}"),
    ):
        ctx_head_tail = get_document_context_for_chat(model, max_context=8000, include_end=True, include_selection=False)

    assert ctx_head_tail.count("[END DOCUMENT]") == 0
    assert ctx_head_tail.count("[DOCUMENT END]") == 1
    assert snapshot_text(ctx_head_tail).count("[END DOCUMENT]") == 1


def test_document_context_uses_given_selection():
    from plugin.framework.doc_type import DocumentType

    model = MagicMock()
    sentinel = MagicMock()
    with (
        patch("plugin.framework.doc_type.get_document_type", return_value=DocumentType.WRITER),
        patch("plugin.doc.text_helpers.writer_char_count", return_value=20),
        patch("plugin.doc.text_helpers.get_document_length", return_value=20),
        patch("plugin.doc.text_helpers.read_writer_text_slice", return_value="hello world"),
        patch("plugin.doc.text_helpers.get_writer_selection_positions", return_value=(MagicMock(), MagicMock(), MagicMock())) as mock_pos,
        patch("plugin.doc.document_helpers._writer_selection_overlaps_windows", return_value=True),
        patch("plugin.doc.text_helpers.get_selection_range", return_value=(0, 5)) as mock_range,
        patch("plugin.doc.document_helpers._inject_selection_markers", return_value="marked"),
    ):
        get_document_context_for_chat(model, max_context=8000, include_end=False, include_selection=True, selection=sentinel)

    assert mock_pos.call_args.kwargs["selection"] is sentinel
    assert mock_range.call_args.kwargs["selection"] is sentinel


def _fake_writer_shape(
    shape_type: str,
    *,
    name: str,
    services: set[str] | None = None,
    clsid: str | None = None,
    anchor_type: str = "AS_CHARACTER",
    anchor_page: int = 0,
    anchor_paragraph: str = "",
):
    shape = MagicMock()
    shape.getShapeType.return_value = shape_type
    shape.Name = name
    shape.supportsService.side_effect = lambda svc: svc in (services or set())
    if clsid is not None:
        shape.CLSID = clsid
    else:
        del shape.CLSID
    shape.getPosition.side_effect = AttributeError("no pos")
    shape.getSize.side_effect = AttributeError("no size")
    del shape.getString

    anchor_enum = MagicMock()
    anchor_enum.value = anchor_type

    def get_prop(prop: str):
        if prop == "AnchorType":
            return anchor_enum
        if prop == "AnchorPageNo":
            return anchor_page
        raise KeyError(prop)

    shape.getPropertyValue.side_effect = get_prop
    if anchor_paragraph:
        cur = MagicMock()
        cur.getString.return_value = anchor_paragraph
        anchor = MagicMock()
        anchor.getText.return_value.createTextCursorByRange.return_value = cur
        shape.getAnchor.return_value = anchor
    else:
        shape.getAnchor.side_effect = AttributeError("no anchor")
    return shape


def _fake_draw_page(shapes):
    page = MagicMock()
    page.getCount.return_value = len(shapes)
    page.getByIndex.side_effect = lambda i: shapes[i]
    return page


def _writer_ctx_with_page(page=None, *, draw_page_error=None):
    from plugin.framework.doc_type import DocumentType

    model = MagicMock()
    if draw_page_error is not None:
        model.getDrawPage.side_effect = draw_page_error
    else:
        model.getDrawPage.return_value = page

    with (
        patch("plugin.framework.doc_type.get_document_type", return_value=DocumentType.WRITER),
        patch("plugin.doc.text_helpers.writer_char_count", return_value=20),
        patch("plugin.doc.text_helpers.get_document_length", return_value=20),
        patch("plugin.doc.text_helpers.read_writer_text_slice", return_value="Intro paragraph here."),
        patch("plugin.doc.document_helpers._writer_has_math_ole", return_value=False),
    ):
        return get_document_context_for_chat(model, max_context=8000, include_end=True, include_selection=False)


def test_writer_context_lists_embedded_object_names():
    chart = _fake_writer_shape(
        "FrameShape",
        name="Chart_0",
        services={"com.sun.star.text.TextEmbeddedObject"},
        clsid="12DCAE26-281F-416F-a234-c3086127382e",
        anchor_paragraph="Intro paragraph here.",
    )
    image = _fake_writer_shape(
        "FrameShape",
        name="Image1",
        services={"com.sun.star.text.TextGraphicObject"},
    )
    out = _writer_ctx_with_page(_fake_draw_page([chart, image]))
    lines = out.splitlines()
    assert any("Chart_0" in line and "Chart" in line for line in lines)
    assert any("Image1" in line and "Image" in line for line in lines)


def test_writer_context_without_objects_matches_text_only():
    empty_out = _writer_ctx_with_page(_fake_draw_page([]))
    with patch("plugin.doc.document_helpers._writer_objects_section", return_value=""):
        text_only_out = _writer_ctx_with_page(_fake_draw_page([]))
    assert empty_out == text_only_out


def test_writer_context_caps_object_list():
    shapes = [
        _fake_writer_shape(
            "FrameShape",
            name=f"Image{i}",
            services={"com.sun.star.text.TextGraphicObject"},
        )
        for i in range(25)
    ]
    out = _writer_ctx_with_page(_fake_draw_page(shapes))
    obj_lines = [line for line in out.splitlines() if line.startswith("- [")]
    assert len(obj_lines) == 20
    assert "[19]" in out
    assert "[20]" not in out


def test_writer_context_object_list_failure_keeps_text():
    out = _writer_ctx_with_page(draw_page_error=RuntimeError("draw page broken"))
    assert "Intro paragraph here." in out


def test_writer_anchor_note_reports_at_page_number():
    from plugin.doc.document_helpers import _writer_anchor_note

    shape = _fake_writer_shape(
        "com.sun.star.drawing.RectangleShape",
        name="Shape1",
        anchor_type="AT_PAGE",
        anchor_page=3,
    )
    assert "page 2" in _writer_anchor_note(shape)


def test_watch_open_doc_types_reports_now_and_after_document_events(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import plugin.doc.document_helpers as document_helpers

    ctx = MagicMock()
    broadcaster = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.return_value = broadcaster
    monkeypatch.setattr(document_helpers, "get_ctx", lambda: ctx)
    monkeypatch.setattr(document_helpers, "_OPEN_DOC_TYPES_LISTENER", None)

    open_types: dict[str, object] = {"calc": object()}
    monkeypatch.setattr(document_helpers.DocumentService, "open_documents_by_type", lambda _self: dict(open_types))

    seen: list[frozenset[str]] = []
    svc = document_helpers.DocumentService()
    svc.watch_open_doc_types(seen.append)
    assert seen == [frozenset({"calc"})]

    broadcaster.addDocumentEventListener.assert_called_once()
    listener = broadcaster.addDocumentEventListener.call_args.args[0]

    open_types["writer"] = object()
    listener.documentEventOccured(SimpleNamespace(EventName="OnLoad", ViewController=object(), Source=None))
    assert seen[-1] == frozenset({"calc", "writer"})

    del open_types["writer"]
    listener.documentEventOccured(SimpleNamespace(EventName="OnUnload", ViewController=None, Source=None))
    assert seen[-1] == frozenset({"calc"})

    svc.watch_open_doc_types(seen.append)
    broadcaster.addDocumentEventListener.assert_called_once()


def test_open_documents_skips_registered_scratch_and_keeps_user_hidden(monkeypatch):
    from unittest.mock import MagicMock

    import plugin.doc.document_helpers as document_helpers
    import plugin.framework.scratch_writer as scratch_writer

    monkeypatch.setattr(scratch_writer, "_scratch_uids", set())
    monkeypatch.setattr(scratch_writer, "_scratch_loads", 0)
    monkeypatch.setattr(scratch_writer, "_after_loads", [])

    def _office_doc(uid: str):
        d = MagicMock()
        d.supportsService.side_effect = lambda svc: svc == "com.sun.star.document.OfficeDocument"
        d.getRuntimeUID.return_value = uid
        return d

    user_hidden = _office_doc("10")
    scratch_doc = _office_doc("11")
    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = scratch_doc
    scratch_writer.load_scratch_document(desktop, "private:factory/sdraw")

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    desktop.getComponents.return_value.createEnumeration.return_value = _Enum([user_hidden, scratch_doc])
    monkeypatch.setattr(document_helpers, "get_desktop", lambda: desktop)

    docs = document_helpers.DocumentService().open_documents()
    assert docs == [user_hidden]


def test_watch_open_doc_types_ignores_scratch_load_and_reports_user_load(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import plugin.doc.document_helpers as document_helpers
    import plugin.framework.scratch_writer as scratch_writer

    monkeypatch.setattr(scratch_writer, "_scratch_uids", set())
    monkeypatch.setattr(scratch_writer, "_scratch_loads", 0)
    monkeypatch.setattr(scratch_writer, "_after_loads", [])

    ctx = MagicMock()
    broadcaster = MagicMock()
    ctx.getServiceManager.return_value.createInstanceWithContext.return_value = broadcaster
    monkeypatch.setattr(document_helpers, "get_ctx", lambda: ctx)
    monkeypatch.setattr(document_helpers, "_OPEN_DOC_TYPES_LISTENER", None)

    def _doc(uid: str, service: str):
        d = MagicMock()
        d.getRuntimeUID.return_value = uid
        d.supportsService.side_effect = lambda s: s in ("com.sun.star.document.OfficeDocument", service)
        return d

    calc_doc = _doc("1", "com.sun.star.sheet.SpreadsheetDocument")
    scratch_draw = _doc("2", "com.sun.star.drawing.DrawingDocument")
    user_writer = _doc("3", "com.sun.star.text.TextDocument")
    desktop_docs = [calc_doc]

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    desktop = MagicMock()
    desktop.getComponents.side_effect = lambda: SimpleNamespace(createEnumeration=lambda: _Enum(desktop_docs))
    monkeypatch.setattr(document_helpers, "get_desktop", lambda: desktop)

    seen: list[frozenset[str]] = []
    svc = document_helpers.DocumentService()
    svc.watch_open_doc_types(seen.append)
    assert seen == [frozenset({"calc"})]

    listener = broadcaster.addDocumentEventListener.call_args.args[0]

    def _scratch_load(*_args):
        desktop_docs.append(scratch_draw)
        listener.documentEventOccured(SimpleNamespace(EventName="OnViewCreated", ViewController=object(), Source=scratch_draw))
        listener.documentEventOccured(SimpleNamespace(EventName="OnViewCreated", ViewController=object(), Source=scratch_draw))
        return scratch_draw

    desktop.loadComponentFromURL.side_effect = _scratch_load
    scratch_writer.load_scratch_document(desktop, "private:factory/sdraw")
    assert seen == [frozenset({"calc"}), frozenset({"calc"})]

    desktop_docs.append(user_writer)
    listener.documentEventOccured(SimpleNamespace(EventName="OnViewCreated", ViewController=object(), Source=user_writer))
    assert seen[-1] == frozenset({"calc", "writer"})


def test_print_preview_controller_without_view_cursor(monkeypatch):
    from types import SimpleNamespace

    from plugin.doc.document_helpers import DocumentService
    from plugin.framework.errors import UnoObjectError

    model = SimpleNamespace(getCurrentController=lambda: SimpleNamespace())
    ds = DocumentService()
    monkeypatch.setattr(ds, "find_paragraph_element", lambda _model, _idx: object())

    assert ds.get_page_count(model) == 0
    with pytest.raises(UnoObjectError, match="Get view cursor failed"):
        ds.get_page_for_paragraph(model, 0)
