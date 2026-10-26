from contextlib import contextmanager
from unittest.mock import MagicMock, patch


from plugin.writer.format import (  # noqa: E402
    HTML_FILTER,
    _content_has_block_markup,
    insert_html_fragment_at_cursor,
)
from plugin.writer.html_import import (
    _wrap_html_fragment,
)


@contextmanager
def _capture_temp_buffer(content_holder):
    @contextmanager
    def fake_with_temp_buffer(content=None, ext=None):
        content_holder["content"] = content
        yield ("/tmp/fake.html", "file:///tmp/fake.html")

    with patch("plugin.writer.format._with_temp_buffer", side_effect=fake_with_temp_buffer):
        yield


def test_wrap_html_fragment_adds_doctype_and_body():
    wrapped = _wrap_html_fragment("<p>Hi</p>")
    assert "<!DOCTYPE html>" in wrapped
    assert "<body>" in wrapped
    assert "<p>Hi</p>" in wrapped


def test_wrap_html_fragment_extra_css_in_head():
    css = "ul, ol { margin-left: 0.2cm; }"
    wrapped = _wrap_html_fragment("<p>Hi</p>", extra_css=css)
    assert "<style>%s</style>" % css in wrapped
    assert "<meta charset=\"UTF-8\">" in wrapped


def test_wrap_html_fragment_skips_full_document():
    full = "<html><head></head><body><p>Hi</p></body></html>"
    assert _wrap_html_fragment(full, extra_css="ignored") == full


def test_wraps_bare_fragment():
    cursor = MagicMock()
    holder = {}
    with _capture_temp_buffer(holder):
        insert_html_fragment_at_cursor(cursor, "<p>Hi</p>")
    assert "<!DOCTYPE html>" in holder["content"]
    assert "<p>Hi</p>" in holder["content"]
    cursor.insertDocumentFromURL.assert_called_once()


def test_extra_css_in_head():
    cursor = MagicMock()
    holder = {}
    css = "ul, ol { margin-left: 0.2cm; padding-left: 0.3cm; }"
    with _capture_temp_buffer(holder):
        insert_html_fragment_at_cursor(cursor, "<ul><li>x</li></ul>", extra_css=css)
    assert css in holder["content"]
    assert "<style>" in holder["content"]


def test_prewrapped_skips_rewrap():
    cursor = MagicMock()
    holder = {}
    prewrapped = "<html><body><b>Bold</b></body></html>"
    with _capture_temp_buffer(holder):
        insert_html_fragment_at_cursor(cursor, prewrapped, wrap=False)
    assert holder["content"] == prewrapped


def test_advances_cursor_when_model_given():
    """The cursor continues right after the imported content, in its own text. Jumping to the
    BODY end sent later segments of a mid-document edit to the end (#46/#47) and raised
    RuntimeException for a cursor in a frame or cell (#52)."""
    cursor = MagicMock()
    model = MagicMock()
    parked = cursor.getText.return_value.createTextCursorByRange.return_value

    with _capture_temp_buffer({}):
        insert_html_fragment_at_cursor(cursor, "<p>Hi</p>", model=model)

    cursor.getText.return_value.createTextCursorByRange.assert_called_once_with(cursor.getEnd())
    cursor.gotoRange.assert_called_once_with(parked.getStart(), False)
    model.getText.assert_not_called()


def test_content_has_block_markup_inline_span():
    assert _content_has_block_markup('<span style="background: transparent">Title</span>') is False
    assert _content_has_block_markup("<b>Title</b>") is False
    assert _content_has_block_markup("") is False
    assert _content_has_block_markup(None) is False


def test_content_has_block_markup_block_tags():
    assert _content_has_block_markup("<p>x</p>") is True
    assert _content_has_block_markup("<h3>x</h3>") is True
    assert _content_has_block_markup("<ul><li>x</li></ul>") is True
    assert _content_has_block_markup("<P>X</P>") is True


def test_filter_name_starwriter():
    cursor = MagicMock()
    filter_holder = {}

    @contextmanager
    def fake_with_temp_buffer(content=None, ext=None):
        yield ("/tmp/fake.html", "file:///tmp/fake.html")

    def capture_insert(url, props):
        filter_holder["props"] = props

    cursor.insertDocumentFromURL.side_effect = capture_insert

    with patch("plugin.writer.format._with_temp_buffer", side_effect=fake_with_temp_buffer):
        insert_html_fragment_at_cursor(cursor, "<p>Hi</p>")

    assert filter_holder["props"][0].Name == "FilterName"
    assert filter_holder["props"][0].Value == HTML_FILTER


def test_replace_full_document_and_insert_content_call_restore_with_text_obj():
    from plugin.writer.html_import import insert_content_at_position, replace_full_document

    doc = MagicMock()
    body_text = MagicMock()
    doc.getText.return_value = body_text
    cur = MagicMock()
    body_text.createTextCursor.return_value = cur
    cur.getText.return_value = body_text

    with (
        patch("plugin.writer.html_import._insert_starwriter_html_at_cursor"),
        patch("plugin.writer.field_restore._restore_field_placeholders") as mock_restore,
    ):
        replace_full_document(doc, MagicMock(), "<p>Page <span title='page-number'/></p>")
        mock_restore.assert_called_once()
        assert mock_restore.call_args[0][0] is doc
        assert mock_restore.call_args[0][1] is body_text
        assert mock_restore.call_args[1]["nonce"] is not None

    with (
        patch("plugin.writer.html_import._insert_starwriter_html_at_cursor"),
        patch("plugin.writer.field_restore._restore_field_placeholders") as mock_restore,
    ):
        insert_content_at_position(doc, MagicMock(), "<p>Page <span title='page-number'/></p>", "beginning")
        mock_restore.assert_called_once()
        assert mock_restore.call_args[0][0] is doc
        assert mock_restore.call_args[0][1] is body_text
        assert mock_restore.call_args[1]["nonce"] is not None
