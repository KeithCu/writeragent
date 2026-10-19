import os
import tempfile
import zipfile

import uno

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.writer.specialized.bookmarks import BookmarkService


def _setup_headings(doc):
    text = doc.getText()
    cursor = text.createTextCursor()

    # 0: Heading 1
    text.insertString(cursor, "Main Heading", False)
    cursor.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertControlCharacter(cursor, 0, False)

    # 1: Paragraph
    text.insertString(cursor, "A simple paragraph.", False)
    cursor.setPropertyValue("ParaStyleName", "Standard")
    text.insertControlCharacter(cursor, 0, False)

    # 2: Heading 2
    text.insertString(cursor, "Sub Heading", False)
    cursor.setPropertyValue("ParaStyleName", "Heading 2")
    text.insertControlCharacter(cursor, 0, False)


def _mcp_names(doc):
    return [n for n in doc.getBookmarks().getElementNames() if n.startswith("_mcp_")]


def _mcp_count_in_odt(path):
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("content.xml").decode("utf-8")
    return xml.count("_mcp_")


@native_test
@with_native_doc("writer")
def test_ensure_heading_bookmarks_and_map(ctx, doc):
    _setup_headings(doc)
    bookmark_svc = BookmarkService()

    # Initially no bookmarks
    bms = doc.getBookmarks().getElementNames()
    assert len([b for b in bms if b.startswith("_mcp_")]) == 0

    # Ensure bookmarks
    bookmark_map = bookmark_svc.ensure_heading_bookmarks(doc)

    # We have 2 headings (index 0 and 2)
    assert len(bookmark_map) == 2
    assert 0 in bookmark_map
    assert 2 in bookmark_map

    # Verify in document
    bms = doc.getBookmarks().getElementNames()
    mcp_bms = [b for b in bms if b.startswith("_mcp_")]
    assert len(mcp_bms) == 2

    # Verify map retrieval
    retrieved_map = bookmark_svc.get_mcp_bookmark_map(doc)
    assert retrieved_map == bookmark_map


@native_test
@with_native_doc("writer")
def test_find_nearest_heading_bookmark(ctx, doc):
    _setup_headings(doc)
    bookmark_svc = BookmarkService()

    bookmark_map = bookmark_svc.ensure_heading_bookmarks(doc)

    # Nearest heading before or at index 1 is index 0
    res = bookmark_svc.find_nearest_heading_bookmark(1, bookmark_map)
    assert res is not None
    assert res["heading_para_index"] == 0
    assert res["bookmark"] == bookmark_map[0]

    # Nearest heading before or at index 2 is index 2
    res = bookmark_svc.find_nearest_heading_bookmark(2, bookmark_map)
    assert res is not None
    assert res["heading_para_index"] == 2
    assert res["bookmark"] == bookmark_map[2]


@native_test
@with_native_doc("writer")
def test_cleanup_mcp_bookmarks(ctx, doc):
    _setup_headings(doc)
    bookmark_svc = BookmarkService()

    # Ensure we have some bookmarks
    bookmark_svc.ensure_heading_bookmarks(doc)

    # Clean them up
    removed_count = bookmark_svc.cleanup_mcp_bookmarks(doc)
    assert removed_count == 2

    # Verify they are gone from document
    bms = doc.getBookmarks().getElementNames()
    mcp_bms = [b for b in bms if b.startswith("_mcp_")]
    assert len(mcp_bms) == 0

    # Verify map is empty on next read
    empty_map = bookmark_svc.get_mcp_bookmark_map(doc)
    assert len(empty_map) == 0


@native_test
@with_native_doc("writer")
def test_ensure_does_not_dirty_document(ctx, doc):
    _setup_headings(doc)
    doc.setModified(False)
    BookmarkService().ensure_heading_bookmarks(doc)
    assert doc.isModified() is False
    assert len(_mcp_names(doc)) == 2


@native_test
@with_native_doc("writer")
def test_tree_read_does_not_dirty_document(ctx, doc):
    from types import SimpleNamespace

    from plugin.doc.document_helpers import DocumentService
    from plugin.framework.event_bus import EventBus
    from plugin.writer.tree import TreeService

    _setup_headings(doc)
    doc.setModified(False)
    services = SimpleNamespace()
    services.document = DocumentService()
    services.events = EventBus()
    services.writer_bookmarks = BookmarkService(services)
    tree_svc = TreeService(services)
    tree_svc.get_document_tree(doc, content_strategy="heading_only")
    assert doc.isModified() is False
    assert len(_mcp_names(doc)) == 2


@native_test
@with_native_doc("writer")
def test_undo_after_ensure_undoes_user_type(ctx, doc):
    _setup_headings(doc)
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    text.insertString(cursor, "UNIQUE_TYPE", False)
    assert "UNIQUE_TYPE" in text.getString()

    bookmark_svc = BookmarkService()
    bookmark_map = bookmark_svc.ensure_heading_bookmarks(doc)
    assert len(bookmark_map) == 2

    um = doc.getUndoManager()
    assert um.isUndoPossible()
    um.undo()
    assert "UNIQUE_TYPE" not in text.getString()
    assert set(_mcp_names(doc)) == set(bookmark_map.values())


@native_test
@with_native_doc("writer")
def test_strip_on_save_omits_mcp_from_file(ctx, doc):
    _setup_headings(doc)
    bookmark_svc = BookmarkService()
    bookmark_map = bookmark_svc.ensure_heading_bookmarks(doc)
    names_before = set(bookmark_map.values())
    assert names_before

    fd, path = tempfile.mkstemp(suffix=".odt")
    os.close(fd)
    try:
        doc.storeAsURL(uno.systemPathToFileUrl(path), ())
        assert _mcp_count_in_odt(path) == 0
        assert set(_mcp_names(doc)) == names_before
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@native_test
@with_native_doc("writer")
def test_save_as_restores_same_names(ctx, doc):
    _setup_headings(doc)
    bookmark_svc = BookmarkService()
    names = set(bookmark_svc.ensure_heading_bookmarks(doc).values())
    fd, path = tempfile.mkstemp(suffix=".odt")
    os.close(fd)
    try:
        doc.storeAsURL(uno.systemPathToFileUrl(path), ())
        assert set(_mcp_names(doc)) == names
        assert _mcp_count_in_odt(path) == 0
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@native_test
@with_native_doc("writer")
def test_save_a_copy_keeps_original_modified(ctx, doc):
    _setup_headings(doc)
    bookmark_svc = BookmarkService()
    names = set(bookmark_svc.ensure_heading_bookmarks(doc).values())
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    text.insertString(cursor, "EDIT_AFTER_BOOKMARKS", False)
    assert doc.isModified() is True

    fd, path = tempfile.mkstemp(suffix=".odt")
    os.close(fd)
    try:
        doc.storeToURL(uno.systemPathToFileUrl(path), ())
        assert doc.isModified() is True
        assert set(_mcp_names(doc)) == names
        assert _mcp_count_in_odt(path) == 0
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
