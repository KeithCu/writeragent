from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.doc.paragraph_search import get_paragraph_ranges, find_paragraph_for_range as doc_find_para


def _setup_paragraphs(doc, count):
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    cursor.setString("")
    
    for i in range(count):
        text.insertString(cursor, f"Paragraph {i}", False)
        if i < count - 1:
            text.insertControlCharacter(cursor, 0, False) # com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK


@native_test
@with_native_doc("writer")
def test_find_para_boundaries(ctx, doc):
    _setup_paragraphs(doc, 5)
    para_ranges = get_paragraph_ranges(doc)
    text = doc.getText()
    
    # Test first paragraph (index 0)
    p0 = para_ranges[0]
    cursor = text.createTextCursorByRange(p0.getStart())
    idx = doc_find_para(cursor, para_ranges, text)
    assert idx == 0, f"Expected index 0 for first para, got {idx}"
    
    # Test last paragraph (index 4)
    p4 = para_ranges[4]
    cursor = text.createTextCursorByRange(p4.getStart())
    idx = doc_find_para(cursor, para_ranges, text)
    assert idx == 4, f"Expected index 4 for last para, got {idx}"


@native_test
@with_native_doc("writer")
def test_find_para_middle(ctx, doc):
    _setup_paragraphs(doc, 10)
    para_ranges = get_paragraph_ranges(doc)
    text = doc.getText()
    
    # Test middle paragraph (index 5)
    p5 = para_ranges[5]
    cursor = text.createTextCursorByRange(p5.getStart())
    idx = doc_find_para(cursor, para_ranges, text)
    assert idx == 5, f"Expected index 5 for middle para, got {idx}"
    
    # Test range inside middle paragraph (not at start)
    cursor.goRight(3, False)
    idx = doc_find_para(cursor, para_ranges, text)
    assert idx == 5, f"Expected index 5 for cursor inside middle para, got {idx}"


@native_test
@with_native_doc("writer")
def test_find_para_single(ctx, doc):
    _setup_paragraphs(doc, 1)
    para_ranges = get_paragraph_ranges(doc)
    text = doc.getText()
    
    p0 = para_ranges[0]
    cursor = text.createTextCursorByRange(p0.getStart())
    idx = doc_find_para(cursor, para_ranges, text)
    assert idx == 0, f"Expected index 0 for single para, got {idx}"


@native_test
@with_native_doc("writer")
def test_find_para_exactly_at_end(ctx, doc):
    _setup_paragraphs(doc, 3)
    para_ranges = get_paragraph_ranges(doc)
    text = doc.getText()
    
    # Test range exactly at end of P1
    p1 = para_ranges[1]
    cursor = text.createTextCursorByRange(p1.getEnd())
    idx = doc_find_para(cursor, para_ranges, text)
    assert idx == 1, f"Expected index 1 for cursor at end of P1, got {idx}"


@native_test
@with_native_doc("writer")
def test_find_para_large_document(ctx, doc):
    # Test with 150 paragraphs to ensure binary search efficiency and correctness
    count = 150
    _setup_paragraphs(doc, count)
    para_ranges = get_paragraph_ranges(doc)
    text = doc.getText()
    
    for i in [0, 1, 75, 149]:
        p = para_ranges[i]
        cursor = text.createTextCursorByRange(p.getStart())
        idx = doc_find_para(cursor, para_ranges, text)
        assert idx == i, f"Large doc: expected index {i}, got {idx}"
