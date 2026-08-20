
from plugin.doc.document_helpers import (
    get_paragraph_ranges,
    get_text_cursor_at_range,
    find_paragraph_for_range,
    get_selection_range,
)
from plugin.writer.html_import import insert_html_fragment_at_cursor
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


def _populate_ops_doc(doc):
    text = doc.getText()
    cursor = text.createTextCursor()
    text.insertString(cursor, "P1", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "P2", False)


@native_test
@with_native_doc("writer")
def test_get_text_cursor_at_range(ctx, doc):
    _populate_ops_doc(doc)
    text = doc.getText()
    full_text_str = text.getString()

    # Just grab a known length from the doc string
    start_idx = 0
    end_idx = min(3, len(full_text_str))

    cursor = get_text_cursor_at_range(doc, start_idx, end_idx)
    assert cursor is not None, "get_text_cursor_at_range returned None"

    selected_text = cursor.getString()
    expected_text = full_text_str[start_idx:end_idx]

    assert selected_text == expected_text, f"get_text_cursor_at_range mismatch. Expected '{expected_text}', got '{selected_text}'"


@native_test
@with_native_doc("writer")
def test_find_paragraph_for_range(ctx, doc):
    _populate_ops_doc(doc)
    para_ranges = get_paragraph_ranges(doc)
    text = doc.getText()

    assert len(para_ranges) >= 2, "Test document doesn't have enough paragraphs."

    # Take the start of the second paragraph
    p1 = para_ranges[1]

    # We create a cursor at the start of p1
    cursor = text.createTextCursorByRange(p1.getStart())

    idx = find_paragraph_for_range(cursor, para_ranges, text)
    assert idx == 1, f"find_paragraph_for_range expected index 1, got {idx}"


@native_test
@with_native_doc("writer")
def test_get_selection_range(ctx, doc):
    _populate_ops_doc(doc)
    controller = doc.getCurrentController()
    view_cursor = controller.getViewCursor()
    view_cursor.gotoStart(False)
    view_cursor.goRight(3, True)

    start_offset, end_offset = get_selection_range(doc)

    # Start should be 0, end should be 3
    assert start_offset == 0, f"Expected start_offset 0, got {start_offset}"
    assert end_offset == 3, f"Expected end_offset 3, got {end_offset}"


@native_test
@with_native_doc("writer")
def test_insert_html_fragment_at_cursor(ctx, doc):
    _populate_ops_doc(doc)
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)

    html_content = "<b>Test HTML Insert</b>"

    insert_html_fragment_at_cursor(cursor, html_content, wrap=True)

    # Verify content was inserted. HTML tags shouldn't appear but the text should.
    doc_text = text.getString()
    assert "Test HTML Insert" in doc_text, "Inserted HTML text not found in document"
