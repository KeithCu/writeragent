# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for plugin.writer.html_ruby."""

from __future__ import annotations


from plugin.writer.html_ruby import (
    apply_ruby_spans,
    RubySpan,
    extract_and_strip_ruby,
    inject_ruby_into_html,
)


def test_inject_ruby_rewrites_glued_xhtml():
    """Full XHTML filter concatenates base+reading; rewrite must unglue them."""
    html = '<p data-lo-style="Standard">漢字かんじです</p>'
    out = inject_ruby_into_html(html, [RubySpan("漢字", "かんじ")])
    assert "漢字かんじです" not in out
    assert "<ruby>漢字<rt>かんじ</rt></ruby>です" in out


def test_inject_ruby_wraps_base_when_reading_was_dropped():
    """Range/portion-copy drops RubyText; wrap the surviving base."""
    html = '<p data-lo-style="Standard">漢字です</p>'
    out = inject_ruby_into_html(html, [RubySpan("漢字", "かんじ")])
    assert "<ruby>漢字<rt>かんじ</rt></ruby>です" in out
    assert "漢字かんじです" not in out


def test_inject_ruby_wraps_styled_base_when_reading_was_dropped():
    """Contiguous base inside a span stays inside that span (same as glued-in-span)."""
    html = '<p><span style="font-weight:bold">漢字</span>です</p>'
    out = inject_ruby_into_html(html, [RubySpan("漢字", "かんじ")])
    assert '<span style="font-weight:bold"><ruby>漢字<rt>かんじ</rt></ruby></span>です' in out


def test_inject_ruby_keeps_inner_tags_on_base():
    html = '<p><span style="font-weight:bold">漢字</span>かんじです</p>'
    out = inject_ruby_into_html(html, [RubySpan("漢字", "かんじ")])
    assert '<span style="font-weight:bold"><ruby>漢字<rt>かんじ</rt></ruby></span>です' in out
    assert "漢字かんじ" not in out.replace("<rt>かんじ</rt>", "")


def test_inject_ruby_inside_one_span_is_contiguous():
    html = '<p><span style="font-weight:bold">漢字かんじです</span></p>'
    out = inject_ruby_into_html(html, [RubySpan("漢字", "かんじ")])
    assert '<span style="font-weight:bold"><ruby>漢字<rt>かんじ</rt></ruby>です</span>' in out


def test_inject_ruby_is_idempotent():
    html = "<p><ruby>漢字<rt>かんじ</rt></ruby>です</p>"
    out = inject_ruby_into_html(html, [RubySpan("漢字", "かんじ")])
    assert out.count("<ruby>") == 1
    assert out == html


def test_inject_ruby_sequential_spans():
    html = "<p>東京とうきょうと京都きょうと</p>"
    out = inject_ruby_into_html(html, [RubySpan("東京", "とうきょう"), RubySpan("京都", "きょうと")])
    assert "<ruby>東京<rt>とうきょう</rt></ruby>と<ruby>京都<rt>きょうと</rt></ruby>" in out


def test_inject_ruby_empty_is_noop():
    assert inject_ruby_into_html("<p>漢字です</p>", []) == "<p>漢字です</p>"
    assert inject_ruby_into_html("", [RubySpan("漢字", "かんじ")]) == ""
    assert inject_ruby_into_html("<p>x</p>", [RubySpan("漢字", "")]) == "<p>x</p>"


def test_inject_ruby_keeps_under_position():
    under_html = inject_ruby_into_html("<p>東京とうきょう</p>", [RubySpan("東京", "とうきょう", is_above=False)])
    assert '<ruby style="ruby-position: under">東京<rt>とうきょう</rt></ruby>' in under_html
    over_html = inject_ruby_into_html("<p>東京とうきょう</p>", [RubySpan("東京", "とうきょう", is_above=True)])
    assert "<ruby>東京<rt>とうきょう</rt></ruby>" in over_html


def test_ruby_position_round_trips():
    under_html = inject_ruby_into_html("<p>東京とうきょう</p>", [RubySpan("東京", "とうきょう", is_above=False)])
    _clean, spans = extract_and_strip_ruby(under_html)
    assert len(spans) == 1
    assert spans[0].is_above is False
    assert spans[0].visible_offset == 0


def test_inject_ruby_wraps_every_copy_when_reading_is_absent():
    html = "<p>漢字と漢字と漢字</p>"
    spans = [RubySpan("漢字", "かんじ"), RubySpan("漢字", "かんじ"), RubySpan("漢字", "かんじ")]
    out = inject_ruby_into_html(html, spans)
    assert out.count("<ruby>") == 3
    assert out == "<p><ruby>漢字<rt>かんじ</rt></ruby>と<ruby>漢字<rt>かんじ</rt></ruby>と<ruby>漢字<rt>かんじ</rt></ruby></p>"


def test_inject_ruby_crosses_br_to_the_reading():
    html = "<p>漢字<br>かんじです</p>"
    out = inject_ruby_into_html(html, [RubySpan("漢字", "かんじ")])
    assert out == "<p><ruby>漢字<rt>かんじ</rt></ruby><br>です</p>"


def test_inject_ruby_prefers_glued_copy_over_an_earlier_plain_base():
    html = "<p>漢字と漢字かんじ</p>"
    out = inject_ruby_into_html(html, [RubySpan("漢字", "かんじ")])
    assert out == "<p>漢字と<ruby>漢字<rt>かんじ</rt></ruby></p>"


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


def test_extract_and_strip_ruby_noop():
    html = "<p>plain</p>"
    clean, spans = extract_and_strip_ruby(html)
    assert clean == html
    assert spans == []


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
