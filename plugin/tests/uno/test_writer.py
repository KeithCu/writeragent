from types import SimpleNamespace
from typing import Any

from plugin.framework.document import (
    build_heading_tree,
    resolve_locator,
    get_paragraph_ranges,
    get_document_length,
)
from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test


_test_doc: Any = None
_test_ctx: Any = None


@setup
def setup_writer_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx

    desktop = get_desktop(ctx)
    from com.sun.star.beans import PropertyValue
    hidden_prop = PropertyValue()
    hidden_prop.Name = "Hidden"
    hidden_prop.Value = True
    _test_doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))
    assert _test_doc is not None, "Could not create hidden test writer document"

    # 1. Setup doc content
    text = _test_doc.getText()
    cursor = text.createTextCursor()

    # H1
    text.insertString(cursor, "H1", False)
    cursor.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertControlCharacter(cursor, 0, False) # PARAGRAPH_BREAK

    # P1
    text.insertString(cursor, "P1", False)
    try:
        cursor.setPropertyValue("ParaStyleName", "Default Paragraph Style")
    except Exception:
        cursor.setPropertyValue("ParaStyleName", "Standard")
    text.insertControlCharacter(cursor, 0, False)

    # H1.1
    text.insertString(cursor, "H1.1", False)
    cursor.setPropertyValue("ParaStyleName", "Heading 2")
    text.insertControlCharacter(cursor, 0, False)

    # P2
    text.insertString(cursor, "P2", False)
    try:
        cursor.setPropertyValue("ParaStyleName", "Default Paragraph Style")
    except Exception:
        cursor.setPropertyValue("ParaStyleName", "Standard")
    text.insertControlCharacter(cursor, 0, False)

    # H2
    text.insertString(cursor, "H2", False)
    cursor.setPropertyValue("ParaStyleName", "Heading 1")

    # Populate cache.length so DummyDocSvc and cache test can use it
    get_document_length(_test_doc)


@teardown
def teardown_writer_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


@native_test
def test_proximity_service():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass
    from plugin.modules.writer.proximity import ProximityService
    from plugin.modules.writer.bookmarks import BookmarkService
    from plugin.modules.writer.tree import TreeService
    from plugin.framework.event_bus import EventBus
    from plugin.modules.writer.ops import find_paragraph_for_range as ops_find_paragraph_for_range

    events = EventBus()

    class DocSvcAdapter:
        def doc_key(self, doc):
            return id(doc)
        def get_document_length(self, model):
            return get_document_length(model)
        def resolve_locator(self, doc, locator):
            return resolve_locator(doc, locator)
        def get_paragraph_ranges(self, doc):
            return get_paragraph_ranges(doc)
        def find_paragraph_for_range(self, anchor, para_ranges, text_obj):
            return ops_find_paragraph_for_range(anchor, para_ranges, text_obj)
        def yield_to_gui(self):
            pass

    services = SimpleNamespace()
    services.document = DocSvcAdapter()
    services.events = events
    services.writer_bookmarks = BookmarkService(services)
    services.writer_tree = TreeService(services)
    services.writer_proximity = ProximityService(services)

    res = services.writer_proximity.get_surroundings(_test_doc, "paragraph:0", radius=0)
    assert res is not None and res.get("center_para_index") == 0, f"ProximityService get_surroundings failed: {res}"


@native_test
def test_content_has_markup():
    from plugin.modules.writer.format_support import content_has_markup
    assert content_has_markup("**bold**"), "content_has_markup failed to detect **bold**"
    assert not content_has_markup("plain text"), "content_has_markup falsely detected plain text"


@native_test
def test_ensure_heading_bookmarks():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass
    from plugin.framework.document import ensure_heading_bookmarks
    ensure_heading_bookmarks(_test_doc)
    bookmarks = _test_doc.getBookmarks()
    bnames = bookmarks.getElementNames()
    assert len(bnames) == 3, f"ensure_heading_bookmarks created {len(bnames)} bookmarks instead of 3"

    # Running ensure_heading_bookmarks again should not duplicate
    ensure_heading_bookmarks(_test_doc)
    bnames = _test_doc.getBookmarks().getElementNames()
    assert len(bnames) == 3, f"ensure_heading_bookmarks duplicated bookmarks, total: {len(bnames)}"


@native_test
def test_get_paragraph_ranges():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass
    ranges = get_paragraph_ranges(_test_doc)
    assert len(ranges) == 5, f"get_paragraph_ranges expected 5 paragraphs, got {len(ranges)}"


@native_test
def test_build_heading_tree():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass
    tree = build_heading_tree(_test_doc)
    assert "children" in tree and len(tree["children"]) == 2, "build_heading_tree did not find 2 root children"
    h1 = tree["children"][0]
    h2 = tree["children"][1]
    assert h1["text"] == "H1", "H1 text mismatch"
    assert len(h1["children"]) == 1, "H1 child count mismatch"
    assert h1["children"][0]["text"] == "H1.1", "H1.1 text mismatch"
    assert h2["text"] == "H2", "H2 text mismatch"
    assert h2["body_paragraphs"] == 0, "H2 body paragraphs mismatch"


@native_test
def test_resolve_locator():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass
    res1 = resolve_locator(_test_doc, "paragraph:1")
    assert res1 and res1["para_index"] == 1, f"resolve_locator paragraph:1 failed: {res1}"

    res2 = resolve_locator(_test_doc, "heading:2") # should be index 4 (H2)
    assert res2 and res2["para_index"] == 4, f"resolve_locator heading:2 failed: {res2}"

    res3 = resolve_locator(_test_doc, "heading:1.1") # should be index 2 (H1.1)
    assert res3 and res3["para_index"] == 2, f"resolve_locator heading:1.1 failed: {res3}"


@native_test
def test_writer_structural_and_tree_service():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass

    from plugin.modules.writer.tree import TreeService
    from plugin.modules.writer.bookmarks import BookmarkService
    from plugin.framework.event_bus import EventBus
    from plugin.framework.document import DocumentService
    events = EventBus()
    doc_svc = DocumentService()
    services = SimpleNamespace()
    services.document = doc_svc
    services.events = events
    services.writer_bookmarks = BookmarkService(services)
    services.writer_tree = TreeService(services)
    bm_svc = services.writer_bookmarks
    tree_svc = services.writer_tree

    # 1. Test build_heading_tree from TreeService natively
    tree = tree_svc.build_heading_tree(_test_doc)
    assert tree is not None, "TreeService.build_heading_tree returned None"
    assert "children" in tree and len(tree["children"]) == 2, f"Expected 2 root children, got {len(tree.get('children', []))}"

    h1 = tree["children"][0]
    assert h1["text"] == "H1", "First child should be H1"
    assert len(h1["children"]) == 1, "H1 should have 1 child"
    assert h1["children"][0]["text"] == "H1.1", "H1 child should be H1.1"

    # 2. Test resolve_writer_locator from TreeService natively
    # H1 is index 0, P1 is index 1, H1.1 is index 2
    res = tree_svc.resolve_writer_locator(_test_doc, "heading", "1.1")
    assert res is not None and res.get("para_index") == 2, f"Failed to resolve heading:1.1, got {res}"

    res = tree_svc.resolve_writer_locator(_test_doc, "heading_text", "H1.1")
    assert res is not None and res.get("para_index") == 2, f"Failed to resolve heading_text:H1.1, got {res}"

    # 3. Test structural.py tools natively
    class MockCtx:
        def __init__(self, doc, services):
            self.doc = doc
            self.services = services

    class MockServices:
        def __init__(self, bm_svc, doc_svc):
            self.writer_bookmarks = bm_svc
            self.document = doc_svc

    mock_ctx = MockCtx(_test_doc, MockServices(bm_svc, doc_svc))

    # Test ListBookmarks via registry (Specialized API testing pattern)
    from plugin.main import get_tools
    registry = get_tools()
    list_bm_tool = registry.get("list_bookmarks")
    assert list_bm_tool is not None, "list_bookmarks tool not found in registry"
    
    bm_res = list_bm_tool.execute(mock_ctx)
    assert bm_res["status"] == "ok", f"ListBookmarks failed: {bm_res}"
    # Initially we might have 0 bookmarks unless they were created by ensure_heading_bookmarks in previous test
    # but the API call itself should succeed
    assert isinstance(bm_res["bookmarks"], list), "ListBookmarks should return a list"

    list_sec_tool = registry.get("list_sections")
    assert list_sec_tool is not None, "list_sections (structural domain) should be registered"
    sec_res = list_sec_tool.execute(mock_ctx)
    assert sec_res["status"] == "ok", f"ListSections failed: {sec_res}"
    assert isinstance(sec_res["sections"], list), "ListSections should return a list"


@native_test
def test_get_text_cursor_at_range():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass
    from plugin.modules.writer.ops import get_text_cursor_at_range

    text = _test_doc.getText()
    full_text_str = text.getString()

    # Just grab a known length from the doc string
    start_idx = 0
    end_idx = min(3, len(full_text_str))

    cursor = get_text_cursor_at_range(_test_doc, start_idx, end_idx)
    assert cursor is not None, "get_text_cursor_at_range returned None"

    selected_text = cursor.getString()
    expected_text = full_text_str[start_idx:end_idx]

    assert selected_text == expected_text, f"get_text_cursor_at_range mismatch. Expected '{expected_text}', got '{selected_text}'"


@native_test
def test_find_paragraph_for_range():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass
    from plugin.modules.writer.ops import find_paragraph_for_range

    para_ranges = get_paragraph_ranges(_test_doc)
    text = _test_doc.getText()

    assert len(para_ranges) >= 2, "Test document doesn't have enough paragraphs."

    # Take the start of the second paragraph
    p1 = para_ranges[1]

    # We create a cursor at the start of p1
    cursor = text.createTextCursorByRange(p1.getStart())

    idx = find_paragraph_for_range(cursor, para_ranges, text)
    assert idx == 1, f"find_paragraph_for_range expected index 1, got {idx}"


@native_test
def test_get_selection_range():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass
    from plugin.modules.writer.ops import get_selection_range

    controller = _test_doc.getCurrentController()
    view_cursor = controller.getViewCursor()
    view_cursor.gotoStart(False)
    view_cursor.goRight(3, True)

    start_offset, end_offset = get_selection_range(_test_doc)

    # Start should be 0, end should be 3
    assert start_offset == 0, f"Expected start_offset 0, got {start_offset}"
    assert end_offset == 3, f"Expected end_offset 3, got {end_offset}"


@native_test
def test_insert_html_at_cursor():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass
    from plugin.modules.writer.ops import insert_html_at_cursor

    text = _test_doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    html_content = "<b>Test HTML Insert</b>"

    success = insert_html_at_cursor(cursor, html_content)
    assert success is True, "insert_html_at_cursor failed to return True"

    # Verify content was inserted. HTML tags shouldn't appear but the text should.
    doc_text = text.getString()
    assert "Test HTML Insert" in doc_text, "Inserted HTML text not found in document"
