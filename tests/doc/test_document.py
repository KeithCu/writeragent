"""Headless unit tests for document URL/excerpt helpers (not a native UNO suite)."""

from plugin.doc.document_helpers import _inject_markers_into_excerpt
from plugin.framework.uno_context import normalize_doc_url

def test_normalize_doc_url():
    assert normalize_doc_url("file:///test/") == "file:///test"
    assert normalize_doc_url("file:///test") == "file:///test"
    assert normalize_doc_url("") == ""
    assert normalize_doc_url(None) == ""

def test_inject_markers_into_excerpt():
    text = "0123456789"
    # Excerpt covers 0-10, selection is 2-5
    out = _inject_markers_into_excerpt(text, 0, 10, 2, 5, "PRE", "SUF")
    assert out == "PRE01[SELECTION_START]234[SELECTION_END]56789SUF"

    # Excerpt covers 10-20, selection is 5-8 (outside excerpt)
    out = _inject_markers_into_excerpt(text, 10, 20, 5, 8, "PRE", "SUF")
    assert out == "PRE0123456789SUF"

def test_is_document_disposed():
    from plugin.framework.errors import is_document_disposed

    assert is_document_disposed(None) is True

    try:
        from com.sun.star.lang import DisposedException
        disposed_exc = DisposedException("Document disposed")
    except ImportError:
        disposed_exc = Exception("Mock disposed")

    class MockDisposedDoc:
        def getImplementationName(self):
            raise disposed_exc

    class MockValidDoc:
        def getImplementationName(self):
            return "SwXTextDocument"

    assert is_document_disposed(MockDisposedDoc()) is True
    assert is_document_disposed(MockValidDoc()) is False


