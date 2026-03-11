from plugin.framework.document import (
    DocumentCache,
    build_heading_tree,
    resolve_locator,
    get_paragraph_ranges,
    get_document_length,
)
from plugin.framework.uno_helpers import get_desktop
from plugin.testing_runner import setup, teardown, test


_test_doc = None
_test_ctx = None


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
    cursor.setPropertyValue("ParaStyleName", "Default Paragraph Style")
    text.insertControlCharacter(cursor, 0, False)

    # H1.1
    text.insertString(cursor, "H1.1", False)
    cursor.setPropertyValue("ParaStyleName", "Heading 2")
    text.insertControlCharacter(cursor, 0, False)

    # P2
    text.insertString(cursor, "P2", False)
    cursor.setPropertyValue("ParaStyleName", "Default Paragraph Style")
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


@test
def test_proximity_service():
    from plugin.modules.writer.proximity import ProximityService
    from plugin.modules.writer.bookmarks import BookmarkService
    from plugin.modules.writer.tree import TreeService
    from plugin.framework.events import EventBus

    events = EventBus()

    cache3 = DocumentCache.get(_test_doc)

    class DummyDocSvc:
        def get_document_length(self, model):
            return cache3.length

    doc_svc = DummyDocSvc()
    bm = BookmarkService(doc_svc, events)
    tree_svc = TreeService(doc_svc, bm, events)
    prox = ProximityService(doc_svc, tree_svc, bm, events)

    res = prox.get_context_at_offset(_test_doc, 0)
    assert res is not None and res["paragraph_index"] == 0, f"ProximityService get_context_at_offset failed: {res}"


@test
def test_content_has_markup():
    from plugin.modules.writer.format_support import content_has_markup
    assert content_has_markup("**bold**"), "content_has_markup failed to detect **bold**"
    assert not content_has_markup("plain text"), "content_has_markup falsely detected plain text"


@test
def test_ensure_heading_bookmarks():
    from plugin.modules.writer.bookmarks import ensure_heading_bookmarks
    ensure_heading_bookmarks(_test_doc)
    bookmarks = _test_doc.getBookmarks()
    bnames = bookmarks.getElementNames()
    assert len(bnames) == 3, f"ensure_heading_bookmarks created {len(bnames)} bookmarks instead of 3"

    # Running ensure_heading_bookmarks again should not duplicate
    ensure_heading_bookmarks(_test_doc)
    bnames = _test_doc.getBookmarks().getElementNames()
    assert len(bnames) == 3, f"ensure_heading_bookmarks duplicated bookmarks, total: {len(bnames)}"


@test
def test_get_paragraph_ranges():
    ranges = get_paragraph_ranges(_test_doc)
    assert len(ranges) == 5, f"get_paragraph_ranges expected 5 paragraphs, got {len(ranges)}"


@test
def test_build_heading_tree():
    tree = build_heading_tree(_test_doc)
    assert "children" in tree and len(tree["children"]) == 2, "build_heading_tree did not find 2 root children"
    h1 = tree["children"][0]
    h2 = tree["children"][1]
    assert h1["text"] == "H1", "H1 text mismatch"
    assert len(h1["children"]) == 1, "H1 child count mismatch"
    assert h1["children"][0]["text"] == "H1.1", "H1.1 text mismatch"
    assert h2["text"] == "H2", "H2 text mismatch"
    assert h2["body_paragraphs"] == 0, "H2 body paragraphs mismatch"


@test
def test_resolve_locator():
    res1 = resolve_locator(_test_doc, "paragraph:1")
    assert res1 and res1["para_index"] == 1, f"resolve_locator paragraph:1 failed: {res1}"

    res2 = resolve_locator(_test_doc, "heading:2") # should be index 4 (H2)
    assert res2 and res2["para_index"] == 4, f"resolve_locator heading:2 failed: {res2}"

    res3 = resolve_locator(_test_doc, "heading:1.1") # should be index 2 (H1.1)
    assert res3 and res3["para_index"] == 2, f"resolve_locator heading:1.1 failed: {res3}"


@test
def test_document_cache_length_tracking():
    cache3 = DocumentCache.get(_test_doc)
    _ = get_document_length(_test_doc)
    prev_len = cache3.length
    assert prev_len is not None and prev_len > 0, "DocumentCache length not properly initialized"

    text = _test_doc.getText()
    cursor = text.createTextCursor()
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "More text", False)
    DocumentCache.invalidate(_test_doc)
    _ = get_document_length(_test_doc)
    cache3_new = DocumentCache.get(_test_doc)
    new_len = cache3_new.length
    assert new_len is not None and new_len > prev_len, "DocumentCache length did not update"
