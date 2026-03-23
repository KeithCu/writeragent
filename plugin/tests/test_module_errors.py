import pytest
from unittest.mock import MagicMock, patch
import sys

# Mock UNO modules before importing plugin code
sys.modules['uno'] = MagicMock()
sys.modules['unohelper'] = MagicMock()
sys.modules['com.sun.star.beans'] = MagicMock()
class MockBase: pass
sys.modules['unohelper'].Base = MockBase


from plugin.framework.errors import WriterError
from plugin.modules.writer.ops import (
    find_paragraph_for_range,
    get_selection_range,
    insert_html_at_cursor,
    get_text_cursor_at_range
)


class TestWriterModuleErrors:

    def test_find_paragraph_for_range_null_anchor(self):
        with pytest.raises(WriterError) as exc_info:
            find_paragraph_for_range(None, [MagicMock()], MagicMock())
        assert exc_info.value.code == "WRITER_ANCHOR_NULL"

    def test_find_paragraph_for_range_empty_ranges(self):
        with pytest.raises(WriterError) as exc_info:
            find_paragraph_for_range(MagicMock(), [], MagicMock())
        assert exc_info.value.code == "WRITER_PARA_RANGES_EMPTY"

    def test_find_paragraph_for_range_null_text_obj(self):
        with pytest.raises(WriterError) as exc_info:
            find_paragraph_for_range(MagicMock(), [MagicMock()], None)
        assert exc_info.value.code == "WRITER_TEXT_OBJ_NULL"

    def test_get_selection_range_null_model(self):
        with pytest.raises(WriterError) as exc_info:
            get_selection_range(None)
        assert exc_info.value.code == "WRITER_MODEL_NULL"

    def test_get_selection_range_null_controller(self):
        mock_model = MagicMock()
        mock_model.getCurrentController.return_value = None
        with pytest.raises(WriterError) as exc_info:
            get_selection_range(mock_model)
        assert exc_info.value.code == "WRITER_CONTROLLER_NULL"

    def test_insert_html_at_cursor_null_cursor(self):
        with pytest.raises(WriterError) as exc_info:
            insert_html_at_cursor(None, "<p>Test</p>")
        assert exc_info.value.code == "WRITER_CURSOR_NULL"

    def test_insert_html_at_cursor_empty_content(self):
        with pytest.raises(WriterError) as exc_info:
            insert_html_at_cursor(MagicMock(), "")
        assert exc_info.value.code == "WRITER_HTML_CONTENT_EMPTY"

    def test_get_text_cursor_at_range_null_model(self):
        with pytest.raises(WriterError) as exc_info:
            get_text_cursor_at_range(None, 0, 10)
        assert exc_info.value.code == "WRITER_MODEL_NULL"

    def test_get_text_cursor_at_range_null_offsets(self):
        with pytest.raises(WriterError) as exc_info:
            get_text_cursor_at_range(MagicMock(), None, 10)
        assert exc_info.value.code == "WRITER_INVALID_OFFSETS"

        with pytest.raises(WriterError) as exc_info:
            get_text_cursor_at_range(MagicMock(), 0, None)
        assert exc_info.value.code == "WRITER_INVALID_OFFSETS"

    @patch("plugin.modules.writer.ops._doc_length", return_value=100)
    def test_get_text_cursor_at_range_null_text_obj(self, mock_doc_len):
        mock_model = MagicMock()
        mock_model.getText.return_value = None
        with pytest.raises(WriterError) as exc_info:
            get_text_cursor_at_range(mock_model, 0, 10)
        assert exc_info.value.code == "WRITER_TEXT_NOT_FOUND"
