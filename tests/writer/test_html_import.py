

from plugin.writer.html_import import content_has_markup, extract_and_strip_ruby


def test_extract_and_strip_ruby_simple():
    html = "<p>前<ruby>漢字<rt>かんじ</rt></ruby>後</p>"
    clean, spans = extract_and_strip_ruby(html)
    assert clean == "<p>前漢字後</p>"
    assert spans == [("漢字", "かんじ", True)]


def test_extract_and_strip_ruby_under_and_rp():
    html = '<ruby style="ruby-position: under">東京<rp>(</rp><rt>とうきょう</rt><rp>)</rp></ruby>'
    clean, spans = extract_and_strip_ruby(html)
    assert clean == "東京"
    assert spans == [("東京", "とうきょう", False)]


def test_extract_and_strip_ruby_noop():
    html = "<p>plain</p>"
    clean, spans = extract_and_strip_ruby(html)
    assert clean == html
    assert spans == []


def test_content_has_markup_detects_ruby():
    assert content_has_markup("<ruby>漢字<rt>かんじ</rt></ruby>")
    assert content_has_markup("<p>前<ruby>漢字<rt>かんじ</rt></ruby>後</p>")
