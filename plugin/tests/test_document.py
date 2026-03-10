import pytest

from plugin.framework.document import (
    normalize_linebreaks,
    _normalize_doc_url,
    _inject_markers_into_excerpt
)

def test_normalize_linebreaks():
    assert normalize_linebreaks("hello\r\nworld") == "hello\nworld"
    assert normalize_linebreaks("hello\n\rworld") == "hello\nworld"
    assert normalize_linebreaks("hello\rworld") == "hello\nworld"
    assert normalize_linebreaks("") == ""
    assert normalize_linebreaks(None) == ""

def test_normalize_doc_url():
    assert _normalize_doc_url("file:///test/") == "file:///test"
    assert _normalize_doc_url("file:///test") == "file:///test"
    assert _normalize_doc_url("") == ""
    assert _normalize_doc_url(None) == ""

def test_inject_markers_into_excerpt():
    text = "0123456789"
    # Excerpt covers 0-10, selection is 2-5
    out = _inject_markers_into_excerpt(text, 0, 10, 2, 5, "PRE", "SUF")
    assert out == "PRE01[SELECTION_START]234[SELECTION_END]56789SUF"

    # Excerpt covers 10-20, selection is 5-8 (outside excerpt)
    out = _inject_markers_into_excerpt(text, 10, 20, 5, 8, "PRE", "SUF")
    assert out == "PRE0123456789SUF"

    # Selection partially outside start
    out = _inject_markers_into_excerpt(text, 5, 15, 2, 8, "PRE", "SUF")
    assert out == "PRE[SELECTION_START]012[SELECTION_END]3456789SUF"
