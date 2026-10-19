"""clone_heading_block must not clone paragraph 0 when the locator misses."""

from unittest.mock import MagicMock, patch

import pytest

from plugin.doc.document_helpers import DocumentService
from plugin.framework.errors import ToolExecutionError
from plugin.tests.testing_utils import ElementStub, WriterDocStub
from plugin.writer.structural import CloneHeadingBlock, _resolve_para_index


def _doc():
    return WriterDocStub(
        [
            ElementStub("Preamble"),
            ElementStub("Alpha", outline_level=1),
            ElementStub("body"),
        ]
    )


def _ctx(doc):
    ctx = MagicMock()
    ctx.doc = doc
    ctx.services.document = DocumentService()
    return ctx


def test_resolve_para_index_heading_text_is_not_paragraph_zero():
    doc = _doc()
    assert _resolve_para_index(_ctx(doc), {"locator": "heading_text:Alpha"}) == 1


def test_resolve_para_index_missing_heading_text_raises():
    with pytest.raises(ToolExecutionError, match="No heading matching 'Missing'"):
        _resolve_para_index(_ctx(_doc()), {"locator": "heading_text:Missing"})


def test_clone_heading_block_missing_locator_does_not_insert():
    doc = _doc()
    inserted = []
    original_get_text = doc.getText

    def tracking_get_text():
        text = original_get_text()
        text.insertString = lambda *_args, **_kwargs: inserted.append(True)
        text.insertControlCharacter = lambda *_args, **_kwargs: inserted.append(True)
        return text

    doc.getText = tracking_get_text
    result = CloneHeadingBlock().execute(_ctx(doc), locator="heading_text:Missing")
    assert result["status"] == "error"
    assert "No heading matching 'Missing'" in result["message"]
    assert inserted == []


def test_clone_heading_block_stale_bookmark_does_not_insert():
    doc = _doc()

    class _Anchor:
        def getStart(self):
            raise RuntimeError("stale anchor")

        def getString(self):
            return ""

    class _Marks:
        def hasByName(self, name):
            return name == "_mcp_stale"

        def getByName(self, _name):
            bookmark = MagicMock()
            bookmark.getAnchor.return_value = _Anchor()
            return bookmark

        def getElementNames(self):
            return ["_mcp_stale"]

    doc.getBookmarks = lambda: _Marks()
    inserted = []
    original_get_text = doc.getText

    def tracking_get_text():
        text = original_get_text()
        text.insertString = lambda *_args, **_kwargs: inserted.append(True)
        text.insertControlCharacter = lambda *_args, **_kwargs: inserted.append(True)
        return text

    doc.getText = tracking_get_text
    result = CloneHeadingBlock().execute(_ctx(doc), locator="bookmark:_mcp_stale")
    assert result["status"] == "error"
    assert "anchor is not in the document" in result["message"]
    assert inserted == []


def test_clone_heading_block_with_table():
    from plugin.writer.tree import TreeService

    class TableStub:
        def __init__(self):
            self.anchor = MagicMock(name="table_anchor")
            self.para_style_called = False

        def supportsService(self, service):
            return service == "com.sun.star.text.TextTable"

        def getPropertyValue(self, name):
            if name == "ParaStyleName":
                self.para_style_called = True
                raise RuntimeError("TextTable has no ParaStyleName")
            if name == "OutlineLevel":
                return 0
            raise RuntimeError(f"Unknown property: {name}")

        def getAnchor(self):
            return self.anchor

        def getString(self):
            return ""

    class ParaStub(ElementStub):
        def __init__(self, text, outline_level=0, style="Standard"):
            super().__init__(text, outline_level=outline_level)
            self.style = style

        def getPropertyValue(self, name):
            if name == "ParaStyleName":
                return self.style
            return super().getPropertyValue(name)

    table = TableStub()
    elements = [
        ParaStub("Heading A", outline_level=1, style="Heading 1"),
        ParaStub("Body text", outline_level=0, style="Standard"),
        table,
        ParaStub("Heading B", outline_level=1, style="Heading 1"),
    ]
    doc = WriterDocStub(elements)
    inserted_strings = []
    created_cursors = []

    text_obj = doc.getText()
    cursor_mock = MagicMock()

    def mock_create_cursor(target_range):
        created_cursors.append(target_range)
        return cursor_mock

    text_obj.createTextCursorByRange = mock_create_cursor
    text_obj.insertControlCharacter = MagicMock()
    text_obj.insertString = lambda _c, txt, _b: inserted_strings.append(txt)
    doc.getText = lambda: text_obj

    ctx = MagicMock()
    ctx.doc = doc
    doc_svc = DocumentService()
    doc_svc.yield_to_gui = MagicMock()
    doc_svc.doc_key = MagicMock(return_value="test_doc")
    bm_svc = MagicMock()
    bm_svc.ensure_heading_bookmarks.return_value = {}
    events = MagicMock()

    services = MagicMock()
    services.document = doc_svc
    services.writer_bookmarks = bm_svc
    services.events = events
    tree_svc = TreeService(services)
    services.get.side_effect = lambda name: tree_svc if name == "writer_tree" else None
    ctx.services = services

    import sys
    import types

    cc_mod = types.ModuleType("com.sun.star.text.ControlCharacter")
    cc_mod.PARAGRAPH_BREAK = 0  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"com.sun.star.text.ControlCharacter": cc_mod}):
        res = CloneHeadingBlock().execute(ctx, locator="heading_text:Heading A")
    assert res["status"] == "ok"
    assert not table.para_style_called
    assert inserted_strings == ["Heading A", "Body text"]
    assert created_cursors == [table.getAnchor()]
    cursor_mock.collapseToEnd.assert_called_once()
    assert "Plain paragraph text" in CloneHeadingBlock.description
    assert "tables are skipped" in CloneHeadingBlock.description


def test_get_page_objects_disposal_and_error_handling(caplog):
    import logging
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.structural import GetPageObjects

    class DisposedException(Exception):
        pass

    from com.sun.star.text.TextContentAnchorType import AT_PAGE

    class GraphicStub:
        def __init__(self, name, exc=None):
            self.name = name
            self.exc = exc

        def getPropertyValue(self, name):
            if self.exc:
                raise self.exc
            if name == "AnchorType":
                return AT_PAGE
            if name == "AnchorPageNo":
                return 1
            if name == "Size":
                return MagicMock(Width=2000, Height=1000)
            if name == "Title":
                return "Title"
            raise RuntimeError(f"Unknown prop {name}")

        def getAnchor(self):
            return MagicMock()

    class Graphics:
        def __init__(self, items):
            self.items = items

        def getElementNames(self):
            return list(self.items.keys())

        def getByName(self, name):
            return self.items[name]

    # Test 1: first graphic raises DisposedException -> scan raises DocumentDisposedError
    vc = MagicMock()
    vc.getPage.return_value = 1
    doc_disp = MagicMock()
    doc_disp.getCurrentController.return_value.getViewCursor.return_value = vc
    doc_disp.getGraphicObjects.return_value = Graphics({"G1": GraphicStub("G1", DisposedException("disposed"))})
    doc_disp.getTextTables.return_value.getElementNames.return_value = []
    doc_disp.getTextFrames.return_value.getElementNames.return_value = []
    doc_disp.getDrawPage.return_value.getCount.return_value = 0
    ctx = MagicMock()
    ctx.doc = doc_disp

    with pytest.raises((DisposedException, DocumentDisposedError)):
        GetPageObjects().execute(ctx, page=1)

    # Test 2: G1 succeeds, G2 raises RuntimeError -> G2 skipped, G1 kept, warning logged
    g1 = GraphicStub("G1")

    class CorruptGraphic(GraphicStub):
        def getPropertyValue(self, name):
            if name == "AnchorType":
                return AT_PAGE
            if name == "AnchorPageNo":
                return 1
            if name == "Size":
                raise RuntimeError("corrupt graphic")
            return super().getPropertyValue(name)

    g2 = CorruptGraphic("G2")
    doc_live = MagicMock()
    doc_live.getCurrentController.return_value.getViewCursor.return_value = vc
    doc_live.getGraphicObjects.return_value = Graphics({"G1": g1, "G2": g2})
    doc_live.getTextTables.return_value.getElementNames.return_value = []
    doc_live.getTextFrames.return_value.getElementNames.return_value = []
    doc_live.getDrawPage.return_value.getCount.return_value = 0
    ctx.doc = doc_live

    with caplog.at_level(logging.WARNING):
        res = GetPageObjects().execute(ctx, page=1)
    assert res["status"] == "ok"
    assert len(res["images"]) == 1
    assert res["images"][0]["name"] == "G1"
    assert any("Failed to scan graphic object 'G2'" in rec.message for rec in caplog.records)

