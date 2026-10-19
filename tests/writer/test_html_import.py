
import html as html_mod

import pytest
from unittest.mock import MagicMock, patch

from plugin.writer.html_import import (
    _apply_ruby_spans,
    _ensure_html_linebreaks,
    content_has_markup,
    extract_and_strip_ruby,
    replace_xtext_with_html,
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
    _apply_ruby_spans(text, spans)
    painted = [cursor.ruby for cursor in text.cursors if cursor.ruby]
    assert painted == [(len("漢字と"), len("漢字と漢字"), "かんじ")]


def test_ruby_offset_is_relative_to_the_imported_suffix():
    """skip_chars is existing text. The span offset is inside the inserted suffix."""
    prefix = "前置き"
    suffix = "漢字と漢字"
    text = _RubyText(prefix + suffix)
    spans = [("漢字", "かんじ", True, len("漢字と"))]
    _apply_ruby_spans(text, spans, skip_chars=len(prefix))
    painted = [cursor.ruby for cursor in text.cursors if cursor.ruby]
    start = len(prefix) + len("漢字と")
    assert painted == [(start, start + len("漢字"), "かんじ")]


def test_ruby_readings_stay_on_their_own_repeated_base():
    html = "<p><ruby>漢字<rt>いち</rt></ruby><ruby>漢字<rt>に</rt></ruby></p>"
    _unused, spans = extract_and_strip_ruby(html)
    assert [span[1] for span in spans] == ["いち", "に"]
    assert [span[3] for span in spans] == [0, len("漢字")]
    text = _RubyText("漢字漢字")
    _apply_ruby_spans(text, spans)
    painted = [cursor.ruby for cursor in text.cursors if cursor.ruby]
    assert painted == [(0, 2, "いち"), (2, 4, "に")]


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
    _apply_ruby_spans(text, spans)
    painted = [c.ruby for c in text.cursors if c.ruby]
    assert painted == expected_painted
    assert len(text.cursors) <= 205


def test_mixed_math_segments_produce_separate_ruby_spans_without_tex_source():
    from plugin.writer.html_import import extract_and_strip_ruby, segment_html_with_mixed_math

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
    out = _ensure_html_linebreaks(once)
    assert "<p>hello</p>" not in out
    assert "&lt;p&gt;hello&lt;/p&gt;" in out

    inside = html_mod.unescape("<p>see &amp;lt;b&amp;gt;x&amp;lt;/b&amp;gt;</p>")
    wrapped = _ensure_html_linebreaks(inside)
    assert "<b>" not in wrapped
    assert "&lt;b&gt;x&lt;/b&gt;" in wrapped
    assert "<p>see" in wrapped


def test_ensure_html_linebreaks_escapes_prose_angle_brackets():
    out = _ensure_html_linebreaks("<p>List<String> and 2 < 3</p>")
    assert "List&lt;String&gt;" in out
    assert "2 &lt; 3" in out
    assert "<p>" in out and "</p>" in out


def test_decode_complete_character_references_only_decodes_with_semicolon():
    from plugin.writer.html_import import _decode_complete_character_references

    assert _decode_complete_character_references("&sect 2o &amp;lt;") == "&sect 2o &lt;"
    assert _decode_complete_character_references("&amp; &quot; &#36; &#x24;") == '& " $ $'
    assert _decode_complete_character_references("&not covered") == "&not covered"


def test_insert_content_entry_points_decode_complete_entities():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import

    raw = "<p>&sect 2o &amp;lt;b&amp;gt;x&amp;lt;/b&amp;gt;</p>"
    expected = "<p>&sect 2o &lt;b&gt;x&lt;/b&gt;</p>"

    model = MagicMock()
    model.getPropertyValue.return_value = False

    with patch.object(html_import, "_insert_mixed_or_plain_html") as mock_mixed:
        html_import._replace_full_document(model, MagicMock(), raw)
        assert mock_mixed.call_args.args[3] == expected

    cursor = MagicMock()
    model.getText.return_value.createTextCursor.return_value = cursor
    with patch.object(html_import, "_insert_mixed_or_plain_html") as mock_mixed:
        html_import._insert_content_at_position(model, MagicMock(), raw, "beginning")
        assert mock_mixed.call_args.args[3] == expected

    text_range = MagicMock()
    text_range.getText.return_value = MagicMock()
    with patch.object(html_import, "_insert_mixed_or_plain_html") as mock_mixed, \
         patch("plugin.writer.table_nesting.raise_if_range_hosts_nested_table"):
        html_import._replace_single_range_with_content(model, text_range, raw, MagicMock())
        assert mock_mixed.call_args.args[3] == expected


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



def test_replace_xtext_with_html_restores_text_when_import_fails():
    text = MagicMock()
    cursor = MagicMock()
    text.createTextCursor.return_value = cursor
    cursor.getString.return_value = "Old header"
    with patch(
        "plugin.writer.html_import.insert_html_fragment_at_cursor",
        side_effect=RuntimeError("import failed"),
    ):
        with pytest.raises(RuntimeError, match="import failed"):
            replace_xtext_with_html(text, "<p>New</p>")
    assert cursor.setString.call_args_list[0].args == ("",)
    assert cursor.setString.call_args_list[-1].args == ("Old header",)


def test_replace_xtext_with_html_leaves_cleared_text_when_import_succeeds():
    text = MagicMock()
    cursor = MagicMock()
    text.createTextCursor.return_value = cursor
    cursor.getString.return_value = "Old header"
    with patch("plugin.writer.html_import.insert_html_fragment_at_cursor") as insert:
        replace_xtext_with_html(text, "<p>New</p>")
    insert.assert_called_once()
    assert [call.args for call in cursor.setString.call_args_list] == [("",)]


def test_replace_xtext_with_html_undo_rollback_on_import_failure():
    from plugin.writer.html_import import replace_xtext_with_html

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


def test_replace_xtext_with_html_undo_rollback_on_field_restore_failure():
    from plugin.writer.html_import import replace_xtext_with_html

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
         patch("plugin.writer.html_import._apply_ruby_spans"), \
         patch("plugin.writer.html_import._restore_field_placeholders", side_effect=RuntimeError("field failed")):
        with pytest.raises(RuntimeError, match="field failed"):
            replace_xtext_with_html(text, "<p>New</p>", model=model)

    undo_mgr.enterUndoContext.assert_called_once()
    undo_mgr.leaveUndoContext.assert_called_once()
    undo_mgr.undo.assert_called_once()


def test_replace_xtext_with_html_undo_manager_locked_refuses_clear():
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer.html_import import replace_xtext_with_html

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
    """content.py decodes only complete references; a second html.unescape turned "&sect 2o"
    into "§ 2o"."""
    from unittest.mock import MagicMock, patch

    from plugin.writer import html_import

    cursor = MagicMock()
    with patch.object(html_import, "insert_html_fragment_at_cursor"):
        html_import.insert_inline_at_cursor(MagicMock(), MagicMock(), cursor, "&sect 2o ")
    cursor.getText.return_value.insertString.assert_called_once_with(cursor, "&sect 2o ", False)


def test_insert_inline_at_cursor_paints_ruby_instead_of_gluing_the_reading():
    from unittest.mock import MagicMock, patch

    from plugin.writer import html_import

    cursor = MagicMock()
    with patch.object(html_import, "insert_html_fragment_at_cursor") as frag, \
         patch.object(html_import, "_prefix_char_count", return_value=7), \
         patch.object(html_import, "_apply_ruby_spans") as paint:
        html_import.insert_inline_at_cursor(MagicMock(), MagicMock(), cursor, "<ruby>漢字<rt>かんじ</rt></ruby>")
    assert frag.call_args.args[1] == "漢字"
    assert paint.call_args.args[1] == [("漢字", "かんじ", True, 0)]
    assert paint.call_args.kwargs["skip_chars"] == 7


def test_insert_inline_at_cursor_skips_prefix_count_without_ruby():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import

    cursor = MagicMock()
    with patch.object(html_import, "_prefix_char_count") as count, \
         patch.object(html_import, "insert_html_fragment_at_cursor"):
        html_import._insert_inline_at_cursor(MagicMock(), MagicMock(), cursor, "<b>x</b>")
    count.assert_not_called()


def test_insert_mixed_or_plain_html_skips_prefix_count_without_ruby():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import

    cursor = MagicMock()
    with patch.object(html_import, "_prefix_char_count") as count, \
         patch.object(html_import, "_insert_starwriter_html_at_cursor"):
        html_import._insert_mixed_or_plain_html(MagicMock(), MagicMock(), cursor, "<p>hi</p>")
    count.assert_not_called()


def test_insert_mixed_html_and_math_skips_prefix_count_without_ruby():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import

    cursor = MagicMock()
    with patch.object(html_import, "_prefix_char_count") as count, \
         patch.object(html_import, "_insert_starwriter_html_at_cursor"), \
         patch("plugin.writer.math.html_math_segment.html_fragment_contains_mixed_math", return_value=True):
        html_import._insert_mixed_html_and_math_at_cursor(
            MagicMock(), MagicMock(), cursor, "<p>text before</p> \\(x^2\\) <p>text after</p>"
        )
    count.assert_not_called()


def test_insert_mixed_html_and_math_counts_prefix_when_segment_contains_ruby():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import

    cursor = MagicMock()
    calls = []
    with patch.object(html_import, "_prefix_char_count", side_effect=lambda t, c: calls.append("count") or 5) as count, \
         patch.object(html_import, "_insert_starwriter_html_at_cursor", side_effect=lambda *a, **k: calls.append("insert")), \
         patch.object(html_import, "_apply_ruby_spans") as paint, \
         patch("plugin.writer.math.html_math_segment.html_fragment_contains_mixed_math", return_value=True):
        html_import._insert_mixed_html_and_math_at_cursor(
            MagicMock(), MagicMock(), cursor, "<p><ruby>漢字<rt>かんじ</rt></ruby></p> \\(x^2\\)"
        )
    count.assert_called_once()
    assert calls == ["count", "insert"]
    assert paint.call_args.kwargs["skip_chars"] == 5


def test_nested_blocks_share_one_style_slot():
    """<blockquote><p> and <li><p> are ONE Writer paragraph: two slots shifted every later style
    ("2. DO DIREITO" lost its heading) and the leftover ones hit old text (relatos #35/#40)."""
    from plugin.writer.html_import import _extract_block_lo_styles

    html = ('<h1 data-lo-style="Heading1">T</h1>'
            '<blockquote><p data-lo-style="Quotations">q</p></blockquote>'
            '<ul><li><p data-lo-style="Standard">a</p></li><li><p data-lo-style="Standard">b</p></li></ul>'
            '<h2 data-lo-style="Heading2">D</h2>')
    clean, styles = _extract_block_lo_styles(html)
    assert styles == ["Heading1", "Quotations", "Standard", "Standard", "Heading2"]
    assert "data-lo-style" not in clean


def test_second_block_inside_a_block_gets_its_own_slot():
    from plugin.writer.html_import import _extract_block_lo_styles

    _clean, styles = _extract_block_lo_styles(
        '<li data-lo-style="List"><p>a</p><p data-lo-style="Standard">b</p></li><p data-lo-style="Standard">c</p>')
    assert styles == ["List", "Standard", "Standard"]


def test_block_styles_stop_at_the_end_of_the_import():
    """More styles than imported paragraphs must not style the text after the import (the old
    text a full_document had just deleted: its Delete turned into a Format change)."""
    from unittest.mock import MagicMock, patch

    from plugin.writer import html_import

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
    from plugin.writer import format as format_mod
    with patch.object(format_mod, "apply_paragraph_style_preserving_direct_char") as apply:
        html_import._apply_block_lo_styles(
            MagicMock(), text, start_cursor, ["Heading1", "Standard", "Standard", "Heading2"], end=end,
        )
    assert apply.call_count == 2


def test_apply_block_lo_styles_token_cache_reads_element_names_once():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import

    model = MagicMock()
    fam = MagicMock()
    fam.getElementNames.return_value = ["Heading 1", "Standard"]
    fam.hasByName.return_value = True
    model.getStyleFamilies.return_value.getByName.return_value = fam

    text = MagicMock()
    walk_cur = MagicMock()
    walk_cur.gotoNextParagraph.side_effect = [True, False]
    text.createTextCursorByRange.return_value = walk_cur

    from plugin.writer import format as format_mod
    with patch.object(format_mod, "apply_paragraph_style_preserving_direct_char") as apply:
        html_import._apply_block_lo_styles(
            model, text, MagicMock(), ["Heading1", "Heading1"],
        )
    fam.getElementNames.assert_called_once()
    assert apply.call_count == 2
    assert apply.call_args_list[0].args[2] == "Heading 1"
    assert apply.call_args_list[1].args[2] == "Heading 1"


def test_apply_block_lo_styles_ambiguous_token_resolves_to_standard():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import

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

    from plugin.writer import format as format_mod
    with patch.object(format_mod, "apply_paragraph_style_preserving_direct_char") as apply:
        html_import._apply_block_lo_styles(
            model, text, MagicMock(), ["Heading1"],
        )
    assert apply.call_args.args[2] == "Standard"


def test_full_document_in_review_mode_imports_into_a_fresh_paragraph():
    """Imported at the start of the deleted text, the new paragraphs took its character formatting
    (an 18pt bold opening heading spread over the whole document)."""
    from unittest.mock import MagicMock, patch

    from plugin.writer import format as format_mod
    from plugin.writer import html_import

    model = MagicMock()
    model.getPropertyValue.return_value = True  # RecordChanges
    text = model.getText.return_value
    leftover = text.createTextCursorByRange.return_value
    leftover.getString.return_value = ""
    with patch.object(html_import, "_insert_mixed_or_plain_html") as imp, \
         patch.object(format_mod, "_deletion_author"):
        html_import.replace_full_document(model, MagicMock(), "<p>x</p>")
    cursor = text.createTextCursor.return_value
    text.insertControlCharacter.assert_called_once_with(cursor, 0, False)
    cursor.goLeft.assert_called_once_with(1, False)
    imp.assert_called_once()
    leftover.goLeft.assert_called_once_with(1, True)
    leftover.setString.assert_called_once_with("")


def test_full_document_without_review_imports_in_place():
    from unittest.mock import MagicMock, patch

    from plugin.writer import format as format_mod
    from plugin.writer import html_import

    model = MagicMock()
    model.getPropertyValue.return_value = False
    with patch.object(html_import, "_insert_mixed_or_plain_html") as imp, \
         patch.object(format_mod, "_deletion_author"):
        html_import.replace_full_document(model, MagicMock(), "<p>x</p>")
    model.getText.return_value.insertControlCharacter.assert_not_called()
    imp.assert_called_once()


@pytest.mark.parametrize(
    "value, value_2",
    [
        pytest.param('<div data-lo-style="Standard">Pelotas.</div><div><p data-lo-style="Standard">Adv</p></div>', "Standard", id="test_styled_div_is_a_slot_and_plain_div_is_not"),
        pytest.param('<p data-lo-style="Standard">a<p data-lo-style="Heading2">b</p>', "Heading2", id="test_omitted_paragraph_end_tag_does_not_nest_the_next_block"),
    ],
)
def test_styled_div_is_a_slot_and_plain_div_is_not(value, value_2):
    from plugin.writer.html_import import _extract_block_lo_styles

    _clean, styles = _extract_block_lo_styles(
        value)
    assert styles == ["Standard", value_2]

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
    from plugin.writer.html_import import _swap_image_placeholders

    model = _graphics_doc("Assinatura", "Selo")
    content = _FRAME + '<p>Texto <span class="graphic-fr2" id="Selo"><img src=""/></span> depois.</p>'
    swapped, kept = _swap_image_placeholders(model, content)
    assert [k.name for k in kept] == ["Assinatura", "Selo"]
    assert "<img" not in swapped and "clear:both" not in swapped  # no stray empty paragraph either
    assert swapped.count(kept[0].marker) == 1 and "Pelotas." in swapped


def test_swap_image_placeholders_keeps_a_picture_inside_a_heading():
    # The export puts a <p> between the wrapper and the <img> when the picture sits in a heading;
    # the edit was refused as "an image without its data" before.
    from plugin.writer.html_import import _swap_image_placeholders

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
    from plugin.writer.html_import import _swap_image_placeholders

    with pytest.raises(ToolExecutionError, match="'Assinatura'"):
        _swap_image_placeholders(_graphics_doc(), _FRAME)
    with pytest.raises(ToolExecutionError, match="without its data"):
        _swap_image_placeholders(_graphics_doc(), '<p>Novo <img src=""/> texto</p>')


def test_swap_image_placeholders_leaves_other_content_alone():
    from unittest.mock import MagicMock

    from plugin.writer.html_import import _swap_image_placeholders

    model = MagicMock()
    content = '<p>Sem imagem</p><img src="data:image/png;base64,AAAA"/>'
    assert _swap_image_placeholders(model, content) == (content, [])
    model.getGraphicObjects.assert_not_called()


def _kept(anchor="AT_PARAGRAPH"):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from plugin.writer.html_import import _KeptImage

    return _KeptImage("waXimg0", "Assinatura", MagicMock(), MagicMock(), SimpleNamespace(value=anchor),
                      MagicMock(), {"TextWrap": 1})


def test_restore_puts_a_copy_where_the_marker_is_when_the_original_went():
    from unittest.mock import MagicMock, patch

    from plugin.writer import html_import

    model = MagicMock()
    found = model.findFirst.return_value
    with patch.object(html_import, "_deleted_by_the_edit", return_value=True):
        html_import._restore_image_placeholders(model, [_kept()])
    picture = model.createInstance.return_value
    found.getText.return_value.insertTextContent.assert_called_once_with(found, picture, True)
    picture.setPropertyValue.assert_called_with("TextWrap", 1)


def test_restore_only_drops_the_marker_when_the_original_stayed():
    """A search replace of a paragraph's text keeps the paragraph and its picture: no duplicate."""
    from unittest.mock import MagicMock, patch

    from plugin.writer import html_import

    for anchor, deleted in (("AT_PARAGRAPH", False), ("AT_PAGE", True)):
        model = MagicMock()
        found = model.findFirst.return_value
        with patch.object(html_import, "_deleted_by_the_edit", return_value=deleted):
            html_import._restore_image_placeholders(model, [_kept(anchor)])
        found.setString.assert_called_once_with("")
        found.getText.return_value.insertTextContent.assert_not_called()


def test_restore_image_placeholders_missing_marker_for_deleted_original_raises():
    from unittest.mock import MagicMock, patch
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import html_import

    model = MagicMock()
    model.findFirst.return_value = None
    kept = [_kept("AT_PARAGRAPH")]
    with patch.object(html_import, "_deleted_by_the_edit", return_value=True):
        with pytest.raises(ToolExecutionError, match="'Assinatura'"):
            html_import._restore_image_placeholders(model, kept)


def test_restore_image_placeholders_missing_marker_for_kept_original_does_not_raise():
    from unittest.mock import MagicMock, patch
    from plugin.writer import html_import

    model = MagicMock()
    model.findFirst.return_value = None
    kept = [_kept("AT_PARAGRAPH")]
    with patch.object(html_import, "_deleted_by_the_edit", return_value=False):
        html_import._restore_image_placeholders(model, kept)
    model.createInstance.assert_not_called()


def test_deleted_by_the_edit():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from plugin.writer import html_import

    gone = MagicMock()
    gone.getAnchor.side_effect = RuntimeError("disposed")
    assert html_import._deleted_by_the_edit(MagicMock(), gone) is True

    model = MagicMock()
    model.getPropertyValue.return_value = False  # review off: alive means still there
    assert html_import._deleted_by_the_edit(model, MagicMock()) is False

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
    assert html_import._deleted_by_the_edit(model, original) is True


def test_full_document_refuses_before_deleting_anything():
    import pytest
    from unittest.mock import patch

    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import html_import

    model = _graphics_doc()
    with patch.object(html_import, "_replace_full_document") as replace, pytest.raises(ToolExecutionError):
        html_import.replace_full_document(model, None, _FRAME)
    replace.assert_not_called()


def test_html_to_plain_text_pure_does_not_call_scratch_writer():
    from plugin.writer.html_import import html_to_plain_text

    with patch("plugin.framework.uno_context.new_blank_writer") as mock_new_writer:
        assert html_to_plain_text("<b>foo</b>", MagicMock()) == "foo"
        mock_new_writer.assert_not_called()

        assert html_to_plain_text("<p>Hello</p><p>World</p>", MagicMock()) == "Hello\nWorld"
        mock_new_writer.assert_not_called()

        # Ruby input returns base text only
        assert html_to_plain_text("<ruby>漢字<rt>かんじ</rt></ruby>", MagicMock()) == "漢字"
        mock_new_writer.assert_not_called()


def test_html_to_plain_text_plain_string_without_markup_succeeds_without_scratch_doc():
    from plugin.writer.html_import import html_to_plain_text

    with patch("plugin.framework.uno_context.new_blank_writer") as mock_new_writer:
        res = html_to_plain_text("  plain text search  ", MagicMock())
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


def test_apply_block_lo_styles_cursor_walk_no_prefix_enumeration():
    from plugin.writer import html_import

    text = MagicMock()
    next_calls = []
    text.createEnumeration.return_value.hasMoreElements.side_effect = lambda: True
    text.createEnumeration.return_value.nextElement.side_effect = lambda: next_calls.append(1) or MagicMock()

    cur = MagicMock()
    walk_cur = MagicMock()
    text.createTextCursorByRange.return_value = walk_cur
    walk_cur.gotoNextParagraph.return_value = False

    html_import._apply_block_lo_styles(MagicMock(), text, cur, ["Heading1"])

    assert len(next_calls) == 0, "createEnumeration should not be called when cursor is provided"
    walk_cur.gotoStartOfParagraph.assert_called_once_with(False)


def test_styled_insert_propagates_style_pass_failure():
    from plugin.writer import html_import

    cursor = MagicMock()
    cursor.getText.return_value.createTextCursor.return_value.getString.return_value = ""
    with patch.object(html_import, "_apply_block_lo_styles", side_effect=RuntimeError("style")), \
         patch.object(html_import, "insert_html_fragment_at_cursor"):
        with pytest.raises(RuntimeError, match="style"):
            html_import._insert_mixed_or_plain_html(
                MagicMock(), MagicMock(), cursor,
                '<p data-lo-style="Heading1">x</p>',
                apply_styles=True,
            )


def test_trailing_paragraph_not_deleted_when_portion_walk_fails():
    """A failed portion inspection is not 'empty'. Deleting can drop a frame."""
    from plugin.writer.html_import import replace_xtext_with_html

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
        def createTextCursor(self):
            cursor = MagicMock()
            cursor.getString.return_value = "Old"
            return cursor

        def createEnumeration(self):
            return _Enum([_Para("kept"), _Para("", boom=RuntimeError("portion"))])

        def createTextCursorByRange(self, _para):
            deleted.append("delete")
            return MagicMock()

    with patch("plugin.writer.html_import.insert_html_fragment_at_cursor"):
        replace_xtext_with_html(_Text(), "<p>New</p>")
    assert deleted == []


def test_trailing_paragraph_still_deleted_when_walk_is_empty_text():
    from plugin.writer.html_import import replace_xtext_with_html

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

    with patch("plugin.writer.html_import.insert_html_fragment_at_cursor"):
        replace_xtext_with_html(_Text(), "<p>New</p>")
    assert deleted == ["delete"]


def test_trailing_paragraph_disposal_is_not_success():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.html_import import replace_xtext_with_html

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
        def createTextCursor(self):
            cursor = MagicMock()
            cursor.getString.return_value = "Old"
            return cursor

        def createEnumeration(self):
            return _Enum([_Para(), _Para()])

    with patch("plugin.writer.html_import.insert_html_fragment_at_cursor"):
        with pytest.raises(DocumentDisposedError):
            replace_xtext_with_html(_Text(), "<p>New</p>")


def test_uno_read_probes_raise_on_disposal_and_default_otherwise():
    from plugin.framework.errors import DocumentDisposedError
    from plugin.writer.html_import import _cursor_paragraph_has_visible_text, _resolve_paragraph_style_token, _uno_service_true

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
    fam.getElementNames.return_value = ("Heading 1", "Heading 1", "Text Body")
    fam.hasByName.return_value = True
    assert _resolve_paragraph_style_token(MagicMock(), fam, "Heading1") == "Heading 1"


def test_styled_math_insert_styles_from_paragraph_before_insert():
    """The math path moves the cursor past the content, so styling starts after the paragraph before it."""
    from plugin.writer import html_import

    cursor = MagicMock()
    before = MagicMock()
    start = MagicMock()
    with patch.object(html_import, "_insert_mixed_html_and_math_at_cursor"), \
         patch.object(html_import, "_paragraph_before", return_value=before) as pb, \
         patch.object(html_import, "_paragraph_after", return_value=start) as pa, \
         patch.object(html_import, "_apply_block_lo_styles") as apply, \
         patch.object(html_import, "_cursor_goto_document_end"):
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
    from plugin.writer.html_import import _content_has_block_markup

    assert _content_has_block_markup("<p\n style=a>") is True
    assert _content_has_block_markup("<p\tx>") is True
    assert _content_has_block_markup("<span>") is False
    assert _content_has_block_markup("<param>") is False
    assert _content_has_block_markup("List<String>") is False


def test_collect_range_portions_magic_mock_returns_immediately():
    from plugin.writer.html_import import _collect_range_portions

    mock_range = MagicMock()
    has_special, portions = _collect_range_portions(mock_range)
    assert has_special is False
    assert portions == []


def test_restore_fields_by_cursor_single_walk_reverse_order():
    """Restore multiple field tokens in a single portion enumeration walk in reverse order."""
    from unittest.mock import MagicMock, patch
    from plugin.writer.html_import import _restore_fields_by_cursor

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
    model = MagicMock()
    inserted_titles = []

    def _inserted(unused_model, cursor, title, *args):
        inserted_titles.append(title)
        return True

    with (
        patch("plugin.writer.html_import._cursor_over") as mock_cursor_over,
        patch("plugin.writer.html_import._insert_restored_field", side_effect=_inserted),
    ):
        mock_cursor_over.side_effect = lambda _txt, _off, tok: MagicMock()
        restored = _restore_fields_by_cursor(model, text, nonce=nonce)

    assert restored == 2
    assert text.enum_calls == 1
    assert inserted_titles == ["date", "page-number"]


def test_restore_field_placeholders_skips_when_prefix_absent():
    """When the field token prefix is absent from plain text, return 0 without enumeration."""
    from unittest.mock import MagicMock
    from plugin.writer.html_import import _restore_field_placeholders

    text = MagicMock()
    text.getString.return_value = "No fields here"
    model = MagicMock()

    restored = _restore_field_placeholders(model, text, nonce="abc123")
    assert restored == 0
    text.createEnumeration.assert_not_called()
    model.createSearchDescriptor.assert_not_called()

