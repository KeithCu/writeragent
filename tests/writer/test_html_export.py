

from plugin.writer.html_export import inject_ruby_into_html, xtext_to_content


def test_xtext_to_content_none_is_empty():
    assert xtext_to_content(None, object(), object()) == ""


def test_ops_uno_skips_windows_leftover_text_offsets() -> None:
    """GHA 34689136372: leftover reuse offset mismatch, not a product range bug."""
    from pathlib import Path

    src = Path(__file__).with_name("test_ops_uno.py").read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_load" in src
    assert "ops_uno leftover text offsets" in src
    assert "34689136372" in src
    assert "normalize_linebreaks" in src
    assert "35466498641" in src


def test_html_export_uno_skips_windows_leftover_hidden_temp_doc() -> None:
    """GHA 34690797019: leftover Hidden _default hung on temp_doc.close."""
    from pathlib import Path

    src = Path(__file__).with_name("test_html_export_uno.py").read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_load" in src
    assert "html_export Hidden _default temp_doc" in src
    assert "34690797019" in src


def test_inject_ruby_rewrites_glued_xhtml():
    """Full XHTML filter concatenates base+reading; rewrite must unglue them."""
    html = '<p data-lo-style="Standard">漢字かんじです</p>'
    out = inject_ruby_into_html(html, [("漢字", "かんじ")])
    assert "漢字かんじです" not in out
    assert "<ruby>漢字<rt>かんじ</rt></ruby>です" in out


def test_inject_ruby_wraps_base_when_reading_was_dropped():
    """Range/portion-copy drops RubyText; wrap the surviving base."""
    html = '<p data-lo-style="Standard">漢字です</p>'
    out = inject_ruby_into_html(html, [("漢字", "かんじ")])
    assert "<ruby>漢字<rt>かんじ</rt></ruby>です" in out
    assert "漢字かんじです" not in out


def test_inject_ruby_wraps_styled_base_when_reading_was_dropped():
    """Contiguous base inside a span stays inside that span (same as glued-in-span)."""
    html = '<p><span style="font-weight:bold">漢字</span>です</p>'
    out = inject_ruby_into_html(html, [("漢字", "かんじ")])
    assert '<span style="font-weight:bold"><ruby>漢字<rt>かんじ</rt></ruby></span>です' in out


def test_inject_ruby_keeps_inner_tags_on_base():
    html = '<p><span style="font-weight:bold">漢字</span>かんじです</p>'
    out = inject_ruby_into_html(html, [("漢字", "かんじ")])
    assert '<span style="font-weight:bold"><ruby>漢字<rt>かんじ</rt></ruby></span>です' in out
    assert "漢字かんじ" not in out.replace("<rt>かんじ</rt>", "")


def test_inject_ruby_inside_one_span_is_contiguous():
    html = '<p><span style="font-weight:bold">漢字かんじです</span></p>'
    out = inject_ruby_into_html(html, [("漢字", "かんじ")])
    assert '<span style="font-weight:bold"><ruby>漢字<rt>かんじ</rt></ruby>です</span>' in out


def test_inject_ruby_is_idempotent():
    html = "<p><ruby>漢字<rt>かんじ</rt></ruby>です</p>"
    out = inject_ruby_into_html(html, [("漢字", "かんじ")])
    assert out.count("<ruby>") == 1
    assert out == html


def test_inject_ruby_sequential_spans():
    html = "<p>東京とうきょうと京都きょうと</p>"
    out = inject_ruby_into_html(html, [("東京", "とうきょう"), ("京都", "きょうと")])
    assert "<ruby>東京<rt>とうきょう</rt></ruby>と<ruby>京都<rt>きょうと</rt></ruby>" in out


def test_inject_ruby_empty_is_noop():
    assert inject_ruby_into_html("<p>漢字です</p>", []) == "<p>漢字です</p>"
    assert inject_ruby_into_html("", [("漢字", "かんじ")]) == ""
    assert inject_ruby_into_html("<p>x</p>", [("漢字", "")]) == "<p>x</p>"
