"""Unit tests for Writer chat document context excerpt helpers."""

from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

from unittest.mock import MagicMock, patch

from plugin.doc.document_helpers import (
    _inject_markers_into_excerpt,
    _read_writer_text_slice,
    _writer_selection_overlaps_windows,
    get_document_context_for_chat,
)
from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS


@patch("plugin.doc.document_helpers.get_string_without_tracked_deletions", return_value="visible text")
@patch("plugin.doc.document_helpers.get_text_cursor_at_range")
def test_read_writer_text_slice_uses_deletion_filter(mock_get_cursor, mock_without_deletions):
    cursor = MagicMock()
    mock_get_cursor.return_value = cursor

    result = _read_writer_text_slice(MagicMock(), 0, 100)

    mock_without_deletions.assert_called_once_with(cursor)
    assert result == "visible text"
    cursor.getString.assert_not_called()


def test_inject_markers_into_excerpt_selection_inside():
    excerpt = "abcdefghij"
    out = _inject_markers_into_excerpt(excerpt, 0, 10, 2, 5, "[START]\n", "\n[END]")
    assert out == "[START]\nab[SELECTION_START]cde[SELECTION_END]fghij\n[END]"


def test_inject_markers_no_overlap():
    excerpt = "abcdefghij"
    out = _inject_markers_into_excerpt(excerpt, 0, 10, 20, 25, "[START]\n", "\n[END]")
    assert out == "[START]\nabcdefghij\n[END]"


@patch("plugin.doc.document_helpers._read_writer_text_slice")
@patch("plugin.doc.document_helpers._writer_char_count", return_value=20000)
@patch("plugin.doc.document_helpers.get_document_type")
def test_get_document_context_reads_slices_not_full_doc(mock_doc_type, mock_char_count, mock_read_slice):
    from plugin.doc.document_helpers import DocumentType

    mock_doc_type.return_value = DocumentType.WRITER
    mock_read_slice.side_effect = lambda _model, start, length: f"slice:{start}:{length}"

    get_document_context_for_chat(MagicMock(), max_context=8000, include_end=True, include_selection=False)

    assert mock_read_slice.call_count == 2
    assert mock_read_slice.call_args_list[0][0][1:] == (0, 4000)
    assert mock_read_slice.call_args_list[1][0][1:] == (16000, 4000)


@patch("plugin.doc.document_helpers._read_writer_text_slice")
@patch("plugin.doc.document_helpers._writer_char_count", return_value=100)
@patch("plugin.doc.document_helpers.get_document_type")
def test_get_document_context_short_doc_single_slice(mock_doc_type, mock_char_count, mock_read_slice):
    from plugin.doc.document_helpers import DocumentType

    mock_doc_type.return_value = DocumentType.WRITER
    mock_read_slice.return_value = "short doc text"
    model = MagicMock()

    ctx = get_document_context_for_chat(model, max_context=8000, include_end=True, include_selection=False)

    assert "short doc text" in ctx
    mock_read_slice.assert_called_once_with(model, 0, 100)


def test_chat_document_context_max_chars_default():
    assert CHAT_DOCUMENT_CONTEXT_MAX_CHARS == 8000


def test_settings_field_specs_omit_chat_context_length():
    from plugin.chatbot.settings_dialog import get_settings_field_specs

    names = {f["name"] for f in get_settings_field_specs(MagicMock())}
    assert "chat_context_length" not in names


def test_writer_selection_overlaps_windows_true_when_ranges_overlap():
    model = MagicMock()
    text = MagicMock()
    model.getText.return_value = text
    exc_cursor = MagicMock()
    exc_cursor.getStart.return_value = "exc_start"
    exc_cursor.getEnd.return_value = "exc_end"
    text.compareRegionStarts.return_value = -1

    with patch("plugin.doc.document_helpers.get_text_cursor_at_range", return_value=exc_cursor):
        assert _writer_selection_overlaps_windows(model, [(0, 4000)], "sel_start", "sel_end") is True


def test_writer_selection_overlaps_windows_false_when_before_excerpt():
    model = MagicMock()
    text = MagicMock()
    model.getText.return_value = text
    exc_cursor = MagicMock()
    exc_cursor.getStart.return_value = "exc_start"
    exc_cursor.getEnd.return_value = "exc_end"
    text.compareRegionStarts.side_effect = lambda a, b: 1 if a == "sel_end" and b == "exc_start" else 0

    with patch("plugin.doc.document_helpers.get_text_cursor_at_range", return_value=exc_cursor):
        assert _writer_selection_overlaps_windows(model, [(100, 200)], "sel_start", "sel_end") is False
