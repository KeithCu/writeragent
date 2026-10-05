
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
