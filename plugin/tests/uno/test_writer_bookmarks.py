try:
    import uno
    import pytest
except ImportError:
    pass

try:
    from plugin.testing_runner import setup, teardown, native_test
    from plugin.framework.uno_helpers import get_desktop
    from plugin.framework.document import DocumentService
    from plugin.framework.events import EventBus
    from plugin.modules.writer.bookmarks import BookmarkService
except ImportError:
    setup, teardown, native_test = (lambda f: f), (lambda f: f), (lambda f: f)

_test_doc = None
_test_ctx = None

@setup
def setup_bookmark_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx

    desktop = get_desktop(ctx)
    from com.sun.star.beans import PropertyValue
    hidden_prop = PropertyValue()
    hidden_prop.Name = "Hidden"
    hidden_prop.Value = True
    _test_doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))

    text = _test_doc.getText()
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

@teardown
def teardown_bookmark_tests():
    if _test_doc:
        _test_doc.close(True)

@native_test
def test_ensure_heading_bookmarks_and_map():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass

    events = EventBus()
    doc_svc = DocumentService(events)
    bookmark_svc = BookmarkService(doc_svc, events)

    # Initially no bookmarks
    bms = _test_doc.getBookmarks().getElementNames()
    assert len([b for b in bms if b.startswith("_mcp_")]) == 0

    # Ensure bookmarks
    bookmark_map = bookmark_svc.ensure_heading_bookmarks(_test_doc)

    # We have 2 headings (index 0 and 2)
    assert len(bookmark_map) == 2
    assert 0 in bookmark_map
    assert 2 in bookmark_map

    # Verify in document
    bms = _test_doc.getBookmarks().getElementNames()
    mcp_bms = [b for b in bms if b.startswith("_mcp_")]
    assert len(mcp_bms) == 2

    # Verify map retrieval
    retrieved_map = bookmark_svc.get_mcp_bookmark_map(_test_doc)
    assert retrieved_map == bookmark_map

@native_test
def test_find_nearest_heading_bookmark():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass

    events = EventBus()
    doc_svc = DocumentService(events)
    bookmark_svc = BookmarkService(doc_svc, events)

    bookmark_map = bookmark_svc.ensure_heading_bookmarks(_test_doc)

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
def test_cleanup_mcp_bookmarks():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass

    events = EventBus()
    doc_svc = DocumentService(events)
    bookmark_svc = BookmarkService(doc_svc, events)

    # Ensure we have some bookmarks
    bookmark_svc.ensure_heading_bookmarks(_test_doc)

    # Clean them up
    removed_count = bookmark_svc.cleanup_mcp_bookmarks(_test_doc)
    assert removed_count == 2

    # Verify they are gone from document
    bms = _test_doc.getBookmarks().getElementNames()
    mcp_bms = [b for b in bms if b.startswith("_mcp_")]
    assert len(mcp_bms) == 0

    # Verify cache is cleared implicitly on next read
    empty_map = bookmark_svc.get_mcp_bookmark_map(_test_doc)
    assert len(empty_map) == 0
