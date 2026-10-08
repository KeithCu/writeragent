
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

    paras = []
    for idx in range(4):
        para = MagicMock()
        para.supportsService.return_value = True
        para.getStart.return_value = idx
        paras.append(para)
    text = MagicMock()
    enum = text.createEnumeration.return_value
    enum.hasMoreElements.side_effect = [True] * len(paras) + [False]
    enum.nextElement.side_effect = paras
    end = 2  # the parked cursor sits where paragraph 2 starts
    text.compareRegionStarts.side_effect = lambda a, b: 1 if a < b else (0 if a == b else -1)
    with patch.object(html_import.format_mod, "apply_paragraph_style_preserving_direct_char") as apply:
        html_import._apply_block_lo_styles(MagicMock(), text, 0, ["Heading1", "Standard", "Standard", "Heading2"], end=end)
    assert apply.call_count == 2


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
         patch.object(html_import.format_mod, "_deletion_author"):
        html_import.replace_full_document(model, MagicMock(), "<p>x</p>")
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
         patch.object(html_import.format_mod, "_deletion_author"):
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
