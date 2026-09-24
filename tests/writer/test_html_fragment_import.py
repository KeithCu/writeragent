from contextlib import contextmanager
from unittest.mock import MagicMock, patch


from plugin.writer.format import (  # noqa: E402
    HTML_FILTER,
    _content_has_block_markup,
    insert_html_fragment_at_cursor,
)
from plugin.writer.html_import import (
    _EXPORTED_FIELD_TITLES,
    _FIELD_TITLE_TO_SERVICE,
    _insert_restored_field,
    _restore_field_placeholders,
    _wrap_html_fragment,
    rewrite_exported_field_spans,
)


@contextmanager
def _capture_temp_buffer(content_holder):
    @contextmanager
    def fake_with_temp_buffer(content, config_svc=None):
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
    cursor = MagicMock()
    end_cursor = MagicMock()
    model = MagicMock()
    text = MagicMock()
    model.getText.return_value = text
    text.createTextCursor.return_value = end_cursor

    with _capture_temp_buffer({}):
        insert_html_fragment_at_cursor(cursor, "<p>Hi</p>", model=model)

    end_cursor.gotoEnd.assert_called_once_with(False)
    cursor.gotoRange.assert_called_once_with(end_cursor.getStart(), False)


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
    def fake_with_temp_buffer(content, config_svc=None):
        yield ("/tmp/fake.html", "file:///tmp/fake.html")

    def capture_insert(url, props):
        filter_holder["props"] = props

    cursor.insertDocumentFromURL.side_effect = capture_insert

    with patch("plugin.writer.format._with_temp_buffer", side_effect=fake_with_temp_buffer):
        insert_html_fragment_at_cursor(cursor, "<p>Hi</p>")

    assert filter_holder["props"][0].Name == "FilterName"
    assert filter_holder["props"][0].Value == HTML_FILTER


def test_rewrite_exported_field_spans_matches_body_xhtml():
    """Body/header XHTML emits titled spans; import must see a token, not a dropped tag."""
    html = (
        '<p>Confidential | <span title="page-number"/> | '
        '<span title="page-count">A</span> '
        '<span title="time">17:41:00</span></p>'
    )
    out = rewrite_exported_field_spans(html)
    assert "[[WA-FIELD:page-number]]" in out
    assert "[[WA-FIELD:page-count]]" in out
    assert "[[WA-FIELD:time]]" in out
    assert "title=\"page-number\"" not in out


def test_rewrite_exported_field_spans_leaves_plain_html():
    assert rewrite_exported_field_spans("<p>Hello</p>") == "<p>Hello</p>"
    assert rewrite_exported_field_spans("") == ""


def test_rewrite_exported_field_spans_letterhead_subset():
    """Chapter / author / file / DocInfo titles become the same placeholder tokens."""
    html = (
        '<p><span title="chapter"/> '
        '<span title="author-name">Ada</span> '
        '<span title="author-initials">AL</span> '
        '<span title="file-name"/> '
        '<span title="title"/> '
        '<span title="subject"/></p>'
    )
    out = rewrite_exported_field_spans(html)
    for title in (
        "chapter", "author-name", "author-initials", "file-name", "title", "subject",
    ):
        assert "[[WA-FIELD:%s]]" % title in out
        assert 'title="%s"' % title not in out


def test_rewrite_exported_field_spans_leaves_unmapped_titles():
    """Titles outside the restore subset stay as spans (easy to widen later)."""
    html = '<p><span title="word-count"/> <span title="keywords"/></p>'
    assert rewrite_exported_field_spans(html) == html


def _mock_field_range():
    field = MagicMock()
    model = MagicMock()
    model.createInstance.return_value = field
    text = MagicMock()
    cursor = MagicMock()
    text.createTextCursorByRange.return_value = cursor
    text_range = MagicMock()
    text_range.getText.return_value = text
    return model, text_range, field, text


def test_insert_restored_field_letterhead_services():
    """Restore maps the new titles to the matching UNO services (defaults OK)."""
    expected = {
        "chapter": "com.sun.star.text.textfield.Chapter",
        "file-name": "com.sun.star.text.textfield.FileName",
        "title": "com.sun.star.text.textfield.docinfo.Title",
        "subject": "com.sun.star.text.textfield.docinfo.Subject",
    }
    for title, service in expected.items():
        model, text_range, field, text = _mock_field_range()
        assert _insert_restored_field(model, text_range, title) is True
        model.createInstance.assert_called_once_with(service)
        text.insertTextContent.assert_called_once()
        field.setPropertyValue.assert_not_called()


def test_insert_restored_field_author_fullname_flag():
    """author-name and author-initials share Author; FullName selects the form."""
    model, text_range, field, text = _mock_field_range()
    assert _insert_restored_field(model, text_range, "author-name") is True
    model.createInstance.assert_called_once_with("com.sun.star.text.textfield.Author")
    field.setPropertyValue.assert_any_call("FullName", True)

    model, text_range, field, text = _mock_field_range()
    assert _insert_restored_field(model, text_range, "author-initials") is True
    model.createInstance.assert_called_once_with("com.sun.star.text.textfield.Author")
    field.setPropertyValue.assert_any_call("FullName", False)


def test_restore_field_placeholders_creates_mapped_services():
    """Placeholder search walks every mapped title and inserts that service."""
    created = []

    def create_instance(svc):
        created.append(svc)
        return MagicMock()

    def find_first(sd):
        rng = MagicMock()
        text = MagicMock()
        text.createTextCursorByRange.return_value = MagicMock()
        rng.getText.return_value = text
        rng.getEnd.return_value = MagicMock()
        return rng

    model = MagicMock()
    model.createInstance.side_effect = create_instance
    model.findFirst.side_effect = find_first
    model.findNext.return_value = None

    restored = _restore_field_placeholders(model)
    assert restored == len(_EXPORTED_FIELD_TITLES)
    assert set(created) == set(_FIELD_TITLE_TO_SERVICE.values())
    # Author is shared by name + initials; DocInfo title/subject must both land.
    assert created.count("com.sun.star.text.textfield.Author") == 2
    assert "com.sun.star.text.textfield.Chapter" in created
    assert "com.sun.star.text.textfield.FileName" in created
    assert "com.sun.star.text.textfield.docinfo.Title" in created
    assert "com.sun.star.text.textfield.docinfo.Subject" in created


def test_restore_field_placeholders_scopes_region_with_uno_same():
    """Header field restore must use UNO identity, not bare ``==`` on XText wrappers."""
    from plugin.writer import html_import as hi

    header = object()
    found = MagicMock()
    found.getText.return_value = object()
    found.getEnd.return_value = MagicMock()
    model = MagicMock()
    model.createSearchDescriptor.return_value = MagicMock()
    model.findFirst.return_value = found
    model.findNext.return_value = None

    with (
        patch.object(hi, "uno_same", return_value=True),
        patch.object(hi, "_insert_restored_field", return_value=True) as insert,
    ):
        assert hi._restore_field_placeholders(model, header) >= 1
        assert insert.called

    with (
        patch.object(hi, "uno_same", return_value=False),
        patch.object(hi, "_insert_restored_field", return_value=True) as insert,
    ):
        assert hi._restore_field_placeholders(model, header) == 0
        insert.assert_not_called()
