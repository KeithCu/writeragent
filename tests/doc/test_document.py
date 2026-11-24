"""Headless unit tests for document excerpt helpers (not a native UNO suite)."""

from plugin.doc.document_helpers import _inject_selection_markers


def test_inject_markers_into_excerpt():
    text = "0123456789"
    # Excerpt covers 0-10, selection is 2-5
    out = _inject_selection_markers(text, 0, 2, 5, "PRE", "SUF")
    assert out == "PRE01[SELECTION_START]234[SELECTION_END]56789SUF"

    # Excerpt covers 10-20, selection is 5-8 (outside excerpt)
    out = _inject_selection_markers(text, 10, 5, 8, "PRE", "SUF")
    assert out == "PRE0123456789SUF"
