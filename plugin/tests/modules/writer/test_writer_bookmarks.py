import pytest
import sys
import types
from unittest.mock import MagicMock, call

# Ensure UNO is mocked if running outside LibreOffice
try:
    import uno
except ImportError:
    sys.modules["uno"] = MagicMock()
    sys.modules["unohelper"] = MagicMock()
    com_mock = MagicMock()
    sys.modules["com"] = com_mock
    # Create module type for com.sun.star.text
    css_text = types.ModuleType("com.sun.star.text")
    sys.modules["com.sun.star.text"] = css_text
    # We don't strictly need class stubs here if we just mock the module objects,
    # but let's make sure it doesn't fail on imports.

from plugin.modules.writer.bookmark_tools import (
    CreateBookmark,
    DeleteBookmark,
    RenameBookmark,
    GetBookmark,
    ListBookmarks,
)


@pytest.fixture
def mock_ctx():
    class DummyContext:
        def __init__(self):
            self.doc = MagicMock()
            self.doc_type = "writer"
            self.services = MagicMock()
            self.ctx = MagicMock()

    return DummyContext()


def test_create_bookmark(mock_ctx):
    doc = mock_ctx.doc
    mock_bookmarks = MagicMock()
    doc.getBookmarks.return_value = mock_bookmarks
    mock_bookmarks.hasByName.return_value = False

    mock_ctrl = MagicMock()
    doc.getCurrentController.return_value = mock_ctrl
    mock_cursor = MagicMock()
    mock_ctrl.getViewCursor.return_value = mock_cursor
    mock_text = MagicMock()
    mock_cursor.getText.return_value = mock_text

    mock_bookmark_inst = MagicMock()
    doc.createInstance.return_value = mock_bookmark_inst

    tool = CreateBookmark()
    res = tool.execute(mock_ctx, name="MyNewBookmark")

    assert res["status"] == "ok"
    assert "created" in res["message"]
    doc.createInstance.assert_called_with("com.sun.star.text.Bookmark")
    assert mock_bookmark_inst.Name == "MyNewBookmark"
    mock_text.insertTextContent.assert_called_with(mock_cursor, mock_bookmark_inst, True)


def test_create_bookmark_already_exists(mock_ctx):
    doc = mock_ctx.doc
    mock_bookmarks = MagicMock()
    doc.getBookmarks.return_value = mock_bookmarks
    mock_bookmarks.hasByName.return_value = True

    tool = CreateBookmark()
    res = tool.execute(mock_ctx, name="ExistingBookmark")

    assert res["status"] == "error"
    assert "already exists" in res["message"]


def test_delete_bookmark(mock_ctx):
    doc = mock_ctx.doc
    mock_bookmarks = MagicMock()
    doc.getBookmarks.return_value = mock_bookmarks
    mock_bookmarks.hasByName.return_value = True

    mock_bm = MagicMock()
    mock_bookmarks.getByName.return_value = mock_bm
    mock_anchor = MagicMock()
    mock_bm.getAnchor.return_value = mock_anchor
    mock_text = MagicMock()
    mock_anchor.getText.return_value = mock_text

    tool = DeleteBookmark()
    res = tool.execute(mock_ctx, name="ToDelete")

    assert res["status"] == "ok"
    assert "deleted" in res["message"]
    mock_text.removeTextContent.assert_called_with(mock_bm)


def test_rename_bookmark(mock_ctx):
    doc = mock_ctx.doc
    mock_bookmarks = MagicMock()
    doc.getBookmarks.return_value = mock_bookmarks

    # old_name exists, new_name does not
    def has_by_name(name):
        return name == "OldName"
    mock_bookmarks.hasByName.side_effect = has_by_name

    mock_bm = MagicMock()
    mock_bookmarks.getByName.return_value = mock_bm

    tool = RenameBookmark()
    res = tool.execute(mock_ctx, old_name="OldName", new_name="NewName")

    assert res["status"] == "ok"
    assert "renamed" in res["message"]
    mock_bm.setName.assert_called_with("NewName")


def test_get_bookmark(mock_ctx):
    doc = mock_ctx.doc
    mock_bookmarks = MagicMock()
    doc.getBookmarks.return_value = mock_bookmarks
    mock_bookmarks.hasByName.return_value = True

    mock_bm = MagicMock()
    mock_bookmarks.getByName.return_value = mock_bm
    mock_anchor = MagicMock()
    mock_bm.getAnchor.return_value = mock_anchor
    mock_anchor.getString.return_value = "Spanned Text Here"

    tool = GetBookmark()
    res = tool.execute(mock_ctx, name="InfoBM")

    assert res["status"] == "ok"
    assert res["bookmark"]["name"] == "InfoBM"
    assert res["bookmark"]["text"] == "Spanned Text Here"


def test_list_bookmarks(mock_ctx):
    doc = mock_ctx.doc
    mock_bookmarks = MagicMock()
    doc.getBookmarks.return_value = mock_bookmarks
    mock_bookmarks.getElementNames.return_value = ("BM1", "BM2")

    mock_bm1 = MagicMock()
    mock_bm2 = MagicMock()
    mock_bookmarks.getByName.side_effect = [mock_bm1, mock_bm2]

    mock_anchor1 = MagicMock()
    mock_anchor1.getString.return_value = "First Anchor"
    mock_bm1.getAnchor.return_value = mock_anchor1

    mock_anchor2 = MagicMock()
    mock_anchor2.getString.return_value = "" # empty text for point bookmark
    mock_bm2.getAnchor.return_value = mock_anchor2

    tool = ListBookmarks()
    res = tool.execute(mock_ctx)

    assert res["status"] == "ok"
    assert res["count"] == 2
    assert len(res["bookmarks"]) == 2
    assert res["bookmarks"][0]["name"] == "BM1"
    assert res["bookmarks"][0]["text"] == "First Anchor"
    assert res["bookmarks"][1]["name"] == "BM2"
    assert res["bookmarks"][1]["text"] == ""
