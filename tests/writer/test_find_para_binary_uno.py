import logging
from plugin.testing_runner import setup, teardown, native_test
from plugin.tests.testing_utils import TestingFactory
from plugin.doc.document_helpers import get_paragraph_ranges, find_paragraph_for_range as doc_find_para
from plugin.writer.ops import find_paragraph_for_range as ops_find_para

_test_doc = None
_lg = logging.getLogger(__name__)

@setup
def setup_binary_search_tests(ctx):
    global _test_doc
    _test_doc = TestingFactory.create_native_doc(ctx, "writer", hidden=True)

@teardown
def teardown_binary_search_tests(ctx):
    global _test_doc
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None

def _setup_paragraphs(count):
    text = _test_doc.getText()
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEnd(True)
    cursor.setString("")
    
    for i in range(count):
        text.insertString(cursor, f"Paragraph {i}", False)
        if i < count - 1:
            text.insertControlCharacter(cursor, 0, False) # com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK

@native_test
def test_find_para_boundaries():
    _setup_paragraphs(5)
    para_ranges = get_paragraph_ranges(_test_doc)
    text = _test_doc.getText()
    
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
def test_find_para_middle():
    _setup_paragraphs(10)
    para_ranges = get_paragraph_ranges(_test_doc)
    text = _test_doc.getText()
    
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
def test_find_para_single():
    _setup_paragraphs(1)
    para_ranges = get_paragraph_ranges(_test_doc)
    text = _test_doc.getText()
    
    p0 = para_ranges[0]
    cursor = text.createTextCursorByRange(p0.getStart())
    idx = doc_find_para(cursor, para_ranges, text)
    assert idx == 0, f"Expected index 0 for single para, got {idx}"

@native_test
def test_find_para_exactly_at_end():
    _setup_paragraphs(3)
    para_ranges = get_paragraph_ranges(_test_doc)
    text = _test_doc.getText()
    
    # Test range exactly at end of P1
    p1 = para_ranges[1]
    cursor = text.createTextCursorByRange(p1.getEnd())
    idx = doc_find_para(cursor, para_ranges, text)
    assert idx == 1, f"Expected index 1 for cursor at end of P1, got {idx}"

@native_test
def test_find_para_large_document():
    # Test with 150 paragraphs to ensure binary search efficiency and correctness
    count = 150
    _setup_paragraphs(count)
    para_ranges = get_paragraph_ranges(_test_doc)
    text = _test_doc.getText()
    
    for i in [0, 1, 75, 149]:
        p = para_ranges[i]
        cursor = text.createTextCursorByRange(p.getStart())
        idx = doc_find_para(cursor, para_ranges, text)
        assert idx == i, f"Large doc: expected index {i}, got {idx}"

@native_test
def test_find_para_ops_equivalence():
    # Ensure ops.py version behaves the same way
    _setup_paragraphs(5)
    para_ranges = get_paragraph_ranges(_test_doc)
    text = _test_doc.getText()
    
    p2 = para_ranges[2]
    cursor = text.createTextCursorByRange(p2.getStart())
    
    idx_doc = doc_find_para(cursor, para_ranges, text)
    idx_ops = ops_find_para(cursor, para_ranges, text)
    
    assert idx_doc == 2
    assert idx_ops == 2
    assert idx_doc == idx_ops
