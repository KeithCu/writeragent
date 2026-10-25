from unittest.mock import MagicMock, patch
import pytest

from plugin.framework.errors import DocumentDisposedError, ToolExecutionError
from plugin.writer.field_restore import (
    _EXPORTED_FIELD_TITLES,
    _FIELD_TITLE_TO_SERVICE,
    _insert_restored_field,
    _restore_field_placeholders,
    rewrite_exported_field_spans,
)


def test_rewrite_exported_field_spans_matches_body_xhtml():
    """Body/header XHTML emits titled spans; import must see a token, not a dropped tag."""
    html = (
        '<p>Confidential | <span title="page-number"/> | '
        '<span title="page-count">A</span> '
        '<span title="time">17:41:00</span></p>'
    )
    out = rewrite_exported_field_spans(html, nonce="test1234")
    assert "[[WA-FIELD:test1234:page-number]]" in out
    assert "[[WA-FIELD:test1234:page-count]]" in out
    assert "[[WA-FIELD:test1234:time]]" in out
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
    out = rewrite_exported_field_spans(html, nonce="test1234")
    for title in (
        "chapter", "author-name", "author-initials", "file-name", "title", "subject",
    ):
        assert "[[WA-FIELD:test1234:%s]]" % title in out
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

    titles = list(_EXPORTED_FIELD_TITLES)
    sd_captured = []

    def make_hit(title):
        rng = MagicMock()
        text = MagicMock()
        text.createTextCursorByRange.return_value = MagicMock()
        rng.getText.return_value = text
        rng.getEnd.return_value = MagicMock()
        rng.getString.return_value = f"[[WA-FIELD:nonce:{title}]]"
        return rng

    hits = [make_hit(t) for t in titles]

    def find_first(sd):
        sd_captured.append(sd.SearchString)
        return hits[0]

    def find_next(end, sd):
        hits.pop(0)
        return hits[0] if hits else None

    model = MagicMock()
    model.createInstance.side_effect = create_instance
    model.findFirst.side_effect = find_first
    model.findNext.side_effect = find_next

    restored = _restore_field_placeholders(model)
    assert restored == len(_EXPORTED_FIELD_TITLES)
    model.findFirst.assert_called_once()
    assert all(title in sd_captured[0] for title in _EXPORTED_FIELD_TITLES)
    assert set(created) == set(_FIELD_TITLE_TO_SERVICE.values())
    # Author is shared by name + initials; DocInfo title/subject must both land.
    assert created.count("com.sun.star.text.textfield.Author") == 2
    assert "com.sun.star.text.textfield.Chapter" in created
    assert "com.sun.star.text.textfield.FileName" in created
    assert "com.sun.star.text.textfield.docinfo.Title" in created
    assert "com.sun.star.text.textfield.docinfo.Subject" in created


def test_restore_field_placeholders_scopes_region_with_uno_same():
    """Header field restore must use UNO identity, not bare ``==`` on XText wrappers."""
    from plugin.writer import field_restore as fr

    header = object()
    found = MagicMock()
    found.getText.return_value = object()
    found.getEnd.return_value = MagicMock()
    found.getString.return_value = "[[WA-FIELD:nonce:page-number]]"
    model = MagicMock()
    model.createSearchDescriptor.return_value = MagicMock()
    model.findFirst.return_value = found
    model.findNext.return_value = None

    with (
        patch.object(fr, "uno_same", return_value=True),
        patch.object(fr, "_insert_restored_field", return_value=True) as insert,
    ):
        assert fr._restore_field_placeholders(model, header) >= 1
        assert insert.called

    with (
        patch.object(fr, "uno_same", return_value=False),
        patch.object(fr, "_insert_restored_field", return_value=True) as insert,
    ):
        assert fr._restore_field_placeholders(model, header) == 0
        insert.assert_not_called()


def test_restore_fields_by_cursor_single_walk_last_token_first():
    """Three tokens across two portions: one createEnumeration walk, no findFirst, inserted in reverse."""
    nonce = "test1234"
    tok1 = f"[[WA-FIELD:{nonce}:page-number]]"
    tok2 = f"[[WA-FIELD:{nonce}:time]]"
    tok3 = f"[[WA-FIELD:{nonce}:date]]"

    portion1_str = f"Page {tok1} and at {tok2} "
    portion2_str = f"on {tok3} end"
    full_str = portion1_str + portion2_str

    class _Portion:
        def __init__(self, s, start_val):
            self.s = s
            self._start = start_val

        def getPropertyValue(self, name):
            assert name == "TextPortionType"
            return "Text"

        def getString(self):
            return self.s

        def getStart(self):
            return self._start

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    class _Para:
        def createEnumeration(self):
            return _Enum([_Portion(portion1_str, "p1"), _Portion(portion2_str, "p2")])

    class _Text:
        def __init__(self):
            self.plain = full_str
            self.enum_calls = 0

        def getString(self):
            return self.plain

        def createEnumeration(self):
            self.enum_calls += 1
            return _Enum([_Para()])

    text = _Text()

    cursor_for_tok1 = MagicMock()
    cursor_for_tok1.getString.return_value = tok1
    cursor_for_tok2 = MagicMock()
    cursor_for_tok2.getString.return_value = tok2
    cursor_for_tok3 = MagicMock()
    cursor_for_tok3.getString.return_value = tok3

    text_cursors = [cursor_for_tok1, cursor_for_tok2, cursor_for_tok3]
    text.createTextCursorByRange = lambda start: text_cursors.pop(0)

    model = MagicMock()
    model.findFirst.side_effect = AssertionError("findFirst should not run")

    inserted_titles = []

    def _inserted(unused_model, cursor, title, *args):
        inserted_titles.append(title)
        text.plain = text.plain.replace(f"[[WA-FIELD:{nonce}:{title}]]", title)
        return True

    with patch("plugin.writer.field_restore._insert_restored_field", side_effect=_inserted):
        restored = _restore_field_placeholders(model, text, nonce=nonce)

    assert restored == 3
    model.findFirst.assert_not_called()
    assert inserted_titles == ["date", "time", "page-number"]


def test_restore_fields_in_region_without_document_search():
    """A footer on an unused page style is invisible to findFirst. Walk that XText."""
    nonce = "test1234"
    needle = f"[[WA-FIELD:{nonce}:page-number]]"

    class _Portion:
        def getPropertyValue(self, name):
            assert name == "TextPortionType"
            return "Text"

        def getString(self):
            return text.plain

        def getStart(self):
            return "start_sentinel"

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    class _Para:
        def createEnumeration(self):
            return _Enum([_Portion()])

    class _Text:
        def __init__(self):
            self.plain = "Annex " + needle

        def getString(self):
            return self.plain

        def createEnumeration(self):
            return _Enum([_Para()])

        def createTextCursorByRange(self, start):
            cursor = MagicMock()
            cursor.goRight.return_value = True
            cursor.getString.return_value = needle
            return cursor

    text = _Text()
    model = MagicMock()
    model.findFirst.side_effect = AssertionError("findFirst should not run")

    def _inserted(unused_model, unused_cursor, title, *args):
        assert title == "page-number"
        text.plain = "Annex 1"
        return True

    with patch("plugin.writer.field_restore._insert_restored_field", side_effect=_inserted):
        restored = _restore_field_placeholders(model, text, nonce=nonce)

    assert restored == 1
    model.findFirst.assert_not_called()

    # Region with no token: findFirst is not called.
    clean_text = _Text()
    clean_text.plain = "Clean region without token"
    assert _restore_field_placeholders(model, clean_text, nonce=nonce) == 0
    model.findFirst.assert_not_called()


def test_restore_fields_in_region_two_paragraphs_advances_past_break():
    nonce = "test1234"
    needle = f"[[WA-FIELD:{nonce}:page-number]]"
    para1_text = "First paragraph"
    para2_text = needle

    class _Portion:
        def __init__(self, s):
            self.s = s

        def getPropertyValue(self, name):
            assert name == "TextPortionType"
            return "Text"

        def getString(self):
            return self.s

        def getStart(self):
            return "portion_start"

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    class _Para:
        def __init__(self, s):
            self.s = s

        def createEnumeration(self):
            return _Enum([_Portion(self.s)])

    go_right_calls = []

    class _Text:
        def __init__(self):
            self.plain = para1_text + "\n" + para2_text

        def getString(self):
            return self.plain

        def createEnumeration(self):
            return _Enum([_Para(para1_text), _Para(para2_text)])

        def createTextCursorByRange(self, start):
            cursor = MagicMock()
            def _go_right(count, select):
                go_right_calls.append((count, select))
                return True
            cursor.goRight.side_effect = _go_right
            cursor.getString.return_value = needle
            return cursor

    text = _Text()
    model = MagicMock()
    model.findFirst.side_effect = AssertionError("findFirst should not run")

    def _inserted(unused_model, unused_cursor, title, *args):
        assert title == "page-number"
        text.plain = para1_text + "\n1"
        return True

    with patch("plugin.writer.field_restore._insert_restored_field", side_effect=_inserted):
        restored = _restore_field_placeholders(model, text, nonce=nonce)

    assert restored == 1
    # goRight is portion-local: m.start() (0) then len(needle)
    assert go_right_calls[0] == (0, False)
    assert go_right_calls[1] == (len(needle), True)


def test_restore_fields_by_cursor_two_tokens_one_portion_reverse_order():
    """Two tokens in a single portion are inserted last-first."""
    from plugin.writer.field_restore import _restore_fields_by_cursor

    nonce = "test1234"
    tok1 = f"[[WA-FIELD:{nonce}:page-number]]"
    tok2 = f"[[WA-FIELD:{nonce}:date]]"
    chunk = f"Start {tok1} middle {tok2} end"

    class _Portion:
        def getPropertyValue(self, name):
            assert name == "TextPortionType"
            return "Text"

        def getString(self):
            return chunk

        def getStart(self):
            return "pstart"

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    class _Para:
        def createEnumeration(self):
            return _Enum([_Portion()])

    cursor1 = MagicMock()
    cursor1.getString.return_value = tok1
    cursor2 = MagicMock()
    cursor2.getString.return_value = tok2
    cursors = [cursor1, cursor2]

    class _Text:
        def getString(self):
            return chunk

        def createEnumeration(self):
            return _Enum([_Para()])

        def createTextCursorByRange(self, start):
            return cursors.pop(0)

    text = _Text()
    model = MagicMock()
    inserted = []

    def fake_insert(unused_model, cur, title, props=None):
        inserted.append(title)
        return True

    with patch("plugin.writer.field_restore._insert_restored_field", side_effect=fake_insert):
        restored = _restore_fields_by_cursor(model, text, nonce=nonce)

    assert restored == 2
    assert inserted == ["date", "page-number"]


def test_restore_field_placeholders_falls_through_to_find_first_if_token_remains():
    nonce = "test1234"
    needle = f"[[WA-FIELD:{nonce}:page-number]]"
    text = MagicMock()
    text.getString.return_value = "Unresolved " + needle

    model = MagicMock()
    found = MagicMock()
    found.getString.return_value = needle
    found.getText.return_value = text
    found.getEnd.return_value = MagicMock()
    model.findFirst.return_value = found
    model.findNext.return_value = None

    def _clear_token(*_args, **_kwargs):
        text.getString.return_value = "Resolved"
        return True

    with (
        patch("plugin.writer.field_restore._restore_fields_by_cursor", return_value=1),
        patch("plugin.writer.field_restore.uno_same", return_value=True),
        patch("plugin.writer.field_restore._insert_restored_field", side_effect=_clear_token) as insert,
    ):
        restored = _restore_field_placeholders(model, text, nonce=nonce)

    assert restored >= 2
    assert model.findFirst.called
    assert insert.called


def test_restore_field_placeholders_raises_when_token_remains_after_find_first():
    nonce = "test1234"
    needle = f"[[WA-FIELD:{nonce}:page-number]]"
    text = MagicMock()
    text.getString.return_value = "Unresolved " + needle

    model = MagicMock()
    model.findFirst.return_value = None

    with (
        patch("plugin.writer.field_restore._restore_fields_by_cursor", return_value=1),
        pytest.raises(ToolExecutionError, match=r"\[\[WA-FIELD:test1234:page-number\]\]"),
    ):
        _restore_field_placeholders(model, text, nonce=nonce)
    assert text.getString() == "Unresolved " + needle


def test_rewrite_exported_field_spans_keeps_page_offset():
    html = '<p><span title="page-number" data-offset="-2" data-subtype="PREV"/></p>'
    out = rewrite_exported_field_spans(html, nonce="test1234")
    assert out.count("[[WA-FIELD:test1234:page-number|subtype=PREV|offset=-2]]") == 1


def test_rewrite_exported_field_spans_nested_titled_span():
    html = '<p><span title="date"><span style="z">a</span>b</span></p>'
    out = rewrite_exported_field_spans(html, nonce="test1234")
    assert out == '<p>[[WA-FIELD:test1234:date]]</p>'


def test_rewrite_exported_field_spans_uppercase():
    html = '<P><SPAN TITLE="date"/></P>'
    out = rewrite_exported_field_spans(html, nonce="test1234")
    assert '[[WA-FIELD:test1234:date]]' in out
    assert 'TITLE="date"' not in out


def test_rewrite_exported_field_spans_attr_with_greater_than():
    html = '<p><span title="page-number" data-x="a>b" data-subtype="PREV"/></p>'
    out = rewrite_exported_field_spans(html, nonce="test1234")
    assert out.count('[[WA-FIELD:test1234:page-number|subtype=PREV]]') == 1


def test_rewrite_exported_field_spans_prose_title_no_span():
    prose = 'The title="date" attribute'
    assert rewrite_exported_field_spans(prose, nonce="test1234") == prose


def test_rewrite_exported_field_spans_unclosed_span():
    unclosed = '<p><span title="date">x</p>'
    assert rewrite_exported_field_spans(unclosed, nonce="test1234") == unclosed


def test_insert_restored_field_unknown_title_raises():
    model, text_range, _field, _text = _mock_field_range()
    with pytest.raises(ToolExecutionError, match="not-a-field"):
        _insert_restored_field(model, text_range, "not-a-field")
    model.createInstance.assert_not_called()


def test_insert_restored_field_create_failure_raises():
    model, text_range, _field, text = _mock_field_range()
    model.createInstance.side_effect = RuntimeError("no service")
    with pytest.raises(ToolExecutionError, match="page-number"):
        _insert_restored_field(model, text_range, "page-number")
    text.createTextCursorByRange.assert_not_called()


def test_insert_restored_field_disposal_is_not_a_tool_error():
    model, text_range, _field, text = _mock_field_range()
    model.createInstance.side_effect = type("DisposedException", (Exception,), {})("gone")
    with pytest.raises(DocumentDisposedError):
        _insert_restored_field(model, text_range, "date")
    text.insertTextContent.assert_not_called()


def test_insert_restored_field_date_property_failure_does_not_insert():
    model, text_range, field, text = _mock_field_range()
    field.setPropertyValue.side_effect = RuntimeError("no IsDate")
    with pytest.raises(ToolExecutionError, match="date"):
        _insert_restored_field(model, text_range, "date")
    text.insertTextContent.assert_not_called()


def test_insert_restored_field_page_number_subtype_failure_does_not_insert():
    model, text_range, field, text = _mock_field_range()
    field.setPropertyValue.side_effect = RuntimeError("SubType failed")
    with pytest.raises(ToolExecutionError, match="page-number"):
        _insert_restored_field(model, text_range, "page-number")
    text.insertTextContent.assert_not_called()


def test_insert_restored_field_property_disposal_raises_document_disposed_error():
    model, text_range, field, text = _mock_field_range()
    field.setPropertyValue.side_effect = type("DisposedException", (Exception,), {})("disposed")
    with pytest.raises(DocumentDisposedError):
        _insert_restored_field(model, text_range, "date")
    text.insertTextContent.assert_not_called()


def test_insert_restored_field_puts_placeholder_back_when_insert_fails():
    model, text_range, _field, text = _mock_field_range()
    cursor = text.createTextCursorByRange.return_value
    cursor.getString.return_value = "[[WA-FIELD:date]]"
    text.insertTextContent.side_effect = RuntimeError("insert failed")
    with pytest.raises(ToolExecutionError, match="date"):
        _insert_restored_field(model, text_range, "date")
    assert cursor.setString.call_args_list[0].args == ("",)
    assert cursor.setString.call_args_list[-1].args == ("[[WA-FIELD:date]]",)


def test_restore_field_placeholders_search_error_propagates():
    model = MagicMock()
    model.createSearchDescriptor.side_effect = RuntimeError("no search")
    with pytest.raises(RuntimeError, match="no search"):
        _restore_field_placeholders(model)


def test_restore_field_placeholders_find_error_propagates():
    model = MagicMock()
    model.findFirst.side_effect = RuntimeError("find failed")
    with pytest.raises(RuntimeError, match="find failed"):
        _restore_field_placeholders(model)


def test_restore_field_placeholders_ignores_token_with_different_or_missing_nonce():
    """A body string that already contains [[WA-FIELD:page-number]] is left alone when nonce differs."""
    text = MagicMock()
    text.getString.return_value = "Existing [[WA-FIELD:page-number]] and [[WA-FIELD:other:page-number]]"
    model = MagicMock()
    model.createSearchDescriptor.return_value = MagicMock()
    model.findFirst.return_value = None

    with patch("plugin.writer.field_restore._insert_restored_field") as mock_insert:
        restored = _restore_field_placeholders(model, text, nonce="thiscall")

    assert restored == 0
    mock_insert.assert_not_called()


def test_restore_fields_by_cursor_single_walk_reverse_order():
    """Restore multiple field tokens in a single portion enumeration walk in reverse order."""
    from plugin.writer.field_restore import _restore_fields_by_cursor

    nonce = "test1234"
    tok1 = f"[[WA-FIELD:{nonce}:page-number]]"
    tok2 = f"[[WA-FIELD:{nonce}:date]]"
    full_str = f"Page {tok1} on {tok2}"

    class _Portion:
        def __init__(self, s):
            self.s = s

        def getPropertyValue(self, name):
            assert name == "TextPortionType"
            return "Text"

        def getString(self):
            return self.s

        def getStart(self):
            return "portion_start"

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    class _Para:
        def createEnumeration(self):
            return _Enum([_Portion(f"Page {tok1} "), _Portion(f"on {tok2}")])

    cursor1 = MagicMock()
    cursor1.getString.return_value = tok1
    cursor2 = MagicMock()
    cursor2.getString.return_value = tok2
    cursors = [cursor1, cursor2]

    class _Text:
        def __init__(self):
            self.plain = full_str
            self.enum_calls = 0

        def getString(self):
            return self.plain

        def createEnumeration(self):
            self.enum_calls += 1
            return _Enum([_Para()])

        def createTextCursorByRange(self, start):
            return cursors.pop(0)

    text = _Text()
    model = MagicMock()
    inserted_titles = []

    def _inserted(unused_model, cursor, title, *args):
        inserted_titles.append(title)
        return True

    with patch("plugin.writer.field_restore._insert_restored_field", side_effect=_inserted):
        restored = _restore_fields_by_cursor(model, text, nonce=nonce)

    assert restored == 2
    assert inserted_titles == ["date", "page-number"]


def test_restore_field_placeholders_skips_when_prefix_absent():
    """When the field token prefix is absent from plain text, return 0 without document search."""
    from plugin.writer.field_restore import _restore_field_placeholders

    # To avoid MagicMock infinite enumeration loop, set createEnumeration return_value
    text = MagicMock()
    text.getString.return_value = "No fields here"
    enum = MagicMock()
    enum.hasMoreElements.return_value = False
    text.createEnumeration.return_value = enum
    model = MagicMock()

    restored = _restore_field_placeholders(model, text, nonce="abc123")
    assert restored == 0
    model.createSearchDescriptor.assert_not_called()


def test_restore_field_placeholders_in_table_cell_walk():
    """Descent into table cell restores field tokens inside cell XText."""
    nonce = "test1234"
    tok = f"[[WA-FIELD:{nonce}:page-number]]"

    body = MagicMock()
    body.getString.return_value = ""  # Body getString does not include cell text

    table = MagicMock()
    table.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    table.getCellNames.return_value = ("A1",)

    cell = MagicMock()
    cell.getString.return_value = f"Cell with {tok}"

    cursor = MagicMock()
    cursor.getString.return_value = tok
    cell.createTextCursorByRange.return_value = cursor

    class _Portion:
        def getPropertyValue(self, name):
            assert name == "TextPortionType"
            return "Text"

        def getString(self):
            return f"Cell with {tok}"

        def getStart(self):
            return "cell_start"

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    class _Para:
        def createEnumeration(self):
            return _Enum([_Portion()])

    cell.createEnumeration.side_effect = lambda: _Enum([_Para()])
    table.getCellByName.return_value = cell

    body_enum = _Enum([table])
    body.createEnumeration.side_effect = lambda: body_enum

    model = MagicMock()
    inserted_cursors = []

    def fake_insert(unused_model, cur, title, props=None):
        inserted_cursors.append(cur)
        cell.getString.return_value = "Cell with 1"
        return True

    with patch("plugin.writer.field_restore._insert_restored_field", side_effect=fake_insert):
        restored = _restore_field_placeholders(model, body, nonce=nonce)

    assert restored == 1
    assert inserted_cursors == [cursor]


def test_restore_field_placeholders_find_first_fallback_accepts_cell_hit_in_body():
    """When the walk does not restore a cell token, findFirst fallback accepts cell hit if text_obj is body."""
    nonce = "test1234"
    tok = f"[[WA-FIELD:{nonce}:page-number]]"

    body = MagicMock()
    body.getString.return_value = ""

    table = MagicMock()
    table.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    table.getCellNames.return_value = ("A1",)

    cell = MagicMock()
    cell.getString.return_value = tok
    table.getCellByName.return_value = cell

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    body.createEnumeration.side_effect = lambda: _Enum([table])

    model = MagicMock()
    model.getText.return_value = body

    found = MagicMock()
    found.getText.return_value = cell  # Hit is in cell XText, distinct from body
    found.getString.return_value = tok
    found.getEnd.return_value = MagicMock()

    model.findFirst.return_value = found
    model.findNext.return_value = None

    def fake_insert(unused_model, cur, title, props=None):
        cell.getString.return_value = "1"
        return True

    with (
        patch("plugin.writer.field_restore._restore_fields_by_cursor", return_value=0),
        patch("plugin.writer.field_restore._insert_restored_field", side_effect=fake_insert) as mock_insert,
    ):
        restored = _restore_field_placeholders(model, body, nonce=nonce)

    assert restored == 1
    mock_insert.assert_called_once()


def test_restore_field_placeholders_remaining_cell_token_raises_tool_execution_error():
    """When a cell token remains after findFirst returns None, ToolExecutionError is raised with the token."""
    nonce = "test1234"
    tok = f"[[WA-FIELD:{nonce}:page-number]]"

    body = MagicMock()
    body.getString.return_value = ""

    table = MagicMock()
    table.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextTable"
    table.getCellNames.return_value = ("A1",)

    cell = MagicMock()
    cell.getString.return_value = f"Cell with {tok}"
    table.getCellByName.return_value = cell

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    body.createEnumeration.side_effect = lambda: _Enum([table])

    model = MagicMock()
    model.getText.return_value = body
    model.findFirst.return_value = None

    with (
        patch("plugin.writer.field_restore._restore_fields_by_cursor", return_value=0),
        pytest.raises(ToolExecutionError, match=r"\[\[WA-FIELD:test1234:page-number\]\]"),
    ):
        _restore_field_placeholders(model, body, nonce=nonce)
