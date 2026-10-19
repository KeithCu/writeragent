import pytest
import sys
import types
from unittest.mock import MagicMock

# Ensure UNO is mocked if running outside LibreOffice
try:
    import uno  # noqa: F401
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

from plugin.tests.testing_utils import TestingFactory
from plugin.writer.specialized.bookmarks import (
    BookmarkCreate,
    BookmarkDelete,
    BookmarkRename,
    BookmarkGet,
    BookmarkList,
    BookmarkService,
)


@pytest.fixture
def mock_ctx():
    return TestingFactory.create_context(doc=MagicMock(), doc_type="writer")


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

    tool = BookmarkCreate()
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

    tool = BookmarkCreate()
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

    tool = BookmarkDelete()
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

    tool = BookmarkRename()
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

    tool = BookmarkGet()
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

    tool = BookmarkList()
    res = tool.execute(mock_ctx)

    assert res["status"] == "ok"
    assert res["count"] == 2
    assert len(res["bookmarks"]) == 2
    assert res["bookmarks"][0]["name"] == "BM1"
    assert res["bookmarks"][0]["text"] == "First Anchor"
    assert res["bookmarks"][1]["name"] == "BM2"
    assert res["bookmarks"][1]["text"] == ""


def test_list_bookmarks_error(mock_ctx):
    mock_ctx.doc.getBookmarks.side_effect = RuntimeError("boom")
    res = BookmarkList().execute_safe(mock_ctx)
    assert res["status"] == "error"
    assert "boom" in res["message"]


def test_bookmarks_disposal_is_document_disposed(mock_ctx):
    from plugin.framework.errors import DocumentDisposedError

    tool = BookmarkList()
    mock_ctx.doc.getBookmarks.side_effect = DocumentDisposedError("gone")
    res = tool.execute_safe(mock_ctx)
    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"


def test_untracked_restores_modified_and_unlocks_on_error():
    doc = MagicMock()
    doc.isModified.return_value = False
    um = MagicMock()
    doc.getUndoManager.return_value = um

    with pytest.raises(RuntimeError):
        with BookmarkService()._untracked(doc):
            raise RuntimeError("insert failed")

    um.lock.assert_called_once()
    um.unlock.assert_called_once()
    doc.setModified.assert_called_with(False)

# ---- 1) reads never doc.store() ----------------------------------------------

def test_ensure_heading_bookmarks_never_stores():
    from plugin.writer.specialized.bookmarks import BookmarkService

    doc = MagicMock()
    text = MagicMock()
    enum = MagicMock()
    enum.hasMoreElements.return_value = False
    text.createEnumeration.return_value = enum
    doc.getText.return_value = text
    doc.getBookmarks.return_value.getElementNames.return_value = []
    BookmarkService().ensure_heading_bookmarks(doc)
    doc.store.assert_not_called()


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


class _Para:
    def __init__(self, text, level):
        self.text = text
        self.level = level

    def supportsService(self, name):
        return name == "com.sun.star.text.Paragraph"

    def getPropertyValue(self, name):
        if name == "OutlineLevel":
            return self.level
        raise KeyError(name)

    def getStart(self):
        return ("start", self.text)

    def getString(self):
        return self.text


class _OkAnchor:
    def __init__(self, para):
        self.para = para

    def getStart(self):
        return self.para

    def getString(self):
        return ""


class _Mark:
    def __init__(self, name, para=None, anchor=None):
        self.Name = name
        self._anchor = anchor if anchor is not None else _OkAnchor(para)

    def getAnchor(self):
        return self._anchor


class _HeadingDoc:
    def __init__(self, paras, bookmarks):
        self.paras = list(paras)
        self.bookmarks = dict(bookmarks)
        self.modified = False

    def getText(self):
        return self

    def createEnumeration(self):
        return _Enum(self.paras)

    def getBookmarks(self):
        return self

    def getElementNames(self):
        return list(self.bookmarks)

    def getByName(self, name):
        return self.bookmarks[name]

    def hasByName(self, name):
        return name in self.bookmarks

    def removeTextContent(self, bookmark):
        self.bookmarks.pop(getattr(bookmark, "Name", None), None)

    def createTextCursorByRange(self, start):
        return start

    def insertTextContent(self, cursor, bookmark, absorb):
        self.bookmarks[bookmark.Name] = bookmark

    def createInstance(self, name):
        return MagicMock()

    def isModified(self):
        return self.modified

    def setModified(self, value):
        self.modified = bool(value)

    def getUndoManager(self):
        raise RuntimeError("no undo")


def test_ensure_removes_mcp_bookmark_when_heading_is_demoted(monkeypatch):
    def fake_find(anchor, _ranges, _text=None):
        return anchor.para

    monkeypatch.setattr("plugin.writer.specialized.bookmarks.find_paragraph_for_range", fake_find)
    doc = _HeadingDoc(
        [
            _Para("Preamble", 0),
            _Para("Title", 1),
            _Para("Demoted", 0),
            _Para("Next", 1),
        ],
        {
            "_mcp_keep": _Mark("_mcp_keep", para=1),
            "_mcp_old": _Mark("_mcp_old", para=2),
            "Mine": _Mark("Mine", para=2),
        },
    )
    result = BookmarkService().ensure_heading_bookmarks(doc)
    assert result[1] == "_mcp_keep"
    assert result[3].startswith("_mcp_")
    assert result[3] != "_mcp_old"
    assert "_mcp_old" not in doc.bookmarks
    assert "Mine" in doc.bookmarks
    assert doc.modified is False


def test_ensure_drops_unplaced_mcp_bookmark_instead_of_paragraph_zero(monkeypatch):
    monkeypatch.setattr(
        "plugin.writer.specialized.bookmarks.find_paragraph_for_range",
        lambda *_args, **_kwargs: 0,
    )

    class _Broken:
        def getStart(self):
            raise RuntimeError("stale anchor")

        def getString(self):
            return ""

    doc = _HeadingDoc(
        [_Para("Title", 1)],
        {"_mcp_stale": _Mark("_mcp_stale", anchor=_Broken())},
    )
    result = BookmarkService().ensure_heading_bookmarks(doc)
    assert result[0].startswith("_mcp_")
    assert result[0] != "_mcp_stale"
    assert "_mcp_stale" not in doc.bookmarks


def test_ensure_keeps_demoted_bookmark_during_save_hook(monkeypatch):
    from plugin.writer.specialized.bookmarks import _in_save_hook

    def fake_find(anchor, _ranges, _text=None):
        return anchor.para

    monkeypatch.setattr("plugin.writer.specialized.bookmarks.find_paragraph_for_range", fake_find)
    doc = _HeadingDoc(
        [_Para("Preamble", 0), _Para("Title", 1), _Para("Demoted", 0)],
        {
            "_mcp_keep": _Mark("_mcp_keep", para=1),
            "_mcp_old": _Mark("_mcp_old", para=2),
        },
    )
    svc = BookmarkService()
    doc_key = svc._doc_key(doc)
    with _in_save_hook(doc_key):
        result = svc.ensure_heading_bookmarks(doc)
    assert result[1] == "_mcp_keep"
    assert "_mcp_old" in doc.bookmarks


def test_bookmark_resolve_rejects_unplaced_anchor():
    from plugin.writer.specialized.bookmarks import BookmarkResolve

    ctx = MagicMock()
    anchor = MagicMock()
    anchor.getStart.side_effect = RuntimeError("stale anchor")
    bookmark = MagicMock()
    bookmark.getAnchor.return_value = anchor
    ctx.doc.getBookmarks.return_value.hasByName.return_value = True
    ctx.doc.getBookmarks.return_value.getByName.return_value = bookmark
    ctx.services.document.find_paragraph_for_range.return_value = 0
    ctx.services.document.get_paragraph_ranges.return_value = [MagicMock()]
    result = BookmarkResolve().execute(ctx, name="_mcp_stale")
    assert result["status"] == "error"
    assert "anchor is not in the document" in result["message"]


def test_ensure_heading_bookmarks_raising_map_read(monkeypatch):
    from plugin.writer.specialized.bookmarks import BookmarkService

    doc = _HeadingDoc(
        [_Para("Title", 1)],
        {"_mcp_existing": _Mark("_mcp_existing", para=0)},
    )
    svc = BookmarkService()

    def raise_map(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(svc, "get_mcp_bookmark_map", raise_map)
    result = svc.ensure_heading_bookmarks(doc)
    assert result == {}
    assert "_mcp_existing" in doc.bookmarks


def test_strip_for_save_raising_map_read(monkeypatch):
    from plugin.writer.specialized.bookmarks import BookmarkService, _PENDING_STRIPS

    doc = _HeadingDoc(
        [_Para("Title", 1)],
        {"_mcp_existing": _Mark("_mcp_existing", para=0)},
    )
    svc = BookmarkService()

    def raise_map(*args, **kwargs):
        raise RuntimeError("boom")

    doc_key = "test_strip_key"
    monkeypatch.setattr(svc, "get_mcp_bookmark_map", raise_map)
    monkeypatch.setattr(svc, "_doc_key", lambda d: doc_key)
    svc.strip_for_save(doc)
    assert "_mcp_existing" in doc.bookmarks
    assert doc_key not in _PENDING_STRIPS


def test_restore_after_save_failure_leaves_pending(monkeypatch):
    from plugin.writer.specialized.bookmarks import BookmarkService, _PendingStrip, _PENDING_STRIPS

    doc = _HeadingDoc(
        [_Para("Title", 1)],
        {},
    )
    svc = BookmarkService()
    doc_key = "test_restore_key"
    monkeypatch.setattr(svc, "_doc_key", lambda d: doc_key)
    _PENDING_STRIPS[doc_key] = _PendingStrip({0: "_mcp_saved"}, False, doc)

    def raise_reinsert(*args, **kwargs):
        raise RuntimeError("reinsert error")

    monkeypatch.setattr(svc, "_reinsert_named", raise_reinsert)
    svc.restore_after_save(doc, saved=True)
    assert doc_key in _PENDING_STRIPS

    called_restore = []

    def fake_reinsert(target, names):
        called_restore.append(dict(names))
        return {}

    monkeypatch.setattr(svc, "_reinsert_named", fake_reinsert)
    svc.ensure_heading_bookmarks(doc)
    assert doc_key not in _PENDING_STRIPS
    assert called_restore == [{0: "_mcp_saved"}]


def test_bookmark_create_and_rename_reject_mcp(mock_ctx):
    create_tool = BookmarkCreate()
    res = create_tool.execute(mock_ctx, name="_mcp_custom")
    assert res["status"] == "error"
    assert "bookmark_cleanup" in res["message"]

    rename_tool = BookmarkRename()
    res2 = rename_tool.execute(mock_ctx, old_name="regular", new_name="_mcp_renamed")
    assert res2["status"] == "error"
    assert "bookmark_cleanup" in res2["message"]


def test_ensure_heading_bookmarks_collision_retries(monkeypatch):
    doc = _HeadingDoc(
        [_Para("Title", 1)],
        {},
    )
    svc = BookmarkService()
    calls = []

    def counting_has_by_name(name):
        calls.append(name)
        if len(calls) < 4:
            return True  # 3 collisions
        return False

    doc.getBookmarks().hasByName = counting_has_by_name
    result = svc.ensure_heading_bookmarks(doc)
    assert 0 in result
    # 4 calls in the retry loop (3 collisions + 1 success) + 1 call in _insert_named_bookmark
    assert len(calls) == 5
    assert calls[3] == calls[4]


def test_save_hook_key_doc_a_does_not_suppress_doc_b(monkeypatch):
    from plugin.writer.specialized.bookmarks import _in_save_hook, _PENDING_STRIPS, _PendingStrip

    def fake_find(anchor, _ranges, _text=None):
        return anchor.para

    monkeypatch.setattr("plugin.writer.specialized.bookmarks.find_paragraph_for_range", fake_find)

    doc_a = _HeadingDoc(
        [_Para("Title A", 1), _Para("Demoted A", 0)],
        {"_mcp_a_keep": _Mark("_mcp_a_keep", para=0), "_mcp_a_old": _Mark("_mcp_a_old", para=1)},
    )
    doc_b = _HeadingDoc(
        [_Para("Title B", 1), _Para("Demoted B", 0)],
        {"_mcp_b_keep": _Mark("_mcp_b_keep", para=0), "_mcp_b_old": _Mark("_mcp_b_old", para=1)},
    )
    svc = BookmarkService()
    key_a = "doc_key_a"
    key_b = "doc_key_b"
    monkeypatch.setattr(svc, "_doc_key", lambda d: key_a if d is doc_a else key_b)

    # Put an abandoned pending strip on doc_b
    _PENDING_STRIPS[key_b] = _PendingStrip({0: "_mcp_b_restored"}, False, doc_b)

    with _in_save_hook(key_a):
        # Doc A is in save hook: demoted is NOT pruned
        res_a = svc.ensure_heading_bookmarks(doc_a)
        assert "_mcp_a_old" in doc_a.bookmarks

        # Doc B is NOT in save hook: abandoned is restored and demoted IS pruned
        res_b = svc.ensure_heading_bookmarks(doc_b)
        assert key_b not in _PENDING_STRIPS
        assert "_mcp_b_old" not in doc_b.bookmarks

