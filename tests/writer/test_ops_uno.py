
from plugin.doc.paragraph_search import find_paragraph_for_range, get_paragraph_ranges
from plugin.doc.text_helpers import normalize_linebreaks, read_visible_body, visible_body_range
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
def test_visible_body_range(ctx, doc):
    _populate_ops_doc(doc)
    visible_text, _ = read_visible_body(doc)

    # Just grab a known length from the visible body text
    start_idx = 0
    end_idx = min(3, len(visible_text))

    found = visible_body_range(doc, start_idx, end_idx)
    assert found is not None, "visible_body_range returned None"

    selected_text = normalize_linebreaks(found.cursor.getString())
    expected_text = visible_text[start_idx:end_idx]

    assert found.text == expected_text
    assert selected_text == expected_text, (
        f"visible_body_range mismatch. Expected '{expected_text}', got '{selected_text}'"
    )


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
