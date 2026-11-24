# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for plugin.writer.html_ruby."""

from __future__ import annotations


from plugin.writer.html_ruby import (
    ImportRubySpan,
    RubySpan,
    inject_ruby_into_html,
    ruby_placeholders,
    ruby_token_prefix,
    strip_ruby,
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
    _clean, spans = ruby_placeholders(under_html, "n1")
    assert spans == [ImportRubySpan("東京", "とうきょう", False)]


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


def test_ruby_placeholders_simple():
    html = "<p>前<ruby>漢字<rt>かんじ</rt></ruby>後</p>"
    clean, spans = ruby_placeholders(html, "n1")
    assert clean == f"<p>前{ruby_token_prefix('n1')}0]]漢字後</p>"
    assert spans == [ImportRubySpan("漢字", "かんじ", True)]
    assert strip_ruby(html) == "<p>前漢字後</p>"


def test_ruby_placeholders_under_and_rp():
    html = '<ruby style="ruby-position: under">東京<rp>(</rp><rt>とうきょう</rt><rp>)</rp></ruby>'
    clean, spans = ruby_placeholders(html, "n1")
    assert clean == f"{ruby_token_prefix('n1')}0]]東京"
    assert spans == [ImportRubySpan("東京", "とうきょう", False)]
    assert strip_ruby(html) == "東京"


def test_ruby_placeholders_position_ignores_unrelated_attrs():
    html = '<ruby class="thunder">東京<rt>とうきょう</rt></ruby>'
    clean, spans = ruby_placeholders(html, "n1")
    assert clean == f"{ruby_token_prefix('n1')}0]]東京"
    assert len(spans) == 1
    assert spans[0].is_above is True


def test_ruby_placeholders_unbalanced_ruby_has_no_spans():
    html = "<ruby>漢字<rt>かんじ</rt>" * 2000
    clean, spans = ruby_placeholders(html, "n1")
    assert spans == []
    assert "<rt>" not in clean
    assert "かんじ" not in clean
    assert clean == strip_ruby(html)


def test_ruby_placeholders_uppercase_tags():
    html = "<RUBY>漢字<RT>かんじ</RT></RUBY>"
    clean, spans = ruby_placeholders(html, "n1")
    assert clean == f"{ruby_token_prefix('n1')}0]]漢字"
    assert spans == [ImportRubySpan("漢字", "かんじ", True)]


def test_ruby_placeholders_noop():
    html = "<p>plain</p>"
    clean, spans = ruby_placeholders(html, "n1")
    assert clean == html
    assert spans == []
    assert strip_ruby(html) == html


def test_ruby_placeholders_padded_base_keeps_token_after_leading_ws():
    html = "<p>a<ruby>\n  <b>漢字</b>\n  <rt>かんじ</rt></ruby></p>"
    clean, spans = ruby_placeholders(html, "n1")
    assert clean == f"<p>a\n  <b>{ruby_token_prefix('n1')}0]]漢字</b>\n  </p>"
    assert spans == [ImportRubySpan("漢字", "かんじ", True)]


def test_ruby_placeholders_empty_reading_drops_token():
    html = "<p><ruby>漢字<rt>  </rt></ruby></p>"
    clean, spans = ruby_placeholders(html, "n1")
    assert clean == "<p>漢字</p>"
    assert spans == []


def test_ruby_placeholders_skips_leading_tags_and_nbsp():
    pfx = ruby_token_prefix("n1")
    clean1, spans1 = ruby_placeholders("a<ruby><b> 漢字</b><rt>かんじ</rt></ruby>", "n1")
    assert clean1 == f"a<b> {pfx}0]]漢字</b>"
    assert spans1 == [ImportRubySpan("漢字", "かんじ", True)]

    clean2, spans2 = ruby_placeholders("<ruby><rb>\n 漢字\n</rb><rt>かんじ</rt></ruby>", "n1")
    assert clean2 == f"<rb>\n {pfx}0]]漢字\n</rb>"
    assert spans2 == [ImportRubySpan("漢字", "かんじ", True)]

    clean3, spans3 = ruby_placeholders("<ruby>&nbsp;漢字<rt>かんじ</rt></ruby>", "n1")
    assert clean3 == f"{pfx}0]]&nbsp;漢字"
    assert spans3 == [ImportRubySpan("\u00a0漢字", "かんじ", True)]
