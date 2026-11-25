from unittest.mock import MagicMock, patch
import pytest

from plugin.framework.errors import DocumentDisposedError, ToolExecutionError
from plugin.writer.field_restore import (
    _FIELD_TITLE_TO_SERVICE,
    _insert_restored_field,
    restore_field_placeholders,
    rewrite_exported_field_spans,
)


class _Enum:
    def __init__(self, items):
        self._items = list(items)

    def hasMoreElements(self):
        return bool(self._items)

    def nextElement(self):
        return self._items.pop(0)


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


@pytest.mark.parametrize(
    "attrs, expected_body",
    [
        ("", "page-count"),
        (' data-numbering-type="ARABIC"', "page-count"),
        (' data-numbering-type="ROMAN_UPPER"', "page-count|numbering_type=ROMAN_UPPER"),
        (' data-numbering-type="7"', "page-count|numbering_type=PAGE_DESCRIPTOR"),
        (' data-numbering-type="bogus"', "page-count"),
        (' data-subtype="NEXT" data-offset="2"', "page-count"),
    ],
)
def test_rewrite_exported_field_spans_page_count_numbering_type(attrs: str, expected_body: str) -> None:
    html = f'<p><span title="page-count"{attrs}/></p>'
    assert rewrite_exported_field_spans(html, nonce="test1234") == f"<p>[[WA-FIELD:test1234:{expected_body}]]</p>"


@pytest.mark.parametrize(
    "html, expected",
    [
        pytest.param(
            '<p><span title="chapter"/></p>',
            "<p>[[WA-FIELD:test1234:chapter]]</p>",
            id="mapped_title",
        ),
        pytest.param(
            '<p><span title="word-count"/> <span title="keywords"/></p>',
            '<p><span title="word-count"/> <span title="keywords"/></p>',
            id="unmapped_titles",
        ),
    ],
)
def test_rewrite_exported_field_spans_mapped_and_unmapped(html: str, expected: str) -> None:
    assert rewrite_exported_field_spans(html, nonce="test1234") == expected


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


def test_insert_restored_field_author_fullname_flag():
    """author-name and author-initials share Author; FullName selects the form."""
    model, text_range, field, text = _mock_field_range()
    _insert_restored_field(model, text_range, "author-name")
    model.createInstance.assert_called_once_with("com.sun.star.text.textfield.Author")
    field.setPropertyValue.assert_any_call("FullName", True)

    model, text_range, field, text = _mock_field_range()
    _insert_restored_field(model, text_range, "author-initials")
    model.createInstance.assert_called_once_with("com.sun.star.text.textfield.Author")
    field.setPropertyValue.assert_any_call("FullName", False)


def test_restore_field_placeholders_creates_mapped_services():
    """Placeholder search walks every mapped title and inserts that service."""
    created = []
    text_obj = MagicMock()
    text_obj.getString.return_value = "[[WA-FIELD:nonce:page-number]]"

    def create_instance(svc):
        text_obj.getString.return_value = ""
        created.append(svc)
        return MagicMock()

    titles = list(_FIELD_TITLE_TO_SERVICE)
    sd_captured = []

    def make_hit(title):
        rng = MagicMock()
        text_obj.createTextCursorByRange.return_value = MagicMock()
        rng.getText.return_value = text_obj
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

    restored = restore_field_placeholders(model, text_obj, nonce="nonce")
    assert restored == len(_FIELD_TITLE_TO_SERVICE)
    model.findFirst.assert_called_once()
    assert all(title in sd_captured[0] for title in _FIELD_TITLE_TO_SERVICE)
    assert set(created) == set(_FIELD_TITLE_TO_SERVICE.values())


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
        patch.object(fr, "_insert_restored_field", return_value=None) as insert,
    ):
        assert fr.restore_field_placeholders(model, header, nonce="nonce") >= 1
        assert insert.called

    with (
        patch.object(fr, "uno_same", return_value=False),
        patch.object(fr, "_insert_restored_field", return_value=None) as insert,
    ):
        assert fr.restore_field_placeholders(model, header, nonce="nonce") == 0
        insert.assert_not_called()


@pytest.mark.parametrize(
    "portions_fn, expected_titles",
    [
        pytest.param(
            lambda n: [
                f"Page [[WA-FIELD:{n}:page-number]] and at [[WA-FIELD:{n}:time]] ",
                f"on [[WA-FIELD:{n}:date]] end",
            ],
            ["page-number", "time", "date"],
            id="two_portions_three_tokens",
        ),
        pytest.param(
            lambda n: [f"Start [[WA-FIELD:{n}:page-number]] middle [[WA-FIELD:{n}:date]] end"],
            ["page-number", "date"],
            id="one_portion_two_tokens",
        ),
    ],
)
def test_restore_fields_by_cursor_single_walk_first_token_first(portions_fn, expected_titles):
    """Tokens across one or multiple portions: one createEnumeration walk, no findFirst, inserted in document order."""
    nonce = "test1234"
    portion_strs = portions_fn(nonce)
    full_str = "".join(portion_strs)

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

    class _Para:
        def createEnumeration(self):
            return _Enum([_Portion(s, f"p{idx}") for idx, s in enumerate(portion_strs)])

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

    token_matches = [
        f"[[WA-FIELD:{nonce}:{title}]]"
        for title in expected_titles
    ]
    text_cursors = []
    for tok in token_matches:
        c = MagicMock()
        c.getString.return_value = tok
        text_cursors.append(c)

    text.createTextCursorByRange = lambda start: text_cursors.pop(0)

    model = MagicMock()
    model.findFirst.side_effect = AssertionError("findFirst should not run")

    inserted_titles = []

    def _inserted(unused_model, cursor, title, *args):
        inserted_titles.append(title)
        text.plain = text.plain.replace(f"[[WA-FIELD:{nonce}:{title}]]", title)
        return True

    with patch("plugin.writer.field_restore._insert_restored_field", side_effect=_inserted):
        restored = restore_field_placeholders(model, text, nonce=nonce)

    assert restored == len(expected_titles)
    model.findFirst.assert_not_called()
    assert inserted_titles == expected_titles


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
        restored = restore_field_placeholders(model, text, nonce=nonce)

    assert restored == 1
    model.findFirst.assert_not_called()

    # Region with no token: findFirst is not called.
    clean_text = _Text()
    clean_text.plain = "Clean region without token"
    assert restore_field_placeholders(model, clean_text, nonce=nonce) == 0
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
        restored = restore_field_placeholders(model, text, nonce=nonce)

    assert restored == 1
    assert go_right_calls == [(len(needle), True)]


def test_restore_fields_by_cursor_skips_unreadable_reads():
    from plugin.writer.field_restore import _restore_fields_by_cursor

    nonce = "test1234"
    tok_date = f"[[WA-FIELD:{nonce}:date]]"
    tok_page = f"[[WA-FIELD:{nonce}:page-number]]"
    full_text = f"{tok_date} {tok_page}"

    class _Portion:
        def __init__(self, text, raise_kind=False, raise_string=False):
            self._text = text
            self._raise_kind = raise_kind
            self._raise_string = raise_string

        def getPropertyValue(self, name):
            if self._raise_kind:
                raise RuntimeError("kind failed")
            assert name == "TextPortionType"
            return "Text"

        def getString(self):
            if self._raise_string:
                raise RuntimeError("getString failed")
            return self._text

        def getStart(self):
            return self

    class _Para1:
        def createEnumeration(self):
            raise RuntimeError("para enum failed")

    class _Para2:
        def createEnumeration(self):
            return _Enum([
                _Portion("bad", raise_kind=True),
                _Portion(tok_date),
            ])

    class _Para3:
        def createEnumeration(self):
            return _Enum([_Portion("bad", raise_string=True)])

    class _Para4:
        def createEnumeration(self):
            return _Enum([_Portion(tok_page)])

    class _Text:
        def getString(self):
            return full_text

        def createEnumeration(self):
            return _Enum([_Para1(), _Para2(), _Para3(), _Para4()])

        def createTextCursorByRange(self, start):
            c = MagicMock()
            c.getString.return_value = getattr(start, "_text", "")
            return c

    text = _Text()
    model = MagicMock()
    inserted = []

    def fake_insert(unused_model, cur, title, props=None):
        inserted.append(title)
        return True

    with patch("plugin.writer.field_restore._insert_restored_field", side_effect=fake_insert):
        restored = _restore_fields_by_cursor(model, text, nonce=nonce)

    assert restored == 1
    assert inserted == ["page-number"]


def test_restore_fields_by_cursor_portion_disposal_raises():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.field_restore import _restore_fields_by_cursor

    nonce = "test1234"
    tok = f"[[WA-FIELD:{nonce}:page-number]]"
    disposed_exc_type = type("DisposedException", (Exception,), {})

    class _Portion:
        def getPropertyValue(self, name):
            raise disposed_exc_type("gone")

        def getString(self):
            return tok

    class _Para:
        def createEnumeration(self):
            return _Enum([_Portion()])

    class _Text:
        def getString(self):
            return tok

        def createEnumeration(self):
            return _Enum([_Para()])

    text = _Text()
    model = MagicMock()

    with pytest.raises(DocumentDisposedError):
        _restore_fields_by_cursor(model, text, nonce=nonce)


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
        restored = restore_field_placeholders(model, text, nonce=nonce)

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
        restore_field_placeholders(model, text, nonce=nonce)


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
    text = MagicMock()
    text.getString.return_value = "[[WA-FIELD:nonce:page-number]]"
    with pytest.raises(RuntimeError, match="no search"):
        restore_field_placeholders(model, text, nonce="nonce")


def test_restore_field_placeholders_find_error_propagates():
    model = MagicMock()
    model.findFirst.side_effect = RuntimeError("find failed")
    text = MagicMock()
    text.getString.return_value = "[[WA-FIELD:nonce:page-number]]"
    with pytest.raises(RuntimeError, match="find failed"):
        restore_field_placeholders(model, text, nonce="nonce")


def test_restore_field_placeholders_ignores_token_with_different_or_missing_nonce():
    """A body string that already contains [[WA-FIELD:page-number]] is left alone when nonce differs."""
    text = MagicMock()
    text.getString.return_value = "Existing [[WA-FIELD:page-number]] and [[WA-FIELD:other:page-number]]"
    model = MagicMock()
    model.createSearchDescriptor.return_value = MagicMock()
    model.findFirst.return_value = None

    with patch("plugin.writer.field_restore._insert_restored_field") as mock_insert:
        restored = restore_field_placeholders(model, text, nonce="thiscall")

    assert restored == 0
    mock_insert.assert_not_called()


def test_restore_field_placeholders_skips_when_prefix_absent():
    """When the field token prefix is absent from plain text, return 0 without document search."""
    from plugin.writer.field_restore import restore_field_placeholders

    # To avoid MagicMock infinite enumeration loop, set createEnumeration return_value
    text = MagicMock()
    text.getString.return_value = "No fields here"
    enum = MagicMock()
    enum.hasMoreElements.return_value = False
    text.createEnumeration.return_value = enum
    model = MagicMock()

    restored = restore_field_placeholders(model, text, nonce="abc123")
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
        restored = restore_field_placeholders(model, body, nonce=nonce)

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
        restored = restore_field_placeholders(model, body, nonce=nonce)

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

    body.createEnumeration.side_effect = lambda: _Enum([table])

    model = MagicMock()
    model.getText.return_value = body
    model.findFirst.return_value = None

    with (
        patch("plugin.writer.field_restore._restore_fields_by_cursor", return_value=0),
        pytest.raises(ToolExecutionError, match=r"\[\[WA-FIELD:test1234:page-number\]\]"),
    ):
        restore_field_placeholders(model, body, nonce=nonce)


def test_find_tokens_prefilter_skips_non_matching_paragraphs_and_walks_unreadable():
    import re
    from plugin.writer.field_restore import find_tokens

    prefix = "[[WA-MATH:n1:"
    token_re = re.compile(r"\[\[WA-MATH:([a-zA-Z0-9]+):(\d+)\]\]")

    para_skip = MagicMock()
    para_skip.getString.return_value = "plain paragraph without token"

    para_unreadable = MagicMock()
    para_unreadable.getString.side_effect = RuntimeError("table has no getString")
    portion = MagicMock()
    portion.getPropertyValue.return_value = "Text"
    portion.getString.return_value = "[[WA-MATH:n1:0]]"
    para_unreadable.createEnumeration.return_value = _Enum([portion])

    xtext = MagicMock()
    xtext.getString.return_value = "[[WA-MATH:n1:0]]"
    xtext.createEnumeration.return_value = _Enum([para_skip, para_unreadable])
    cur = MagicMock()
    cur.goRight.return_value = True
    cur.getString.return_value = "[[WA-MATH:n1:0]]"
    xtext.createTextCursorByRange.return_value = cur

    hits = find_tokens(xtext, prefix, token_re)
    assert len(hits) == 1
    para_skip.createEnumeration.assert_not_called()
    para_unreadable.createEnumeration.assert_called_once()


def test_find_tokens_joins_soft_page_break_and_stops_at_text_field():
    import re
    from plugin.writer.field_restore import find_tokens

    prefix = "[[WA-MATH:n1:"
    token_re = re.compile(r"\[\[WA-MATH:([a-zA-Z0-9]+):(\d+)\]\]")

    def _portion(kind, text, start_marker=None):
        p = MagicMock()
        p.getPropertyValue.return_value = kind
        p.getString.return_value = text
        p.getStart.return_value = start_marker
        return p

    # Token 0 split across Text + SoftPageBreak + Text with leading prose "ab"
    p1 = _portion("Text", "ab[[WA-", "START_P1")
    p_spb = _portion("SoftPageBreak", "")
    p2 = _portion("Text", "MATH:n1:0]]", "START_P2")
    # Token 1 interrupted by a TextField must not match
    p3 = _portion("Text", "[[WA-MATH:", "START_P3")
    p_field = _portion("TextField", "1")
    p4 = _portion("Text", "n1:1]]", "START_P4")

    para = MagicMock()
    para.getString.return_value = "ab[[WA-MATH:n1:0]][[WA-MATH:1n1:1]]"
    para.createEnumeration.return_value = _Enum([p1, p_spb, p2, p3, p_field, p4])

    xtext = MagicMock()
    xtext.getString.return_value = "ab[[WA-MATH:n1:0]][[WA-MATH:1n1:1]]"
    xtext.createEnumeration.return_value = _Enum([para])

    cur = MagicMock()
    cur.goRight.return_value = True
    cur.getString.return_value = "[[WA-MATH:n1:0]]"
    xtext.createTextCursorByRange.return_value = cur

    hits = find_tokens(xtext, prefix, token_re)
    assert len(hits) == 1
    assert hits[0][1].group(0) == "[[WA-MATH:n1:0]]"
    xtext.createTextCursorByRange.assert_called_once_with("START_P1")
    assert cur.goRight.call_args_list[0].args == (2, False)
    assert cur.goRight.call_args_list[1].args == (16, True)
