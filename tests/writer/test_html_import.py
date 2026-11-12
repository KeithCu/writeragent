
import html as html_mod
from typing import Any

import pytest
from unittest.mock import MagicMock, patch

from plugin.writer.html_import import (
    ensure_html_linebreaks,
    content_has_markup,
    replace_xtext_with_html,
)
from plugin.writer.html_ruby import (
    apply_ruby_spans,
    extract_and_strip_ruby,
)


def test_extract_and_strip_ruby_simple():
    html = "<p>前<ruby>漢字<rt>かんじ</rt></ruby>後</p>"
    clean, spans = extract_and_strip_ruby(html)
    assert clean == "<p>前漢字後</p>"
    # Offset 1: the visible text before the ruby is 前, not the first character of the file.
    assert spans == [("漢字", "かんじ", True, 1)]


def test_extract_and_strip_ruby_under_and_rp():
    html = '<ruby style="ruby-position: under">東京<rp>(</rp><rt>とうきょう</rt><rp>)</rp></ruby>'
    clean, spans = extract_and_strip_ruby(html)
    assert clean == "東京"
    assert spans == [("東京", "とうきょう", False, 0)]


def test_extract_and_strip_ruby_position_ignores_unrelated_attrs():
    html = '<ruby class="thunder">東京<rt>とうきょう</rt></ruby>'
    clean, spans = extract_and_strip_ruby(html)
    assert clean == "東京"
    assert len(spans) == 1
    assert spans[0].is_above is True


def test_extract_and_strip_ruby_unbalanced_ruby_has_no_spans():
    html = "<ruby>漢字<rt>かんじ</rt>" * 2000
    clean, spans = extract_and_strip_ruby(html)
    assert spans == []
    assert "<rt>" not in clean
    assert "かんじ" not in clean


def test_extract_and_strip_ruby_uppercase_tags():
    html = "<RUBY>漢字<RT>かんじ</RT></RUBY>"
    clean, spans = extract_and_strip_ruby(html)
    assert clean == "漢字"
    assert spans == [("漢字", "かんじ", True, 0)]


def test_iter_ruby_spans_in_paragraph_skips_ruby_inside_deletion():
    from plugin.writer.html_ruby import _iter_ruby_spans_in_paragraph

    para = MagicMock()
    portion_start = MagicMock()
    portion_start.getPropertyValue.side_effect = lambda prop: {
        "TextPortionType": "Ruby",
        "RubyText": "とうきょう",
        "RubyIsAbove": True,
    }[prop]
    portion_base = MagicMock()
    portion_base.getPropertyValue.side_effect = lambda prop: {"TextPortionType": "Text"}[prop]
    portion_base.getString.return_value = "東京"
    portion_end = MagicMock()
    portion_end.getPropertyValue.side_effect = lambda prop: {"TextPortionType": "Ruby"}[prop]

    portion_enum = MagicMock()
    portion_enum.hasMoreElements.side_effect = [True, True, True, False]
    portion_enum.nextElement.side_effect = [portion_start, portion_base, portion_end]
    para.createEnumeration.return_value = portion_enum

    # When inside deletion (e.g. deletion_state=[True]), ruby spans are omitted
    spans = list(_iter_ruby_spans_in_paragraph(para, deletion_state=[True]))
    assert spans == []


def test_extract_and_strip_ruby_noop():
    html = "<p>plain</p>"
    clean, spans = extract_and_strip_ruby(html)
    assert clean == html
    assert spans == []


def test_content_has_markup_detects_ruby():
    assert content_has_markup("<ruby>漢字<rt>かんじ</rt></ruby>")
    assert content_has_markup("<p>前<ruby>漢字<rt>かんじ</rt></ruby>後</p>")


class _RubyCursor:
    """Plain-text cursor: goRight and getString share one string."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.start = 0
        self.end = 0
        self.ruby: tuple[int, int, str] | None = None

    def gotoEnd(self, expand: bool) -> bool:
        if expand:
            self.end = len(self._text)
        else:
            self.start = self.end = len(self._text)
        return True

    def goRight(self, count: int, expand: bool) -> bool:
        if expand:
            self.end += count
        else:
            self.start += count
            self.end = self.start
        return self.end <= len(self._text) and self.start <= len(self._text)

    def getString(self) -> str:
        return self._text[self.start:self.end]

    def setPropertyValue(self, name: str, value: object) -> None:
        if name == "RubyText":
            self.ruby = (self.start, self.end, str(value))


class _RubyText:
    def __init__(self, text: str) -> None:
        self._text = text
        self.cursors: list[_RubyCursor] = []

    def getStart(self) -> int:
        return 0

    def createTextCursorByRange(self, origin: object) -> _RubyCursor:
        cursor = _RubyCursor(self._text)
        if isinstance(origin, _RubyCursor):
            cursor.start = origin.start
            cursor.end = origin.end
        self.cursors.append(cursor)
        return cursor


def test_ruby_reading_attaches_to_later_base_not_the_first_copy():
    """A plain 漢字 before <ruby>漢字</ruby> must not take the reading."""
    html = "<p>漢字と<ruby>漢字<rt>かんじ</rt></ruby></p>"
    clean, spans = extract_and_strip_ruby(html)
    assert clean == "<p>漢字と漢字</p>"
    assert spans == [("漢字", "かんじ", True, len("漢字と"))]
    text = _RubyText("漢字と漢字")
    apply_ruby_spans(text, spans)
    painted = [cursor.ruby for cursor in text.cursors if cursor.ruby]
    assert painted == [(len("漢字と"), len("漢字と漢字"), "かんじ")]


def test_ruby_offset_is_relative_to_the_imported_suffix():
    """skip_chars is existing text. The span offset is inside the inserted suffix."""
    prefix = "前置き"
    suffix = "漢字と漢字"
    text = _RubyText(prefix + suffix)
    spans = [("漢字", "かんじ", True, len("漢字と"))]
    apply_ruby_spans(text, spans, skip_chars=len(prefix))
    painted = [cursor.ruby for cursor in text.cursors if cursor.ruby]
    start = len(prefix) + len("漢字と")
    assert painted == [(start, start + len("漢字"), "かんじ")]


def test_ruby_readings_stay_on_their_own_repeated_base():
    html = "<p><ruby>漢字<rt>いち</rt></ruby><ruby>漢字<rt>に</rt></ruby></p>"
    _unused, spans = extract_and_strip_ruby(html)
    assert [span[1] for span in spans] == ["いち", "に"]
    assert [span[3] for span in spans] == [0, len("漢字")]
    text = _RubyText("漢字漢字")
    apply_ruby_spans(text, spans)
    painted = [cursor.ruby for cursor in text.cursors if cursor.ruby]
    assert painted == [(0, 2, "いち"), (2, 4, "に")]


def test_ruby_offset_counts_br_before_the_base():
    html = "<p>漢字<br>x<ruby>漢字<rt>かんじ</rt></ruby></p>"
    clean, spans = extract_and_strip_ruby(html)
    assert clean == "<p>漢字<br>x漢字</p>"
    assert spans == [("漢字", "かんじ", True, len("漢字\nx"))]
    text = _RubyText("漢字\nx漢字")
    apply_ruby_spans(text, spans)
    painted = [cursor.ruby for cursor in text.cursors if cursor.ruby]
    assert painted == [(4, 6, "かんじ")]


def test_ruby_offset_counts_paragraph_end():
    html = "<p>a</p><p><ruby>漢字<rt>かんじ</rt></ruby></p>"
    clean, spans = extract_and_strip_ruby(html)
    assert clean == "<p>a</p><p>漢字</p>"
    assert spans == [("漢字", "かんじ", True, len("a\n"))]


def test_ruby_repeated_base_after_br_keeps_own_reading():
    html = "<p><ruby>漢字<rt>いち</rt></ruby>漢字<br><ruby>漢字<rt>に</rt></ruby></p>"
    _clean, spans = extract_and_strip_ruby(html)
    text = _RubyText("漢字漢字\n漢字")
    apply_ruby_spans(text, spans)
    painted = [cursor.ruby for cursor in text.cursors if cursor.ruby]
    assert painted == [(0, 2, "いち"), (5, 7, "に")]


def test_ruby_spans_forward_cursor_walk():
    # One hundred spans spread through a long string: len(text.cursors) stays far below
    # one cursor per span plus a full restart, and painted triples match offsets.
    spans = []
    parts = []
    offset = 0
    expected_painted = []
    for i in range(100):
        filler = f"filler{i}"
        parts.append(filler)
        offset += len(filler)
        base = "漢"
        reading = f"かん{i}"
        spans.append((base, reading, True, offset))
        expected_painted.append((offset, offset + len(base), reading))
        parts.append(base)
        offset += len(base)
    full_text = "".join(parts)
    text = _RubyText(full_text)
    apply_ruby_spans(text, spans)
    painted = [c.ruby for c in text.cursors if c.ruby]
    assert painted == expected_painted
    assert len(text.cursors) <= 205


def test_mixed_math_segments_produce_separate_ruby_spans_without_tex_source():
    from plugin.writer.html_import import segment_html_with_mixed_math
    from plugin.writer.html_ruby import extract_and_strip_ruby

    content = "<p><ruby>東<rt>ひがし</rt></ruby> \\(x^2 + y^2\\) <ruby>西<rt>にし</rt></ruby></p>"
    segs = segment_html_with_mixed_math(content)
    assert len(segs) == 3
    assert segs[0].kind == "html"
    assert segs[1].kind == "tex"
    assert segs[2].kind == "html"

    _clean0, spans0 = extract_and_strip_ruby(segs[0].text)
    _clean2, spans2 = extract_and_strip_ruby(segs[2].text)

    assert spans0 == [("東", "ひがし", True, 0)]
    assert spans2 == [("西", "にし", True, 1)]



def test_ensure_html_linebreaks_does_not_unescape_again():
    """A second unescape used to turn entity-escaped markup into live tags.

    Insert/replace already unescape once. ``&amp;lt;p&amp;gt;`` arrives here as
    ``&lt;p&gt;`` and must stay an entity, not a ``<p>`` element.
    """
    once = html_mod.unescape("&amp;lt;p&amp;gt;hello&amp;lt;/p&amp;gt;")
    assert once == "&lt;p&gt;hello&lt;/p&gt;"
    out = ensure_html_linebreaks(once)
    assert "<p>hello</p>" not in out
    assert "&lt;p&gt;hello&lt;/p&gt;" in out

    inside = html_mod.unescape("<p>see &amp;lt;b&amp;gt;x&amp;lt;/b&amp;gt;</p>")
    wrapped = ensure_html_linebreaks(inside)
    assert "<b>" not in wrapped
    assert "&lt;b&gt;x&lt;/b&gt;" in wrapped
    assert "<p>see" in wrapped


def test_ensure_html_linebreaks_escapes_prose_angle_brackets():
    out = ensure_html_linebreaks("<p>List<String> and 2 < 3</p>")
    assert "List&lt;String&gt;" in out
    assert "2 &lt; 3" in out
    assert "<p>" in out and "</p>" in out


def test_insert_content_entry_points_decode_complete_entities():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import

    raw = "<p>&sect 2o &amp;lt;b&amp;gt;x&amp;lt;/b&amp;gt;</p>"
    expected = "<p>&sect 2o &lt;b&gt;x&lt;/b&gt;</p>"

    model = MagicMock()
    model.getPropertyValue.return_value = False

    with patch.object(html_import, "_insert_mixed_or_plain_html") as mock_mixed:
        html_import.replace_full_document(model, MagicMock(), raw)
        assert mock_mixed.call_args.args[3] == expected

    cursor = MagicMock()
    model.getText.return_value.createTextCursor.return_value = cursor
    with patch.object(html_import, "_insert_mixed_or_plain_html") as mock_mixed:
        html_import.insert_content_at_position(model, MagicMock(), raw, "beginning")
        assert mock_mixed.call_args.args[3] == expected

    text_range = MagicMock()
    text_range.getText.return_value = MagicMock()
    with patch.object(html_import, "_insert_mixed_or_plain_html") as mock_mixed, \
         patch("plugin.writer.html_import.raise_if_range_hosts_nested_table"):
        html_import.replace_single_range_with_content(model, text_range, raw, MagicMock())
        assert mock_mixed.call_args.args[3] == expected


def test_replace_single_range_block_html_in_cell_raises_coded_error():
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import html_import

    model = MagicMock()
    text_range = MagicMock()
    cell_cursor = MagicMock()
    cell_cursor.getPropertyValue.return_value = MagicMock()  # TextTable is not None
    text_obj = MagicMock()
    text_obj.createTextCursorByRange.return_value = cell_cursor
    text_range.getText.return_value = text_obj

    with patch.object(html_import, "_insert_mixed_or_plain_html", side_effect=RuntimeError("nested xtext failure")), \
         patch("plugin.writer.html_import.raise_if_range_hosts_nested_table"):
        with pytest.raises(ToolExecutionError) as exc_info:
            html_import.replace_single_range_with_content(model, text_range, "<p>block</p>", MagicMock())
        assert exc_info.value.code == "TABLE_BLOCK_IN_CELL"
        assert "Rich/block HTML can't be inserted inside a table cell" in str(exc_info.value)


def test_content_has_markup_markdown_punctuation_treated_as_plain():
    from plugin.writer.html_import import content_has_markup

    assert not content_has_markup("**bold**")
    assert not content_has_markup("# Heading")
    assert not content_has_markup("| col1 | col2 |")
    assert not content_has_markup("Issue # 5")
    assert not content_has_markup("a | b")
    assert not content_has_markup("2**3")
    assert not content_has_markup("__init__")
    assert not content_has_markup("```code```")
    assert not content_has_markup("- [ ] todo")
    assert content_has_markup("<b>bold</b>")
    assert content_has_markup(r"\(x^2\)")
    assert content_has_markup("$$E=mc^2$$")



def test_replace_xtext_with_html_undo_rollback_on_import_failure():

    text = MagicMock()
    cursor = MagicMock()
    text.createTextCursor.return_value = cursor

    model = MagicMock()
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    undo_title = None

    def enter_ctx(title):
        nonlocal undo_title
        undo_title = title

    undo_mgr.enterUndoContext.side_effect = enter_ctx
    undo_mgr.getAllUndoActionTitles.side_effect = lambda: [undo_title]
    model.getUndoManager.return_value = undo_mgr

    with patch(
        "plugin.writer.html_import.insert_html_fragment_at_cursor",
        side_effect=RuntimeError("import failed"),
    ):
        with pytest.raises(RuntimeError, match="import failed"):
            replace_xtext_with_html(text, "<p>New</p>", model=model)

    undo_mgr.enterUndoContext.assert_called_once()
    undo_mgr.leaveUndoContext.assert_called_once()
    undo_mgr.undo.assert_called_once()
    assert len(cursor.setString.call_args_list) == 1
    assert cursor.setString.call_args_list[0].args == ("",)


def _undo_model():
    model = MagicMock()
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    model.getUndoManager.return_value = undo_mgr
    return model


def test_replace_xtext_with_html_applies_and_reports_data_lo_attrs():
    """Header/footer writes strip data-lo-* before the import and apply them after it."""
    text = MagicMock()
    imported: list[str] = []
    html = (
        '<p data-lo-style="Header" data-lo-para="margin-left:1cm; font-weight:bold">H</p>'
        '<table><tr><td><p data-lo-para="margin-top:1cm">c</p></td></tr></table>'
    )
    with patch(
        "plugin.writer.html_import.insert_html_fragment_at_cursor",
        side_effect=lambda cursor, prepared, **kw: imported.append(prepared),
    ), patch(
        "plugin.writer.html_import_styles.apply_block_lo_styles", return_value=["font-weight"],
    ) as apply_styles:
        failed = replace_xtext_with_html(text, html, model=_undo_model())

    assert "data-lo-" not in imported[0]
    args, kwargs = apply_styles.call_args
    assert args[1] is text and args[3] == ["Header"]
    assert kwargs["paras"] == ["margin-left:1cm; font-weight:bold"]
    # The table-cell declaration is never applied, so it is reported.
    assert failed.unapplied_para == ["margin-top", "font-weight"]


def test_replace_xtext_with_html_plain_html_skips_style_walk():
    text = MagicMock()
    with patch("plugin.writer.html_import.insert_html_fragment_at_cursor"), patch(
        "plugin.writer.html_import_styles.apply_block_lo_styles",
    ) as apply_styles:
        assert replace_xtext_with_html(text, "<p>New</p>", model=_undo_model()).unapplied_para == []
    apply_styles.assert_not_called()


def test_replace_xtext_with_html_undo_rollback_on_field_restore_failure():

    text = MagicMock()
    cursor = MagicMock()
    text.createTextCursor.return_value = cursor

    model = MagicMock()
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    undo_title = None

    def enter_ctx(title):
        nonlocal undo_title
        undo_title = title

    undo_mgr.enterUndoContext.side_effect = enter_ctx
    undo_mgr.getAllUndoActionTitles.side_effect = lambda: [undo_title]
    model.getUndoManager.return_value = undo_mgr

    with patch("plugin.writer.html_import.insert_html_fragment_at_cursor"), \
         patch("plugin.writer.html_ruby.apply_ruby_spans"), \
         patch("plugin.writer.field_restore.restore_field_placeholders", side_effect=RuntimeError("field failed")):
        with pytest.raises(RuntimeError, match="field failed"):
            replace_xtext_with_html(text, "<p>New</p>", model=model)

    undo_mgr.enterUndoContext.assert_called_once()
    undo_mgr.leaveUndoContext.assert_called_once()
    undo_mgr.undo.assert_called_once()


def test_replace_xtext_with_html_undo_manager_locked_refuses_clear():
    from plugin.framework.errors import ToolExecutionError

    text = MagicMock()
    cursor = MagicMock()
    text.createTextCursor.return_value = cursor

    model = MagicMock()
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = True
    model.getUndoManager.return_value = undo_mgr

    with pytest.raises(ToolExecutionError, match="Could not enter undo context"):
        replace_xtext_with_html(text, "<p>New</p>", model=model)

    cursor.setString.assert_not_called()



def test_insert_inline_at_cursor_plain_text_does_not_wrap_in_p():
    """Plain text wrapped in <p> split the host paragraph when inserted mid-paragraph."""
    from unittest.mock import MagicMock, patch

    from plugin.writer import html_import

    cursor = MagicMock()
    with patch.object(html_import, "insert_html_fragment_at_cursor") as frag:
        html_import.insert_inline_at_cursor(MagicMock(), MagicMock(), cursor, "bem ")
    cursor.getText.return_value.insertString.assert_called_once_with(cursor, "bem ", False)
    frag.assert_not_called()


def test_insert_inline_at_cursor_keeps_edge_whitespace_of_markup():
    """The HTML import drops a fragment's edge whitespace: "<b>muito</b> " fused with the next word."""
    from unittest.mock import MagicMock, patch

    from plugin.writer import html_import

    cursor = MagicMock()
    cursor.getText.return_value.createTextCursor.return_value.getString.return_value = ""
    text = cursor.getText.return_value
    with patch.object(html_import, "insert_html_fragment_at_cursor") as frag:
        html_import.insert_inline_at_cursor(MagicMock(), MagicMock(), cursor, "<b>muito</b> ")
    text.insertString.assert_called_once_with(cursor, " ", False)
    cursor.goLeft.assert_called_once_with(1, False)
    frag.assert_called_once()
    assert frag.call_args.args[1] == "<b>muito</b>"
    assert frag.call_args.kwargs["wrap"] is False


def test_mixed_math_segments_stay_in_place_not_at_body_end():
    """#46/#47: after a math segment the rest of a mid-document replace went to the END of the
    body ("[Math import failed] ..." orphaned after the last paragraph)."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from plugin.writer import html_import

    model, cursor = MagicMock(), MagicMock()
    parked = cursor.getText.return_value.createTextCursorByRange.return_value
    failed = SimpleNamespace(ok=False, starmath=None, error_message="no converter")
    with patch.object(html_import, "insert_html_fragment_at_cursor") as frag, \
         patch.object(html_import, "convert_latex_to_starmath", return_value=failed):
        html_import._insert_mixed_html_and_math_at_cursor(model, MagicMock(), cursor, "<p>antes \\(x\\) depois</p>")
    assert frag.call_count == 2
    cursor.getText.return_value.insertString.assert_called_once_with(cursor, "[Math import failed] x", False)
    assert cursor.gotoRange.call_args.args == (parked.getStart.return_value, False)
    model.getText.assert_not_called()


def test_insert_inline_at_cursor_does_not_unescape_plain_text_again():
    """apply_document_content.py decodes only complete references; a second html.unescape turned "&sect 2o"
    into "§ 2o"."""
    from unittest.mock import MagicMock, patch

    from plugin.writer import html_import

    cursor = MagicMock()
    with patch.object(html_import, "insert_html_fragment_at_cursor"):
        html_import.insert_inline_at_cursor(MagicMock(), MagicMock(), cursor, "&sect 2o ")
    cursor.getText.return_value.insertString.assert_called_once_with(cursor, "&sect 2o ", False)


def test_insert_inline_at_cursor_paints_ruby_instead_of_gluing_the_reading():
    from unittest.mock import MagicMock, patch

    from plugin.writer import html_import, html_ruby, range_copy

    cursor = MagicMock()
    with patch.object(html_import, "insert_html_fragment_at_cursor") as frag, \
         patch.object(range_copy, "prefix_char_count", return_value=7), \
         patch.object(html_ruby, "apply_ruby_spans") as paint:
        html_import.insert_inline_at_cursor(MagicMock(), MagicMock(), cursor, "<ruby>漢字<rt>かんじ</rt></ruby>")
    assert frag.call_args.args[1] == "漢字"
    assert paint.call_args.args[1] == [("漢字", "かんじ", True, 0)]
    assert paint.call_args.kwargs["skip_chars"] == 7


def test_insert_inline_at_cursor_skips_prefix_count_without_ruby():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import, range_copy

    cursor = MagicMock()
    with patch.object(range_copy, "prefix_char_count") as count, \
         patch.object(html_import, "insert_html_fragment_at_cursor"):
        html_import.insert_inline_at_cursor(MagicMock(), MagicMock(), cursor, "<b>x</b>")
    count.assert_not_called()


def test_insert_mixed_or_plain_html_skips_prefix_count_without_ruby():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import, range_copy

    cursor = MagicMock()
    with patch.object(range_copy, "prefix_char_count") as count, \
         patch.object(html_import, "insert_html_fragment_at_cursor"):
        html_import._insert_mixed_or_plain_html(MagicMock(), MagicMock(), cursor, "<p>hi</p>")
    count.assert_not_called()


def test_insert_mixed_html_and_math_skips_prefix_count_without_ruby():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import, range_copy

    cursor = MagicMock()
    with patch.object(range_copy, "prefix_char_count") as count, \
         patch.object(html_import, "insert_html_fragment_at_cursor"), \
         patch.object(html_import, "html_fragment_contains_mixed_math", return_value=True):
        html_import._insert_mixed_html_and_math_at_cursor(
            MagicMock(), MagicMock(), cursor, "<p>text before</p> \\(x^2\\) <p>text after</p>"
        )
    count.assert_not_called()


def test_insert_mixed_html_and_math_counts_prefix_when_segment_contains_ruby():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import, html_ruby, range_copy

    cursor = MagicMock()
    calls = []
    with patch.object(range_copy, "prefix_char_count", side_effect=lambda t, c: calls.append("count") or 5) as count, \
         patch.object(html_import, "insert_html_fragment_at_cursor", side_effect=lambda *a, **k: calls.append("insert")), \
         patch.object(html_ruby, "apply_ruby_spans") as paint, \
         patch.object(html_import, "html_fragment_contains_mixed_math", return_value=True):
        html_import._insert_mixed_html_and_math_at_cursor(
            MagicMock(), MagicMock(), cursor, "<p><ruby>漢字<rt>かんじ</rt></ruby></p> \\(x^2\\)"
        )
    count.assert_called_once()
    assert calls == ["count", "insert"]
    assert paint.call_args.kwargs["skip_chars"] == 5


def test_nested_blocks_share_one_style_slot():
    """<blockquote><p> and <li><p> are ONE Writer paragraph: two slots shifted every later style
    ("2. DO DIREITO" lost its heading) and the leftover ones hit old text (relatos #35/#40)."""
    from plugin.writer.html_import_styles import extract_block_lo_styles

    html = ('<h1 data-lo-style="Heading1">T</h1>'
            '<blockquote><p data-lo-style="Quotations">q</p></blockquote>'
            '<ul><li><p data-lo-style="Standard">a</p></li><li><p data-lo-style="Standard">b</p></li></ul>'
            '<h2 data-lo-style="Heading2">D</h2>')
    clean, styles, _paras, _skipped = extract_block_lo_styles(html)
    assert styles == ["Heading1", "Quotations", "Standard", "Standard", "Heading2"]
    assert "data-lo-style" not in clean


def test_second_block_inside_a_block_gets_its_own_slot():
    from plugin.writer.html_import_styles import extract_block_lo_styles

    _clean, styles, _paras, _skipped = extract_block_lo_styles(
        '<li data-lo-style="List"><p>a</p><p data-lo-style="Standard">b</p></li><p data-lo-style="Standard">c</p>')
    assert styles == ["List", "Standard", "Standard"]


def test_block_styles_stop_at_the_end_of_the_import():
    """More styles than imported paragraphs must not style the text after the import (the old
    text a full_document had just deleted: its Delete turned into a Format change)."""
    from unittest.mock import MagicMock, patch

    from plugin.writer import html_import_styles

    text = MagicMock()
    walk_cur = MagicMock()
    starts = [0, 1, 2, 3]
    pos = [0]

    def get_start():
        return starts[min(pos[0], len(starts) - 1)]

    def goto_next(expand):
        pos[0] += 1
        return pos[0] < len(starts)

    walk_cur.getStart.side_effect = get_start
    walk_cur.gotoNextParagraph.side_effect = goto_next
    text.createTextCursorByRange.return_value = walk_cur

    end = 2  # the parked cursor sits where paragraph 2 starts
    text.compareRegionStarts.side_effect = lambda a, b: 1 if a < b else (0 if a == b else -1)
    start_cursor = MagicMock()
    with patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char") as apply:
        html_import_styles.apply_block_lo_styles(
            MagicMock(), text, start_cursor, ["Heading1", "Standard", "Standard", "Heading2"], end=end,
        )
    assert apply.call_count == 2


def test_apply_block_lo_styles_token_cache_reads_element_names_once():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import_styles

    model = MagicMock()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Heading 1", "Standard"]
    fam.hasByName.return_value = True
    model.getStyleFamilies.return_value.getByName.return_value = fam

    text = MagicMock()
    walk_cur = MagicMock()
    walk_cur.gotoNextParagraph.side_effect = [True, False]
    text.createTextCursorByRange.return_value = walk_cur

    with patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char") as apply:
        html_import_styles.apply_block_lo_styles(
            model, text, MagicMock(), ["Heading1", "Heading1"],
        )
    fam.getElementNames.assert_called_once()
    assert apply.call_count == 2
    assert apply.call_args_list[0].args[2] == "Heading 1"
    assert apply.call_args_list[1].args[2] == "Heading 1"


def test_apply_block_lo_styles_ambiguous_token_resolves_to_standard():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import_styles

    model = MagicMock()
    fam = MagicMock()
    # "Heading1" matches both literal "Heading1" and compact form of "Heading 1" -> ambiguous
    fam.getElementNames.return_value = ["Heading1", "Heading 1", "Standard"]
    fam.hasByName.return_value = True
    model.getStyleFamilies.return_value.getByName.return_value = fam

    text = MagicMock()
    walk_cur = MagicMock()
    walk_cur.gotoNextParagraph.return_value = False
    text.createTextCursorByRange.return_value = walk_cur

    with patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char") as apply:
        html_import_styles.apply_block_lo_styles(
            model, text, MagicMock(), ["Heading1"],
        )
    assert apply.call_args.args[2] == "Standard"


def test_apply_block_lo_styles_unknown_token_resolves_to_standard():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import_styles

    model = MagicMock()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Standard"]
    fam.hasByName.return_value = False
    model.getStyleFamilies.return_value.getByName.return_value = fam

    text = MagicMock()
    walk_cur = MagicMock()
    walk_cur.getPropertyValue.return_value = None
    walk_cur.gotoNextParagraph.return_value = False
    text.createTextCursorByRange.return_value = walk_cur

    with patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char") as apply:
        html_import_styles.apply_block_lo_styles(
            model, text, MagicMock(), ["Nope"],
        )
    assert apply.call_args.args[2] == "Standard"


def test_apply_block_lo_styles_skips_when_style_already_matches():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import_styles

    model = MagicMock()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Heading 1", "Standard"]
    fam.hasByName.return_value = True
    model.getStyleFamilies.return_value.getByName.return_value = fam

    text = MagicMock()
    walk_cur = MagicMock()
    walk_cur.getPropertyValue.return_value = "Heading 1"
    walk_cur.gotoNextParagraph.return_value = False
    text.createTextCursorByRange.return_value = walk_cur

    with patch("plugin.writer.styles.apply_paragraph_style_preserving_direct_char") as apply:
        res = html_import_styles.apply_block_lo_styles(
            model, text, MagicMock(), ["Heading1"],
        )
    apply.assert_not_called()
    assert res == []
    walk_cur.gotoNextParagraph.assert_called_once_with(False)


def test_full_document_in_review_mode_imports_into_a_fresh_paragraph():
    """Imported at the start of the deleted text, the new paragraphs took its character formatting
    (an 18pt bold opening heading spread over the whole document)."""
    from unittest.mock import MagicMock, patch

    from plugin.writer import html_import

    model = MagicMock()
    model.getPropertyValue.return_value = True  # RecordChanges
    text = model.getText.return_value
    leftover = text.createTextCursorByRange.return_value
    leftover.getString.return_value = ""
    with patch.object(html_import, "_insert_mixed_or_plain_html") as imp, \
         patch.object(html_import, "_deletion_author"):
        imp.return_value = ["margin-left"]
        dropped = html_import.replace_full_document(model, MagicMock(), "<p>x</p>")
    assert dropped == ["margin-left"]
    cursor = text.createTextCursor.return_value
    text.insertControlCharacter.assert_called_once_with(cursor, 0, False)
    cursor.goLeft.assert_called_once_with(1, False)
    imp.assert_called_once()
    leftover.goLeft.assert_called_once_with(1, True)
    leftover.setString.assert_called_once_with("")


def test_full_document_without_review_imports_in_place():
    from unittest.mock import MagicMock, patch

    from plugin.writer import html_import

    model = MagicMock()
    model.getPropertyValue.return_value = False
    with patch.object(html_import, "_insert_mixed_or_plain_html") as imp, \
         patch.object(html_import, "_deletion_author"):
        imp.return_value = ["margin-left"]
        dropped = html_import.replace_full_document(model, MagicMock(), "<p>x</p>")
    assert dropped == ["margin-left"]
    model.getText.return_value.insertControlCharacter.assert_not_called()
    imp.assert_called_once()


def test_deletion_author_noop_when_review_authors_missing(monkeypatch):
    """LibrePy omits review_authors; tracked-delete helper must not ImportError."""
    import contextlib
    import sys

    import plugin.writer.html_import as html_import

    monkeypatch.setitem(sys.modules, "plugin.writer.review_authors", None)
    cm = html_import._deletion_author()
    with cm:
        pass
    assert type(cm).__name__ == type(contextlib.nullcontext()).__name__ or hasattr(cm, "__enter__")


@pytest.mark.parametrize(
    "value, value_2",
    [
        pytest.param('<div data-lo-style="Standard">Pelotas.</div><div><p data-lo-style="Standard">Adv</p></div>', "Standard", id="test_styled_div_is_a_slot_and_plain_div_is_not"),
        pytest.param('<p data-lo-style="Standard">a<p data-lo-style="Heading2">b</p>', "Heading2", id="test_omitted_paragraph_end_tag_does_not_nest_the_next_block"),
    ],
)
def test_styled_div_is_a_slot_and_plain_div_is_not(value, value_2):
    from plugin.writer.html_import_styles import extract_block_lo_styles

    _clean, styles, _paras, _skipped = extract_block_lo_styles(
        value)
    assert styles == ["Standard", value_2]


def test_parse_data_lo_para_text_align_center():
    from plugin.writer.html_import_styles import parse_data_lo_para

    props, dropped = parse_data_lo_para("text-align:center")
    assert props == {"text-align": ("ParaAdjust", 3)}
    assert dropped == []


def test_apply_block_lo_styles_returns_failures():
    """apply_block_lo_styles returns property names for dropped, failed, or unreached slots."""
    from unittest.mock import MagicMock
    from plugin.writer import html_import_styles

    model = MagicMock()
    model.getStyleFamilies.return_value.getByName.return_value = None

    text = MagicMock()
    walk_cur = MagicMock()
    # Fails on the first paragraph advance so the second slot is unreached
    walk_cur.gotoNextParagraph.return_value = False

    para_cur = MagicMock()
    para_el = MagicMock()
    # Raises on setPropertyValue so the applied property fails
    para_el.setPropertyValue.side_effect = RuntimeError("locked")
    para_cur.createEnumeration.return_value.hasMoreElements.return_value = True
    para_cur.createEnumeration.return_value.nextElement.return_value = para_el
    text.createTextCursorByRange.side_effect = [walk_cur, para_cur]

    failed = html_import_styles.apply_block_lo_styles(
        model,
        text,
        MagicMock(),
        [None, None],
        paras=["margin-left:2cm; font-size:12pt", "text-align:right"],
    )
    # font-size dropped by parser, margin-left failed at runtime, text-align unreached
    assert failed == ["font-size", "margin-left", "text-align"]
    para_el.setPropertyValue.assert_called()


def test_parse_data_lo_para_margins():
    from plugin.writer.html_import_styles import parse_data_lo_para

    props_left, dropped_left = parse_data_lo_para("margin-left:3.25cm")
    assert props_left == {"margin-left": ("ParaLeftMargin", 3250)}
    assert dropped_left == []

    props_right, dropped_right = parse_data_lo_para("margin-right:1in")
    assert props_right == {"margin-right": ("ParaRightMargin", 2540)}
    assert dropped_right == []


def test_parse_data_lo_para_background_color():
    from plugin.writer.html_import_styles import parse_data_lo_para

    props_hex, dropped_hex = parse_data_lo_para("background-color:#ffe4c4")
    assert props_hex == {"background-color": ("ParaBackColor", 0xFFE4C4)}
    assert dropped_hex == []

    props_trans, dropped_trans = parse_data_lo_para("background-color:transparent")
    assert props_trans == {"background-color": ("ParaBackColor", -1)}
    assert dropped_trans == []


def test_parse_data_lo_para_vertical_margins_and_indent():
    from plugin.writer.html_import_styles import parse_data_lo_para

    props, dropped = parse_data_lo_para("margin-top:0.5cm; margin-bottom:12pt; text-indent:-0.5cm")
    assert props == {
        "margin-top": ("ParaTopMargin", 500),
        "margin-bottom": ("ParaBottomMargin", 423),
        "text-indent": ("ParaFirstLineIndent", -500),
    }
    assert dropped == []


class _FakeLineSpacing:
    """Stands in for com.sun.star.style.LineSpacing (pytest has no real UNO structs)."""

    def __init__(self) -> None:
        self.Mode = 0
        self.Height = 0


@pytest.mark.parametrize(
    ("css", "mode", "height"),
    [
        pytest.param("line-height:150%", 0, 150, id="percent_is_proportional"),
        pytest.param("line-height:115.4%", 0, 115, id="percent_rounds"),
        pytest.param("line-height:normal", 0, 100, id="normal_is_single"),
        pytest.param("line-height:0.5cm", 3, 500, id="length_is_fixed"),
    ],
)
def test_parse_data_lo_para_line_height(monkeypatch, css, mode, height):
    import sys

    from plugin.writer.html_import_styles import parse_data_lo_para

    # setitem on __dict__ (not setattr) so undo removes the attribute instead of leaving a
    # materialised MagicMock child; other tests check that createUnoStruct is absent.
    monkeypatch.setitem(sys.modules["uno"].__dict__, "createUnoStruct", lambda _name: _FakeLineSpacing())
    props, dropped = parse_data_lo_para(css)
    assert dropped == []
    uno_prop, spacing = props["line-height"]
    assert uno_prop == "ParaLineSpacing"
    assert (spacing.Mode, spacing.Height) == (mode, height)


@pytest.mark.parametrize("value", ["tall", "0cm", "-1cm", "%", "0%", "0.0%"])
def test_parse_data_lo_para_bad_line_height_is_dropped(value):
    from plugin.writer.html_import_styles import parse_data_lo_para

    props, dropped = parse_data_lo_para("line-height:%s" % value)
    assert props == {}
    assert dropped == ["line-height"]


def test_every_read_para_name_has_a_writer_or_is_listed_read_only():
    """Every CSS name the read side emits in data-lo-para is either written or listed.

    A property added to the read table without a writer would otherwise only show up as
    one more warning; this makes it a test failure that forces a decision.
    """
    from plugin.writer import html_import_styles, xhtml_style_postprocess

    read_names = {css for _odf, css in xhtml_style_postprocess._FODT_OVERRIDE_ATTRS}
    writers = set(html_import_styles._para_writers())
    read_only = html_import_styles._READ_ONLY_PARA_NAMES
    assert not writers & read_only
    assert read_names - writers - read_only == set()
    assert writers <= read_names, "a writer for a name reads never emit cannot round-trip"


def test_round_trip_read_fodt_autostyle_value():
    from unittest.mock import MagicMock, call
    from plugin.writer import html_import_styles
    from plugin.writer.xhtml_style_postprocess import parse_fodt_autostyles

    fodt = """<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0">
 <style:style style:name="P2" style:family="paragraph" style:parent-style-name="Standard">
  <style:paragraph-properties fo:margin-left="3.25cm" fo:text-indent="0cm" fo:text-align="start"/>
 </style:style>
</office:document>"""
    emitted = parse_fodt_autostyles(fodt)[1]["P2"]
    assert emitted == "margin-left:3.25cm; text-indent:0cm; text-align:left"

    props, dropped = html_import_styles.parse_data_lo_para(emitted)
    assert props == {
        "margin-left": ("ParaLeftMargin", 3250),
        "text-indent": ("ParaFirstLineIndent", 0),
        "text-align": ("ParaAdjust", 0),
    }
    assert dropped == []

    html = f'<p data-lo-para="{emitted}">Clause text</p>'
    clean, styles, paras, skipped = html_import_styles.extract_block_lo_styles(html)
    assert clean == "<p>Clause text</p>"
    assert paras == [emitted]

    model = MagicMock()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Standard"]
    model.getStyleFamilies.return_value.getByName.return_value = fam

    text = MagicMock()
    walk_cur = MagicMock()
    walk_cur.getPropertyValue.return_value = "Standard"
    walk_cur.gotoNextParagraph.return_value = False

    para_cur = MagicMock()
    para_cur.createEnumeration.return_value.hasMoreElements.return_value = True
    text.createTextCursorByRange.side_effect = [walk_cur, para_cur]

    failed = html_import_styles.apply_block_lo_styles(
        model, text, MagicMock(), styles, paras=paras
    )
    assert failed == []
    para_el = para_cur.createEnumeration.return_value.nextElement.return_value
    para_el.setPropertyValue.assert_has_calls([
        call("ParaLeftMargin", 3250),
        call("ParaFirstLineIndent", 0),
        call("ParaAdjust", 0),
    ], any_order=True)


def test_apply_block_lo_styles_background_color_sets_fill_properties_on_paragraph():
    from unittest.mock import MagicMock, call
    from plugin.writer import html_import_styles

    model = MagicMock()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Standard"]
    model.getStyleFamilies.return_value.getByName.return_value = fam

    text = MagicMock()
    walk_cur = MagicMock()
    walk_cur.getPropertyValue.return_value = "Standard"
    walk_cur.gotoNextParagraph.return_value = False

    para_cur = MagicMock()
    text.createTextCursorByRange.side_effect = [walk_cur, para_cur]

    para_el = MagicMock()
    penum = MagicMock()
    penum.hasMoreElements.side_effect = [True, False]
    penum.nextElement.return_value = para_el
    para_cur.createEnumeration.return_value = penum

    solid_style, _none_style = html_import_styles._get_fill_style_enums()

    failed = html_import_styles.apply_block_lo_styles(
        model, text, MagicMock(), [None], paras=["background-color:#ffe4c4"]
    )
    assert failed == []
    para_el.setPropertyValue.assert_has_calls([
        call("FillColor", 0xFFE4C4),
        call("FillStyle", solid_style),
    ])
    # Must not set FillStyle or ParaBackColor on the text cursor.
    cursor_set_props = [c.args[0] for c in para_cur.setPropertyValue.call_args_list]
    assert "FillStyle" not in cursor_set_props
    assert "ParaBackColor" not in cursor_set_props


def test_apply_block_lo_styles_background_color_transparent_sets_fill_none():
    from unittest.mock import MagicMock, call
    from plugin.writer import html_import_styles

    model = MagicMock()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Standard"]
    model.getStyleFamilies.return_value.getByName.return_value = fam

    text = MagicMock()
    walk_cur = MagicMock()
    walk_cur.getPropertyValue.return_value = "Standard"
    walk_cur.gotoNextParagraph.return_value = False

    para_cur = MagicMock()
    text.createTextCursorByRange.side_effect = [walk_cur, para_cur]

    para_el = MagicMock()
    penum = MagicMock()
    penum.hasMoreElements.side_effect = [True, False]
    penum.nextElement.return_value = para_el
    para_cur.createEnumeration.return_value = penum

    _solid_style, none_style = html_import_styles._get_fill_style_enums()

    failed = html_import_styles.apply_block_lo_styles(
        model, text, MagicMock(), [None], paras=["background-color:transparent"]
    )
    assert failed == []
    para_el.setPropertyValue.assert_has_calls([
        call("FillStyle", none_style),
    ])


def test_apply_block_lo_styles_background_color_missing_para_obj_fails():
    from unittest.mock import MagicMock
    from plugin.writer import html_import_styles

    model = MagicMock()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Standard"]
    model.getStyleFamilies.return_value.getByName.return_value = fam

    text = MagicMock()
    walk_cur = MagicMock()
    walk_cur.getPropertyValue.return_value = "Standard"
    walk_cur.gotoNextParagraph.return_value = False

    para_cur = MagicMock()
    text.createTextCursorByRange.side_effect = [walk_cur, para_cur]
    # The paragraph cursor enumerates nothing, so there is no paragraph to format.
    para_cur.createEnumeration.return_value.hasMoreElements.return_value = False

    failed = html_import_styles.apply_block_lo_styles(
        model, text, MagicMock(), [None], paras=["background-color:#ffe4c4"]
    )
    assert failed == ["background-color"]


def test_apply_para_props_split_helper():
    from unittest.mock import MagicMock, call
    from plugin.writer.html_import_styles import _apply_para_props, _get_fill_style_enums

    solid_style, _none_style = _get_fill_style_enums()
    para_obj = MagicMock()

    mapped = {
        "text-align": ("ParaAdjust", 3),
        "background-color": ("ParaBackColor", 0xFFE4C4),
    }
    failed = _apply_para_props(para_obj, mapped)
    assert failed == []
    para_obj.setPropertyValue.assert_has_calls([
        call("ParaAdjust", 3),
        call("FillColor", 0xFFE4C4),
        call("FillStyle", solid_style),
    ], any_order=True)


def test_declared_para_names_flattens_lists_in_order_and_skips_none():
    """Unapplied declarations are reported in document order across all lists; blocks
    without data-lo-para (None) and empty strings contribute nothing."""
    from plugin.writer.html_import_styles import declared_para_names

    assert declared_para_names(
        ["margin-left:1cm; text-align:center", None, ""],
        ["color:#ff0000"],
    ) == ["margin-left", "text-align", "color"]
    assert declared_para_names([]) == []


def test_parse_data_lo_para_duplicate_properties_keeps_first():
    from plugin.writer.html_import_styles import parse_data_lo_para

    props, dropped = parse_data_lo_para("margin-left:1cm; margin-left:2cm; text-align:center; text-align:right")
    assert props == {
        "margin-left": ("ParaLeftMargin", 1000),
        "text-align": ("ParaAdjust", 3),
    }
    assert dropped == []


def test_extractor_handles_data_lo_para():
    from plugin.writer.html_import_styles import extract_block_lo_styles

    html = ('<blockquote data-lo-style="Quotations">'
            '<p data-lo-para="margin-left:1cm">Indented quote</p>'
            '</blockquote>'
            '<p data-lo-para="text-align:right">Only para</p>')
    clean, styles, paras, skipped = extract_block_lo_styles(html)
    assert "data-lo-para" not in clean
    assert "data-lo-style" not in clean
    assert styles == ["Quotations", None]
    assert paras == ["margin-left:1cm", "text-align:right"]
    assert skipped == []

# --- data-less images kept (get_document_content leaves the picture data out) ------------------

_FRAME = ('<div data-lo-style="Standard"><div style="float:left" class="graphic-fr1" id="Assinatura">'
          '<img style="height:1cm" alt="" src=""/></div><div style="clear:both; line-height:0;">&nbsp;</div>'
          'Pelotas.</div>')


def _graphics_doc(*names):
    from unittest.mock import MagicMock

    model = MagicMock()
    graphics = model.getGraphicObjects.return_value
    graphics.hasByName.side_effect = lambda name: name in names
    return model


def test_swap_image_placeholders_turns_each_named_frame_into_a_marker():
    from plugin.writer.image_placeholders import _swap_image_placeholders

    model = _graphics_doc("Assinatura", "Selo")
    content = _FRAME + '<p>Texto <span class="graphic-fr2" id="Selo"><img src=""/></span> depois.</p>'
    swapped, kept = _swap_image_placeholders(model, content)
    assert [k.name for k in kept] == ["Assinatura", "Selo"]
    assert "<img" not in swapped and "clear:both" not in swapped  # no stray empty paragraph either
    assert swapped.count(kept[0].marker) == 1 and "Pelotas." in swapped


def test_swap_image_placeholders_keeps_a_picture_inside_a_heading():
    # The export puts a <p> between the wrapper and the <img> when the picture sits in a heading;
    # the edit was refused as "an image without its data" before.
    from plugin.writer.image_placeholders import _swap_image_placeholders

    content = ('<h1 data-lo-style="Heading1"><a id="a__1423bb33"><span/></a><span class="graphic-fr1" id="Image2">'
               '<p><img alt="" src=""/></p></span>AO DOUTO JUIZO</h1>')
    swapped, kept = _swap_image_placeholders(_graphics_doc("Image2"), content)
    assert [k.name for k in kept] == ["Image2"]
    assert "<img" not in swapped and "<p>" not in swapped and swapped.count(kept[0].marker) == 1
    assert "AO DOUTO JUIZO</h1>" in swapped


def test_swap_image_placeholders_refuses_a_picture_it_cannot_keep():
    """Losing a signature silently is worse than failing before anything changed."""
    import pytest

    from plugin.framework.errors import ToolExecutionError
    from plugin.writer.image_placeholders import _swap_image_placeholders

    with pytest.raises(ToolExecutionError, match="'Assinatura'"):
        _swap_image_placeholders(_graphics_doc(), _FRAME)
    with pytest.raises(ToolExecutionError, match="without its data"):
        _swap_image_placeholders(_graphics_doc(), '<p>Novo <img src=""/> texto</p>')


@pytest.mark.parametrize("markup", [
    "<p>Novo <img src=''/> texto</p>",
    '<p>Novo <img src = ""> texto</p>',
    '<span id="Selo"><img src=\'\'/></span>',
])
def test_swap_image_placeholders_refuses_non_canonical_empty_src(markup):
    import pytest

    from plugin.framework.errors import ToolExecutionError
    from plugin.writer.image_placeholders import _swap_image_placeholders

    with pytest.raises(ToolExecutionError, match="without its data"):
        _swap_image_placeholders(_graphics_doc("Selo"), markup)


def test_swap_image_placeholders_leaves_other_content_alone():
    from unittest.mock import MagicMock

    from plugin.writer.image_placeholders import _swap_image_placeholders

    model = MagicMock()
    content = '<p>Sem imagem</p><img src="data:image/png;base64,AAAA"/>'
    assert _swap_image_placeholders(model, content) == (content, [])
    model.getGraphicObjects.assert_not_called()


def _kept(anchor="AT_PARAGRAPH"):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from plugin.writer.image_placeholders import _KeptImage

    return _KeptImage("waXimg0", "Assinatura", MagicMock(), MagicMock(), SimpleNamespace(value=anchor),
                      MagicMock(), {"TextWrap": 1})


def test_restore_puts_a_copy_where_the_marker_is_when_the_original_went():
    from unittest.mock import MagicMock, patch

    from plugin.writer import image_placeholders

    model = MagicMock()
    found = model.findFirst.return_value
    with patch.object(image_placeholders, "_deleted_by_the_edit", return_value=True):
        image_placeholders._restore_image_placeholders(model, [_kept()])
    picture = model.createInstance.return_value
    found.getText.return_value.insertTextContent.assert_called_once_with(found, picture, True)
    picture.setPropertyValue.assert_called_with("TextWrap", 1)


def test_restore_only_drops_the_marker_when_the_original_stayed():
    """A search replace of a paragraph's text keeps the paragraph and its picture: no duplicate."""
    from unittest.mock import MagicMock, patch

    from plugin.writer import image_placeholders

    for anchor, deleted in (("AT_PARAGRAPH", False), ("AT_PAGE", True)):
        model = MagicMock()
        found = model.findFirst.return_value
        with patch.object(image_placeholders, "_deleted_by_the_edit", return_value=deleted):
            image_placeholders._restore_image_placeholders(model, [_kept(anchor)])
        found.setString.assert_called_once_with("")
        found.getText.return_value.insertTextContent.assert_not_called()


def test_restore_image_placeholders_missing_marker_for_deleted_original_raises():
    from unittest.mock import MagicMock, patch
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import image_placeholders

    model = MagicMock()
    model.findFirst.return_value = None
    kept = [_kept("AT_PARAGRAPH")]
    with patch.object(image_placeholders, "_deleted_by_the_edit", return_value=True):
        with pytest.raises(ToolExecutionError, match="'Assinatura'"):
            image_placeholders._restore_image_placeholders(model, kept)


def test_restore_image_placeholders_missing_marker_for_kept_original_does_not_raise():
    from unittest.mock import MagicMock, patch
    from plugin.writer import image_placeholders

    model = MagicMock()
    model.findFirst.return_value = None
    kept = [_kept("AT_PARAGRAPH")]
    with patch.object(image_placeholders, "_deleted_by_the_edit", return_value=False):
        image_placeholders._restore_image_placeholders(model, kept)
    model.createInstance.assert_not_called()


def test_restore_image_placeholders_missing_marker_resolves_before_mutation():
    from unittest.mock import MagicMock, patch
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import image_placeholders

    model = MagicMock()
    found1 = MagicMock()
    model.findFirst.side_effect = [found1, None]
    kept = [_kept("AT_PARAGRAPH"), _kept("AT_PARAGRAPH")]
    with patch.object(image_placeholders, "_deleted_by_the_edit", return_value=True):
        with pytest.raises(ToolExecutionError, match="'Assinatura'"):
            image_placeholders._restore_image_placeholders(model, kept)
    model.createInstance.assert_not_called()
    found1.setString.assert_not_called()


def test_deleted_by_the_edit():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from plugin.writer import image_placeholders

    gone = MagicMock()
    gone.getAnchor.side_effect = RuntimeError("disposed")
    assert image_placeholders._deleted_by_the_edit(MagicMock(), gone, "Picture 1") is True

    model = MagicMock()
    model.getPropertyValue.return_value = False  # review off: alive means still there
    assert image_placeholders._deleted_by_the_edit(model, MagicMock(), "Picture 1") is False

    # Review on: an inline picture whose anchor sits inside a pending Delete went with it.
    model = MagicMock()
    model.getPropertyValue.return_value = True
    original = MagicMock()
    original.getPropertyValue.return_value = SimpleNamespace(value="AS_CHARACTER")
    text = MagicMock()
    delete = MagicMock()
    delete.getPropertyValue.side_effect = lambda name: (
        "Delete" if name == "RedlineType" else SimpleNamespace(getText=lambda: text))
    text.compareRegionStarts.return_value = 1
    text.compareRegionEnds.return_value = 1
    model.getRedlines.return_value.getCount.return_value = 1
    enum = model.getRedlines.return_value.createEnumeration.return_value
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.side_effect = [delete]
    assert image_placeholders._deleted_by_the_edit(model, original, "Picture 1") is True


def test_deleted_by_the_edit_name_guard(caplog):
    import logging
    import pytest
    from unittest.mock import MagicMock
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer import image_placeholders

    # 1. Name missing from doc.getGraphicObjects() is treated as deleted without calling getAnchor()
    model = MagicMock()
    model.getGraphicObjects.return_value.hasByName.return_value = False
    original = MagicMock()
    original.getAnchor.side_effect = AssertionError("getAnchor must not be called when name is missing")
    assert image_placeholders._deleted_by_the_edit(model, original, "Picture 1") is True
    original.getAnchor.assert_not_called()
    model.getGraphicObjects.return_value.hasByName.assert_called_once_with("Picture 1")

    # 2. Name present in doc.getGraphicObjects() proceeds to check anchor
    model = MagicMock()
    model.getGraphicObjects.return_value.hasByName.return_value = True
    model.getPropertyValue.return_value = False  # review off
    original = MagicMock()
    assert image_placeholders._deleted_by_the_edit(model, original, "Picture 1") is False
    original.getAnchor.assert_called_once()

    # 3. Non-disposal lookup failures are logged and treated as deleted
    model = MagicMock()
    model.getGraphicObjects.side_effect = RuntimeError("collection lookup failed")
    original = MagicMock()
    original.getAnchor.side_effect = AssertionError("getAnchor must not be called on lookup failure")
    with caplog.at_level(logging.WARNING):
        assert image_placeholders._deleted_by_the_edit(model, original, "Picture 1") is True
    original.getAnchor.assert_not_called()
    assert "collection lookup failed" in caplog.text

    # 4. Disposal lookup failure is re-raised
    model = MagicMock()
    model.getGraphicObjects.side_effect = DocumentDisposedError("doc disposed")
    with pytest.raises(DocumentDisposedError):
        image_placeholders._deleted_by_the_edit(model, MagicMock(), "Picture 1")


def test_restore_image_placeholders_passes_name():
    from unittest.mock import MagicMock, patch
    from plugin.writer import image_placeholders

    model = MagicMock()
    kept = [_kept()]
    with patch.object(image_placeholders, "_deleted_by_the_edit", return_value=True) as mock_deleted:
        image_placeholders._restore_image_placeholders(model, kept)
    mock_deleted.assert_called_once_with(model, kept[0].original, kept[0].name)

    # Missing marker case:
    model = MagicMock()
    model.findFirst.return_value = None
    with patch.object(image_placeholders, "_deleted_by_the_edit", return_value=False) as mock_deleted:
        image_placeholders._restore_image_placeholders(model, kept)
    mock_deleted.assert_called_once_with(model, kept[0].original, kept[0].name)


def test_full_document_refuses_before_deleting_anything():
    import pytest

    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import html_import

    model = _graphics_doc()
    with pytest.raises(ToolExecutionError):
        html_import.replace_full_document(model, None, _FRAME)
    model.getText.assert_not_called()


def test_html_to_plain_text_pure_does_not_call_scratch_writer():
    from plugin.writer.html_import import html_to_plain_text

    with patch("plugin.framework.scratch_writer.new_blank_writer") as mock_new_writer:
        assert html_to_plain_text("<b>foo</b>") == "foo"
        mock_new_writer.assert_not_called()

        assert html_to_plain_text("<p>Hello</p><p>World</p>") == "Hello\nWorld"
        mock_new_writer.assert_not_called()

        # Ruby input returns base text only
        assert html_to_plain_text("<ruby>漢字<rt>かんじ</rt></ruby>") == "漢字"
        mock_new_writer.assert_not_called()


def test_html_to_plain_text_plain_string_without_markup_succeeds_without_scratch_doc():
    from plugin.writer.html_import import html_to_plain_text

    with patch("plugin.framework.scratch_writer.new_blank_writer") as mock_new_writer:
        res = html_to_plain_text("  plain text search  ")
        assert res == "plain text search"
        mock_new_writer.assert_not_called()


def test_content_has_markup_cases():
    from plugin.writer.html_import import content_has_markup

    # Unclosed elements and attribute variants are markup
    assert content_has_markup("<h4>Title</h4>") is True
    assert content_has_markup("<h4>Title") is True
    assert content_has_markup('<a href="x">t</a>') is True
    assert content_has_markup('<a href="x">t') is True
    assert content_has_markup("<b class=x>t</b>") is True
    assert content_has_markup("<b class=x>t") is True

    # Unknown angle-bracket prose is not markup
    assert content_has_markup("<spanish>hi</spanish>") is False
    assert content_has_markup("a < b") is False
    assert content_has_markup("List<String>") is False

    # TeX and MathML are markup
    assert content_has_markup(r"formula \(x\)") is True
    assert content_has_markup(r"formula \[x\]") is True
    assert content_has_markup("cost $$10") is True
    assert content_has_markup("<math>...</math>") is True
    assert content_has_markup("The identity $x^2$ holds.") is False

    from plugin.writer.html_import import content_needs_html_import
    assert content_needs_html_import("The identity $x^2$ holds.") is True
    assert content_needs_html_import("costs $5 and $7") is False
    assert content_needs_html_import("R$ 5,00 e R$ 9,00") is False


def test_apply_block_lo_styles_cursor_walk_no_prefix_enumeration():
    from plugin.writer import html_import_styles

    text = MagicMock()
    next_calls = []
    text.createEnumeration.return_value.hasMoreElements.side_effect = lambda: True
    text.createEnumeration.return_value.nextElement.side_effect = lambda: next_calls.append(1) or MagicMock()

    cur = MagicMock()
    walk_cur = MagicMock()
    text.createTextCursorByRange.return_value = walk_cur
    walk_cur.gotoNextParagraph.return_value = False

    html_import_styles.apply_block_lo_styles(MagicMock(), text, cur, ["Heading1"])

    assert len(next_calls) == 0, "createEnumeration should not be called when cursor is provided"
    walk_cur.gotoStartOfParagraph.assert_called_once_with(False)


def test_styled_insert_propagates_style_pass_failure():
    from plugin.writer import html_import, html_import_styles

    cursor = MagicMock()
    cursor.getText.return_value.createTextCursor.return_value.getString.return_value = ""
    with patch.object(html_import_styles, "apply_block_lo_styles", side_effect=RuntimeError("style")), \
         patch.object(html_import, "insert_html_fragment_at_cursor"):
        with pytest.raises(RuntimeError, match="style"):
            html_import._insert_mixed_or_plain_html(
                MagicMock(), MagicMock(), cursor,
                '<p data-lo-style="Heading1">x</p>',
                apply_styles=True,
            )


def test_trailing_paragraph_not_deleted_when_portion_walk_fails():
    """A failed portion inspection is not 'empty'. Deleting can drop a frame."""

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    class _Para:
        def __init__(self, text, boom=None):
            self._text = text
            self._boom = boom

        def getString(self):
            return self._text

        def createEnumeration(self):
            if self._boom is not None:
                raise self._boom
            raise AssertionError("only the empty paragraph is walked")

    deleted = []

    class _Text:
        def getString(self):
            return ""

        def createTextCursor(self):
            cursor = MagicMock()
            cursor.getString.return_value = "Old"
            return cursor

        def createEnumeration(self):
            return _Enum([_Para("kept"), _Para("", boom=RuntimeError("portion"))])

        def createTextCursorByRange(self, _para):
            deleted.append("delete")
            return MagicMock()

    model = MagicMock()
    model.findFirst.return_value = None
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    model.getUndoManager.return_value = undo_mgr

    with patch("plugin.writer.html_import.insert_html_fragment_at_cursor"):
        replace_xtext_with_html(_Text(), "<p>New</p>", model=model)
    assert deleted == []


def test_trailing_paragraph_still_deleted_when_walk_is_empty_text():

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    class _Portion:
        def getString(self):
            return ""

        def getPropertyValue(self, name):
            assert name == "TextPortionType"
            return "Text"

    class _Para:
        def __init__(self, text, walk):
            self._text = text
            self._walk = walk

        def getString(self):
            return self._text

        def createEnumeration(self):
            return _Enum(self._walk)

        def getEnd(self):
            return object()

    deleted = []

    class _Text:
        def getString(self):
            return ""

        def createTextCursor(self):
            cursor = MagicMock()
            cursor.getString.return_value = "Old"
            return cursor

        def createEnumeration(self):
            return _Enum([_Para("kept", []), _Para("", [_Portion()])])

        def createTextCursorByRange(self, _para):
            cursor = MagicMock()
            deleted.append("delete")
            return cursor

    model = MagicMock()
    model.findFirst.return_value = None
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    model.getUndoManager.return_value = undo_mgr

    with patch("plugin.writer.html_import.insert_html_fragment_at_cursor"):
        replace_xtext_with_html(_Text(), "<p>New</p>", model=model)
    assert deleted == ["delete"]


def test_trailing_paragraph_disposal_is_not_success():
    from plugin.framework.errors import DocumentDisposedError

    class _Enum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self):
            return bool(self._items)

        def nextElement(self):
            return self._items.pop(0)

    class _Para:
        def getString(self):
            return ""

        def createEnumeration(self):
            raise type("DisposedException", (Exception,), {})("gone")

    class _Text:
        def getString(self):
            return ""

        def createTextCursor(self):
            cursor = MagicMock()
            cursor.getString.return_value = "Old"
            return cursor

        def createEnumeration(self):
            return _Enum([_Para(), _Para()])

    model = MagicMock()
    model.findFirst.return_value = None
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    undo_title = None

    def enter_ctx(title):
        nonlocal undo_title
        undo_title = title

    undo_mgr.enterUndoContext.side_effect = enter_ctx
    undo_mgr.getAllUndoActionTitles.side_effect = lambda: [undo_title]
    model.getUndoManager.return_value = undo_mgr

    with patch("plugin.writer.html_import.insert_html_fragment_at_cursor"):
        with pytest.raises(DocumentDisposedError):
            replace_xtext_with_html(_Text(), "<p>New</p>", model=model)


def test_uno_read_probes_raise_on_disposal_and_default_otherwise():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.html_import import _cursor_paragraph_has_visible_text, _uno_service_true
    from plugin.writer.html_import_styles import resolve_paragraph_style_token

    class DisposedException(Exception):
        pass

    class RuntimeException(Exception):
        pass

    closed = MagicMock()
    closed.supportsService.side_effect = DisposedException("closed")
    with pytest.raises(DocumentDisposedError):
        _uno_service_true(closed, "com.sun.star.drawing.Shape")

    odd = MagicMock()
    odd.supportsService.side_effect = RuntimeException("uno")
    assert _uno_service_true(odd, "com.sun.star.drawing.Shape") is False

    # Fail safe: an unreadable paragraph still counts as non-empty.
    cursor = MagicMock()
    cursor.getText.side_effect = RuntimeException("uno")
    assert _cursor_paragraph_has_visible_text(cursor) is True

    fam = MagicMock()
    names = ("Heading 1", "Heading 1", "Text Body")
    fam.getElementNames.return_value = names
    fam.hasByName.return_value = True
    assert resolve_paragraph_style_token(fam, "Heading1", element_names=names) == "Heading 1"


def test_resolve_paragraph_style_token_fallback_and_disposal():
    from unittest.mock import MagicMock
    import pytest
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.html_import_styles import resolve_paragraph_style_token

    class DisposedException(Exception):
        pass

    # (i) Case-insensitive fallback
    fam = MagicMock()
    names = ("Heading 1", "Standard")
    fam.getElementNames.return_value = names
    fam.hasByName.return_value = True
    assert resolve_paragraph_style_token(fam, "heading 1", element_names=names) == "Heading 1"

    # (ii) Unknown fallback
    fam.hasByName.return_value = False
    assert resolve_paragraph_style_token(fam, "heading 1", element_names=names) == "Standard"

    # (iii) DisposedException on hasByName
    fam.getElementNames.return_value = []
    fam.hasByName.side_effect = DisposedException("closed")
    with pytest.raises(DocumentDisposedError):
        resolve_paragraph_style_token(fam, "heading 1", element_names=[])


def test_styled_math_insert_styles_from_paragraph_before_insert():
    """The math path moves the cursor past the content, so styling starts after the paragraph before it."""
    from plugin.writer import html_import, html_import_styles

    cursor = MagicMock()
    before = MagicMock()
    start = MagicMock()
    with patch.object(html_import, "_insert_mixed_html_and_math_at_cursor"), \
         patch.object(html_import, "_paragraph_before", return_value=before) as pb, \
         patch.object(html_import, "_paragraph_after", return_value=start) as pa, \
         patch.object(html_import_styles, "apply_block_lo_styles") as apply:
        html_import._insert_mixed_or_plain_html(
            MagicMock(), MagicMock(), cursor,
            '<p data-lo-style="Heading1">Equation \\(x^2\\) here</p>',
            apply_styles=True,
        )
    pb.assert_called_once()
    pa.assert_called_once_with(cursor.getText.return_value, before)
    assert apply.call_args.args[2] is start


def test_paragraph_after_none_is_text_start():
    from plugin.writer import html_import

    text = MagicMock()
    html_import._paragraph_after(text, None)
    text.createTextCursorByRange.assert_called_once_with(text.getStart.return_value)


def test_content_has_block_markup_whitespace_and_inline_tags():
    from plugin.writer.html_import import content_has_block_markup

    assert content_has_block_markup("<p\n style=a>") is True
    assert content_has_block_markup("<p\tx>") is True
    assert content_has_block_markup("<span>") is False
    assert content_has_block_markup("<param>") is False
    assert content_has_block_markup("List<String>") is False
    assert content_has_block_markup("<dl><dt>x</dt></dl>") is True
    assert content_has_block_markup("<figure></figure>") is True
    assert content_has_block_markup("<header>h</header>") is True
    assert content_has_block_markup("<span>a<br>b</span>") is False


def test_collect_range_portions_magic_mock_returns_immediately():
    from plugin.writer.html_import import _collect_range_portions

    mock_range = MagicMock()
    has_special, portions = _collect_range_portions(mock_range)
    assert has_special is False
    assert portions == []


def make_paragraph_edge_range(is_start: bool, is_end: bool) -> MagicMock:
    """Mock an XTextRange whose start and end cursors report paragraph boundary status."""
    start_cur = MagicMock()
    start_cur.isStartOfParagraph.return_value = is_start
    end_cur = MagicMock()
    end_cur.isEndOfParagraph.return_value = is_end
    text = MagicMock()
    text.createTextCursorByRange.side_effect = lambda pos: start_cur if pos == "start" else end_cur
    rng = MagicMock()
    rng.getText.return_value = text
    rng.getStart.return_value = "start"
    rng.getEnd.return_value = "end"
    rng.insertDocumentFromURL = MagicMock()
    return rng


def test_lo_style_for_replaced_range_predicates():
    from plugin.writer.html_import import _lo_style_for_replaced_range

    model = MagicMock()
    content = '<p data-lo-style="Heading2">content</p>'

    with patch("plugin.writer.record_changes.is_recording_changes", return_value=False):
        # Both edges true -> apply True, note None
        apply, note = _lo_style_for_replaced_range(model, content, make_paragraph_edge_range(True, True))
        assert apply is True
        assert note is None

        # Start only -> apply False, note partial
        apply, note = _lo_style_for_replaced_range(model, content, make_paragraph_edge_range(True, False))
        assert apply is False
        assert note == "STYLE_IGNORED_PARTIAL_MATCH"

        # End only -> apply False, note partial
        apply, note = _lo_style_for_replaced_range(model, content, make_paragraph_edge_range(False, True))
        assert apply is False
        assert note == "STYLE_IGNORED_PARTIAL_MATCH"

        # Neither -> apply False, note partial
        apply, note = _lo_style_for_replaced_range(model, content, make_paragraph_edge_range(False, False))
        assert apply is False
        assert note == "STYLE_IGNORED_PARTIAL_MATCH"

        para_content = '<p data-lo-para="text-align:center">content</p>'
        # Both edges true -> apply True, note None
        apply, note = _lo_style_for_replaced_range(model, para_content, make_paragraph_edge_range(True, True))
        assert apply is True
        assert note is None

        # Start only -> apply False, note STYLE_IGNORED_PARTIAL_MATCH
        apply, note = _lo_style_for_replaced_range(model, para_content, make_paragraph_edge_range(True, False))
        assert apply is False
        assert note == "STYLE_IGNORED_PARTIAL_MATCH"

        # End only -> apply False, note STYLE_IGNORED_PARTIAL_MATCH
        apply, note = _lo_style_for_replaced_range(model, para_content, make_paragraph_edge_range(False, True))
        assert apply is False
        assert note == "STYLE_IGNORED_PARTIAL_MATCH"

        # Neither -> apply False, note STYLE_IGNORED_PARTIAL_MATCH
        apply, note = _lo_style_for_replaced_range(model, para_content, make_paragraph_edge_range(False, False))
        assert apply is False
        assert note == "STYLE_IGNORED_PARTIAL_MATCH"


def test_lo_style_for_replaced_range_recording():
    from plugin.writer.html_import import _lo_style_for_replaced_range

    model = MagicMock()
    content = '<p data-lo-style="Heading2">content</p>'

    with patch("plugin.writer.record_changes.is_recording_changes", return_value=True):
        # Edges true, recording true -> apply False, recording sentence
        apply, note = _lo_style_for_replaced_range(model, content, make_paragraph_edge_range(True, True))
        assert apply is False
        assert note == "STYLE_IGNORED_RECORDING"

        # Geometry checked first: partial match while recording returns partial note
        apply, note = _lo_style_for_replaced_range(model, content, make_paragraph_edge_range(False, True))
        assert apply is False
        assert note == "STYLE_IGNORED_PARTIAL_MATCH"

        para_content = '<p data-lo-para="text-align:center">content</p>'
        # Para only while recording returns False, STYLE_IGNORED_RECORDING
        apply, note = _lo_style_for_replaced_range(model, para_content, make_paragraph_edge_range(True, True))
        assert apply is False
        assert note == "STYLE_IGNORED_RECORDING"

        apply, note = _lo_style_for_replaced_range(model, para_content, make_paragraph_edge_range(False, True))
        assert apply is False
        assert note == "STYLE_IGNORED_PARTIAL_MATCH"


def test_lo_style_for_replaced_range_non_block_tokens():
    from plugin.writer.html_import import _lo_style_for_replaced_range

    model = MagicMock()
    rng = MagicMock()

    # Span token -> (False, None), range cursor methods not probed
    apply, note = _lo_style_for_replaced_range(model, '<span data-lo-style="Heading2">x</span>', rng)
    assert apply is False
    assert note is None
    rng.getText.assert_not_called()

    # Body-text mention -> (False, None)
    apply, note = _lo_style_for_replaced_range(model, '<p>The attribute data-lo-style is cool</p>', rng)
    assert apply is False
    assert note is None
    rng.getText.assert_not_called()

    # Table-only token -> (False, None)
    apply, note = _lo_style_for_replaced_range(
        model, '<table><tr><td data-lo-style="Heading2">cell</td></tr></table>', rng
    )
    assert apply is False
    assert note is None
    rng.getText.assert_not_called()

    # Span para -> (False, None), range cursor methods not probed
    rng.reset_mock()
    apply, note = _lo_style_for_replaced_range(model, '<span data-lo-para="text-align:center">x</span>', rng)
    assert apply is False
    assert note is None
    rng.getText.assert_not_called()

    # Table-only para -> (False, None)
    rng.reset_mock()
    apply, note = _lo_style_for_replaced_range(
        model, '<table><tr><td data-lo-para="text-align:center">cell</td></tr></table>', rng
    )
    assert apply is False
    assert note is None
    rng.getText.assert_not_called()


def test_styled_insert_epilogue_cursor_move():
    from plugin.writer import html_import

    model = MagicMock()
    model_text = MagicMock()
    model.getText.return_value = model_text

    cursor = MagicMock()
    cursor_text = MagicMock()
    cursor.getText.return_value = cursor_text

    parked = MagicMock()
    cursor_text.createTextCursorByRange.return_value = parked

    with patch.object(html_import, "insert_html_fragment_at_cursor"), \
         patch("plugin.writer.html_import_styles.apply_block_lo_styles"), \
         patch.object(html_import, "_apply_ruby_and_restore_fields"):
        html_import._insert_mixed_or_plain_html(
            model, MagicMock(), cursor,
            '<p data-lo-style="Heading1">Heading</p>',
            apply_styles=True,
        )

    # cursor.gotoRange received the parked cursor's start
    cursor.gotoRange.assert_called_with(parked.getStart.return_value, False)
    # model.getText().createTextCursor is not called
    model_text.createTextCursor.assert_not_called()

@pytest.mark.parametrize(
    ("html", "expected_count", "expected_headings"),
    [
        # Unclosed tag closes when the next paragraph begins.
        ("<h1>Title<p>Next", 2, [{"level": 1, "text": "Title"}]),
        # data-lo-style to non-heading style overrides the heading tag.
        ('<h1 data-lo-style="Quotations">Title</h1>', 1, []),
        # Heading11 is beyond LibreOffice heading level 1-10 range.
        ('<p data-lo-style="Heading11">Title</p>', 1, []),
        # Shared first child slot: outer style token overrides inner tag level.
        ('<blockquote data-lo-style="Heading2"><h1>Title</h1></blockquote>', 1, [{"level": 2, "text": "Title"}]),
        ('<li data-lo-style="Heading3"><h1>Title</h1></li>', 1, [{"level": 3, "text": "Title"}]),
        ('<blockquote data-lo-style="Quotations"><h1>Title</h1></blockquote>', 1, []),
        # Entity references decoded and nested markup text combined.
        ("<h1>Tom &amp; <b>Jerry</b></h1>", 1, [{"level": 1, "text": "Tom & Jerry"}]),
        # Heading text longer than 120 chars is truncated with an ellipsis.
        ("<h1>" + "A" * 130 + "</h1>", 1, [{"level": 1, "text": "A" * 120 + "…"}]),
        # Styled div counts as a paragraph slot.
        ('<div data-lo-style="Heading1">Hello</div>', 1, [{"level": 1, "text": "Hello"}]),
        # <br> inside paragraph separates text with whitespace.
        ('<p data-lo-style="Heading1">Hello<br>World</p>', 1, [{"level": 1, "text": "Hello World"}]),
        # Plain text without HTML tags: blank lines delimit paragraphs.
        ("Hello\n\nWorld", 2, []),
        ("Hello\nWorld", 1, []),
        # Ruby markup without math is stripped to base text.
        ("<ruby>漢字<rt>かんじ</rt></ruby>", 1, []),
        # Encoded HTML tags in prose are decoded before summarization.
        ("<p>Intro</p>&lt;h2&gt;Sub&lt;/h2&gt;", 2, [{"level": 2, "text": "Sub"}]),
    ],
)
def test_summarize_written_blocks_fixtures(html: str, expected_count: int, expected_headings: list[dict[str, Any]]) -> None:
    from plugin.writer.html_import_styles import summarize_written_blocks

    summary = summarize_written_blocks(html)
    assert summary["paragraph_count"] == expected_count
    assert summary["headings"] == expected_headings


def test_summarize_written_blocks_drops_specified_heading_styles():
    from plugin.writer.html_import_styles import summarize_written_blocks

    html = '<p data-lo-style="Heading1">One</p><p data-lo-style="Heading2">Two</p>'
    summary = summarize_written_blocks(html, drop_heading_styles={"Heading1"})
    assert summary["paragraph_count"] == 2
    assert summary["headings"] == [{"level": 2, "text": "Two"}]


def test_summarize_written_blocks_matches_extractor_slot_counts():
    """Verify summarize_written_blocks paragraph counts match _BlockLoStyleExtractor.styles."""
    from plugin.writer.html_import_styles import _BlockLoStyleExtractor, summarize_written_blocks

    fixtures = [
        "<blockquote><p>Quote</p></blockquote>",
        "<ul><li>Item 1</li><li>Item 2</li></ul>",
        "<table><tr><td>Cell</td></tr></table><p>After</p>",
        '<p data-lo-style="Heading 10">Ten</p>',
    ]
    for html in fixtures:
        ex = _BlockLoStyleExtractor()
        ex.feed(html)
        ex.close()
        summary = summarize_written_blocks(html)
        assert summary["paragraph_count"] == len(ex.styles), f"Mismatch for {html}"


def test_insert_mixed_html_and_math_reports_every_attempt():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import

    model = MagicMock()
    ctx = MagicMock()
    cursor = MagicMock()

    content = r"<p>Start</p> \(x+1\) between $$y=2$$ and <math display='block'><mi>z</mi></math> end"

    def fake_tex(ctx, text, *, display_block=False):
        if text == "x+1":
            return MagicMock(ok=True, starmath="x + 1", error_message=None)
        return MagicMock(ok=False, starmath=None, error_message="tex syntax error")

    def fake_mathml(ctx, text):
        return MagicMock(ok=True, starmath="z", error_message=None)

    with patch.object(html_import, "convert_latex_to_starmath", side_effect=fake_tex), \
         patch.object(html_import, "convert_mathml_to_starmath", side_effect=fake_mathml), \
         patch.object(html_import, "insert_writer_math_formula") as mock_formula, \
         patch.object(html_import, "insert_html_fragment_at_cursor"):
        attempts = html_import._insert_mixed_html_and_math_at_cursor(model, ctx, cursor, content)

    assert attempts == [
        {"kind": "tex", "display_block": False, "snippet": "x+1", "ok": True},
        {"kind": "tex", "display_block": True, "snippet": "y=2", "ok": False, "error": "tex syntax error"},
        {"kind": "mathml", "display_block": True, "snippet": "<math display='block'><mi>z</mi></math>", "ok": True},
    ]
    assert mock_formula.call_count == 2
    cursor.getText.return_value.insertString.assert_called_once_with(
        cursor, "[Math import failed] y=2", False
    )


def test_mixed_inserters_report_math_and_preserve_returns():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import

    model = MagicMock()
    model.getPropertyValue.return_value = False
    cursor = MagicMock()
    model.getText.return_value.createTextCursor.return_value = cursor
    text_range = MagicMock()
    text_range.getText.return_value = MagicMock()
    ctx = MagicMock()

    fake_attempt = [{"kind": "tex", "display_block": False, "snippet": "a", "ok": True}]

    def fake_mixed_math(mod, c, cur, unescaped):
        return list(fake_attempt)

    # 1. replace_full_document returns WriteReport with math
    with patch.object(html_import, "_insert_mixed_html_and_math_at_cursor", side_effect=fake_mixed_math), \
         patch.object(html_import, "html_fragment_contains_mixed_math", return_value=True):
        ret_full = html_import.replace_full_document(model, ctx, r"<p>\(a\)</p>")
    assert ret_full.unapplied_para == []
    assert ret_full.math == fake_attempt

    # 2. insert_content_at_position(..., "end") returns WriteReport with math
    with patch.object(html_import, "_insert_mixed_html_and_math_at_cursor", side_effect=fake_mixed_math), \
         patch.object(html_import, "html_fragment_contains_mixed_math", return_value=True):
        ret_end = html_import.insert_content_at_position(model, ctx, r"<p>\(a\)</p>", "end")
    assert ret_end.unapplied_para == []
    assert ret_end.math == fake_attempt

    # 3. replace_single_range_with_content returns WriteReport with math
    with patch.object(html_import, "_insert_mixed_html_and_math_at_cursor", side_effect=fake_mixed_math), \
         patch("plugin.writer.html_import.raise_if_range_hosts_nested_table"), \
         patch.object(html_import, "html_fragment_contains_mixed_math", return_value=True):
        ret_range = html_import.replace_single_range_with_content(
            model, text_range, r"<p>\(a\)</p>", ctx
        )
    assert ret_range.unapplied_para == []
    assert ret_range.math == fake_attempt


def test_replace_single_range_returns_failures_and_inline_skipped():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import, html_import_styles

    model = MagicMock()
    ctx = MagicMock()
    text_range = MagicMock()
    text_obj = MagicMock()
    text_range.getText.return_value = text_obj
    cursor = MagicMock()
    text_obj.createTextCursorByRange.return_value = cursor

    # Block replacement returns failures from _insert_mixed_or_plain_html
    fake_report = html_import_styles.WriteReport(unapplied_para=["font-size"])
    with patch("plugin.writer.html_import.raise_if_range_hosts_nested_table"), \
         patch.object(html_import, "_insert_mixed_or_plain_html", return_value=fake_report) as mock_ins, \
         patch.object(html_import, "_lo_style_for_replaced_range", return_value=(True, None)):
        rep = html_import.replace_single_range_with_content(
            model, text_range, '<p data-lo-para="font-size:12pt">text</p>', ctx
        )
    assert rep.unapplied_para == ["font-size"]
    assert rep.style_skips == []
    mock_ins.assert_called_once()

    # Inline replacement returns property names from skipped inline tags
    cursor.getPropertyValue.return_value = "Standard"
    with patch("plugin.writer.html_import.raise_if_range_hosts_nested_table"), \
         patch.object(html_import, "insert_html_fragment_at_cursor"), \
         patch("plugin.writer.record_changes.is_recording_changes", return_value=False):
        rep_inline = html_import.replace_single_range_with_content(
            model, text_range, '<span data-lo-para="text-align:center; font-size:10pt">inline</span>', ctx
        )
    assert rep_inline.unapplied_para == ["text-align", "font-size"]
    assert rep_inline.style_skips == []


def test_plain_html_leaves_math_empty():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import

    model = MagicMock()
    cursor = MagicMock()
    ctx = MagicMock()

    with patch.object(html_import, "insert_html_fragment_at_cursor"):
        report = html_import._insert_mixed_or_plain_html(
            model, ctx, cursor, "<p>hi</p>"
        )
    assert report.unapplied_para == []
    assert report.math == []


def test_drop_absorb_paragraph_props_resets_extended_properties() -> None:
    """_drop_absorb_paragraph_props must pass CLEARABLE_PARA_PROPERTIES plus extra direct properties."""
    from plugin.writer.html_import import _drop_absorb_paragraph_props
    from plugin.writer.styles import CLEARABLE_PARA_PROPERTIES

    cursor = MagicMock()
    _drop_absorb_paragraph_props(cursor)

    cursor.setPropertiesToDefault.assert_called_once()
    props = cursor.setPropertiesToDefault.call_args[0][0]

    assert props[: len(CLEARABLE_PARA_PROPERTIES)] == CLEARABLE_PARA_PROPERTIES
    extra_names = (
        "ParaLineSpacing",
        "ParaContextMargin",
        "ParaTabStops",
        "LeftBorder",
        "RightBorder",
        "TopBorder",
        "BottomBorder",
    )
    for name in extra_names:
        assert name in props
        assert name not in CLEARABLE_PARA_PROPERTIES

