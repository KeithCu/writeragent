from typing import Any

from plugin.doc.document_helpers import (
    build_heading_tree,
    resolve_locator,
    get_paragraph_ranges,
    get_document_length,
    ensure_heading_bookmarks,
    get_document_context_for_chat,
    get_string_without_tracked_deletions,
    WriterStreamedRewriteSession,
)
from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test
from plugin.tests.testing_utils import TestingFactory


_test_doc: Any = None
_test_ctx: Any = None


@setup
def setup_doc_helpers_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx

    _test_doc = TestingFactory.create_native_doc(ctx, "writer", hidden=True)
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

    # Populate cache.length
    get_document_length(_test_doc)


@teardown
def teardown_doc_helpers_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


@native_test
def test_ensure_heading_bookmarks():
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
    ranges = get_paragraph_ranges(_test_doc)
    assert len(ranges) == 5, f"get_paragraph_ranges expected 5 paragraphs, got {len(ranges)}"


@native_test
def test_get_string_without_tracked_deletions_hides_deleted_text():
    import uno

    desktop = get_desktop(_test_ctx)
    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))
    assert doc is not None, "Could not create hidden Writer document"

    try:
        text = doc.getText()
        cursor = text.createTextCursor()
        text.insertString(cursor, "Alpha Beta", False)

        doc.setPropertyValue("RecordChanges", True)
        cursor.gotoStart(False)
        cursor.goRight(6, False)
        cursor.goRight(4, True)
        cursor.setString("")

        full_range = text.createTextCursor()
        full_range.gotoStart(False)
        full_range.gotoEnd(True)

        assert get_string_without_tracked_deletions(full_range) == "Alpha "

        redline_enum = doc.getRedlines().createEnumeration()
        assert redline_enum.hasMoreElements(), "Expected a tracked deletion redline"
    finally:
        doc.close(True)


@native_test
def test_get_document_context_for_chat_hides_tracked_deletions():
    import uno

    desktop = get_desktop(_test_ctx)
    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))
    assert doc is not None, "Could not create hidden Writer document"

    try:
        text = doc.getText()
        cursor = text.createTextCursor()
        text.insertString(cursor, "Hello Beta", False)

        doc.setPropertyValue("RecordChanges", True)
        cursor.gotoStart(False)
        cursor.goRight(6, False)
        cursor.goRight(4, True)
        cursor.setString("")

        ctx = get_document_context_for_chat(doc, include_selection=False)

        assert "Hello " in ctx
        assert "Beta" not in ctx
    finally:
        doc.close(True)


@native_test
def test_writer_streamed_rewrite_session_collapses_chunked_edit():
    import uno

    desktop = get_desktop(_test_ctx)
    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))
    assert doc is not None, "Could not create hidden Writer document"

    try:
        text = doc.getText()
        cursor = text.createTextCursor()
        text.insertString(cursor, "Alpha Beta", False)

        doc.setPropertyValue("RecordChanges", True)
        cursor.gotoStart(False)
        cursor.goRight(6, False)
        cursor.goRight(4, True)

        session = WriterStreamedRewriteSession(doc, cursor, "Beta")
        session.append_chunk("Ga")
        session.append_chunk("mma")
        warning = session.finish()

        assert warning is None

        full_range = text.createTextCursor()
        full_range.gotoStart(False)
        full_range.gotoEnd(True)
        assert get_string_without_tracked_deletions(full_range) == "Alpha Gamma"

        redlines = doc.getRedlines().createEnumeration()
        count = 0
        while redlines.hasMoreElements():
            redlines.nextElement()
            count += 1
        assert 1 <= count <= 2, f"Expected one clean replacement, got {count} redlines"

        um = doc.getUndoManager()
        assert um.isUndoPossible()
        um.undo()
        full_range_undo = text.createTextCursor()
        full_range_undo.gotoStart(False)
        full_range_undo.gotoEnd(True)
        assert get_string_without_tracked_deletions(full_range_undo) == "Alpha Beta"
    finally:
        doc.close(True)


@native_test
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


@native_test
def test_resolve_locator():
    res1 = resolve_locator(_test_doc, "paragraph:1")
    assert res1 and res1["para_index"] == 1, f"resolve_locator paragraph:1 failed: {res1}"

    res2 = resolve_locator(_test_doc, "heading:2") # should be index 4 (H2)
    assert res2 and res2["para_index"] == 4, f"resolve_locator heading:2 failed: {res2}"

    res3 = resolve_locator(_test_doc, "heading:1.1") # should be index 2 (H1.1)
    assert res3 and res3["para_index"] == 2, f"resolve_locator heading:1.1 failed: {res3}"
