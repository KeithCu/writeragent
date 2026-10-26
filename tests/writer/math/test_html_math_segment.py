# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for MathML HTML segmentation (no LibreOffice required)."""

import time

from plugin.writer.math.html_math_segment import (
    html_fragment_contains_mathml,
    html_fragment_contains_mixed_math,
    html_fragment_contains_tex_math,
    segment_html_with_mixed_math,
)


class TestHtmlMathSegment:
    def test_contains_mathml(self):
        assert not (html_fragment_contains_mathml(""))
        assert not (html_fragment_contains_mathml("hello"))
        assert (html_fragment_contains_mathml("<math><mi>x</mi></math>"))
        assert (html_fragment_contains_mathml("<MATH xmlns=...>"))

    def test_plain_html_single_segment(self):
        segs = segment_html_with_mixed_math("<p>hi</p>")
        assert (len(segs)) == (1)
        assert (segs[0].kind) == ("html")
        assert (segs[0].text) == ("<p>hi</p>")

    def test_order_mixed_inline(self):
        html = '<p>Before <math><mi>a</mi></math> after.</p>'
        segs = segment_html_with_mixed_math(html)
        assert ([s.kind for s in segs]) == (["html", "math", "html"])
        assert (segs[0].text) == ("<p>Before ")
        assert (segs[1].text.lower().startswith("<math"))
        assert (segs[1].text.lower().endswith("</math>"))
        assert not (segs[1].display_block)
        assert (segs[2].text) == (" after.</p>")

    def test_display_block_attribute(self):
        m = '<math display="block"><mi>x</mi></math>'
        segs = segment_html_with_mixed_math(m)
        assert (len(segs)) == (1)
        assert (segs[0].kind) == ("math")
        assert (segs[0].display_block)

    def test_mode_display_legacy(self):
        m = '<math mode="display"><mi>x</mi></math>'
        segs = segment_html_with_mixed_math(m)
        assert (segs[0].display_block)

    def test_katex_wrapper_preserved(self):
        wrapped = (
            '<span class="katex">'
            '<math><semantics><mi>x</mi></semantics></math>'
            "</span>"
        )
        segs = segment_html_with_mixed_math(wrapped)
        kinds = [s.kind for s in segs]
        assert (kinds) == (["html", "math", "html"])
        assert ("semantics") in (segs[1].text)

    def test_unclosed_math_becomes_html_tail(self):
        segs = segment_html_with_mixed_math("<p>a<math><mi>x</mi>")
        assert ([s.kind for s in segs]) == (["html", "html"])
        assert ("<math") in (segs[1].text)

    def test_multiple_formulas(self):
        h = "<p><math><mi>a</mi></math>+<math><mi>b</mi></math></p>"
        segs = segment_html_with_mixed_math(h)
        assert (segs[0].kind) == ("html")
        assert (segs[1].kind) == ("math")
        assert (segs[2].kind) == ("html")
        assert (segs[3].kind) == ("math")
        assert (segs[4].kind) == ("html")

    def test_contains_tex(self):
        assert not (html_fragment_contains_tex_math(""))
        assert not (html_fragment_contains_tex_math("no math"))
        assert (html_fragment_contains_tex_math(r"$\alpha$"))
        assert (html_fragment_contains_tex_math(r"$$\int$$"))
        assert (html_fragment_contains_tex_math(r"\(x\)"))
        assert (html_fragment_contains_tex_math(r"\[y\]"))
        assert not (html_fragment_contains_tex_math("$100 is a lot"))

    def test_mixed_math_contains_union(self):
        assert (html_fragment_contains_mixed_math("<math></math>"))
        assert (html_fragment_contains_mixed_math(r"$\pi$"))
        assert not (html_fragment_contains_mixed_math("<p>plain</p>"))

    def test_segment_tex_inline(self):
        segs = segment_html_with_mixed_math(r"<p>Hi \(x^2\) there</p>")
        assert ([s.kind for s in segs]) == (["html", "tex", "html"])
        assert (segs[1].text) == ("x^2")
        assert not (segs[1].display_block)

    def test_segment_tex_display_brackets(self):
        segs = segment_html_with_mixed_math(r"pre \[a+b\] post")
        assert ([s.kind for s in segs]) == (["html", "tex", "html"])
        assert (segs[1].text) == ("a+b")
        assert (segs[1].display_block)

    def test_segment_tex_dollar_display(self):
        segs = segment_html_with_mixed_math(r"$$\frac{1}{2}$$")
        assert (len(segs)) == (1)
        assert (segs[0].kind) == ("tex")
        assert ("frac") in (segs[0].text)
        assert (segs[0].display_block)

    def test_mathml_before_tex_in_stream(self):
        h = r'<math><mi>a</mi></math> then $\pi$'
        segs = segment_html_with_mixed_math(h)
        assert ([s.kind for s in segs]) == (["math", "html", "tex"])
        assert (segs[0].text.lower().startswith("<math"))

    def test_currency_does_not_open_tex(self):
        """``R$ 1.234,56`` pairs must stay prose (issue: Writer inserted a formula)."""
        brl = (
            "<p>Requer a condena\u00e7\u00e3o em R$ 12.798,82, como montante "
            "indenizat\u00f3rio, evitando-se, assim, o enriquecimento, al\u00e9m "
            "de R$ 500,00 de custas.</p>"
        )
        assert not (html_fragment_contains_tex_math(brl))
        assert not (html_fragment_contains_mixed_math(brl))
        segs = segment_html_with_mixed_math(brl)
        assert ([s.kind for s in segs]) == (["html"])
        assert (segs[0].text) == (brl)

    def test_currency_shapes_are_not_tex(self):
        for text in (
            "R$12.798,82 e R$500,00",
            "US$ 1,00 at\u00e9 US$ 9,00",
            "custa 100$ ou 200$",
            "total: $ 5,00 e $ 9,00",
            "$.50 e $.75",
        ):
            assert not (html_fragment_contains_tex_math(text))
            assert ([s.kind for s in segment_html_with_mixed_math(text)]) == (["html"])

    def test_real_tex_still_segments_next_to_currency(self):
        h = "valor R$ 5,00 e a f\u00f3rmula $x^2$ aqui"
        assert (html_fragment_contains_tex_math(h))
        segs = segment_html_with_mixed_math(h)
        assert ([s.kind for s in segs]) == (["html", "tex", "html"])
        assert (segs[1].text) == ("x^2")

    def test_unclosed_dollar_is_not_tex(self):
        assert not (html_fragment_contains_tex_math(r"$\alpha with no close"))
        assert ([s.kind for s in segment_html_with_mixed_math(r"$\alpha with no close")]) == (["html"])

    def test_letter_prefixed_currency_can_close_an_open_run(self):
        """Closer is not ``_is_currency_dollar``: letter-prefixed ``R$`` can still close.

        Two currency amounts still cannot pair (those ``$`` never open). Reusing
        the opener helper on the closer would also reject ``$x$`` (``x`` is
        alphanumeric) and kill real inline math.
        """
        h = r"$\alpha with no close and R$ 500,00"
        assert (html_fragment_contains_tex_math(h))
        segs = segment_html_with_mixed_math(h)
        assert ([s.kind for s in segs]) == (["tex", "html"])
        assert (segs[0].text) == (r"\alpha with no close and R")
        assert (segs[1].text) == (" 500,00")

        spaced = r"$\alpha with no close and $ 500"
        assert not (html_fragment_contains_tex_math(spaced))
        assert ([s.kind for s in segment_html_with_mixed_math(spaced)]) == (["html"])

    def test_tex_before_mathml(self):
        h = r'$\pi$<math><mi>x</mi></math>'
        segs = segment_html_with_mixed_math(h)
        assert ([s.kind for s in segs]) == (["tex", "math"])
        assert (segs[0].text) == (r"\pi")

    def test_tex_scan_runs_once_for_many_math_segments(self, monkeypatch):
        from plugin.writer.math import html_math_segment

        calls = []
        real = html_math_segment._next_complete_tex_region

        def counting(s, pos, no_close=None):
            calls.append(pos)
            return real(s, pos, no_close)

        monkeypatch.setattr(html_math_segment, "_next_complete_tex_region", counting)
        segs = segment_html_with_mixed_math("<p><math><mi>x</mi></math></p>" * 2000 + r" then $y$.")
        assert sum(1 for s in segs if s.kind == "tex") == 1
        assert len(calls) <= 2

    def test_many_math_segments_are_fast(self):
        started = time.monotonic()
        segs = segment_html_with_mixed_math("<p><math><mi>x</mi></math></p>" * 2000)
        assert sum(1 for s in segs if s.kind == "math") == 2000
        assert time.monotonic() - started < 5.0

    def test_unclosed_currency_prose_does_not_rescan_the_tail(self):
        started = time.monotonic()
        text = " ".join(f"cost $x{i} per item" for i in range(2000))
        segs = segment_html_with_mixed_math(text)
        assert [s.kind for s in segs] == ["html"]
        assert time.monotonic() - started < 5.0

    def test_detector_true_when_a_math_span_swallows_tex(self):
        h = r"<math><mi> $b$ </mi></math>"
        assert html_fragment_contains_tex_math(h) is True
        assert [s.kind for s in segment_html_with_mixed_math(h)] == ["math"]


class TestHtmlMarkupIsNotMath:
    def test_dollar_in_attributes_is_prose(self):
        h = '<a href="/w?x=$a$" title="cost $5$">x</a>'
        assert html_fragment_contains_tex_math(h) is False
        segs = segment_html_with_mixed_math(h)
        assert [s.kind for s in segs] == ["html"]
        assert segs[0].text == h

    def test_math_tag_in_attribute_or_comment_is_not_mathml(self):
        assert html_fragment_contains_mathml('<a title="<math>">x</a>') is False
        assert html_fragment_contains_mathml("<!-- <math><mi>x</mi></math> -->") is False
        assert [s.kind for s in segment_html_with_mixed_math("<!-- <math><mi>x</mi></math> -->")] == ["html"]

    def test_script_and_style_bodies_are_prose(self):
        for h in ('<script>var x = "$a$";</script>', '<style>p::before { content: "$x$"; }</style>'):
            assert html_fragment_contains_tex_math(h) is False
            assert [s.kind for s in segment_html_with_mixed_math(h)] == ["html"]

    def test_math_tag_attributes_do_not_hide_the_math(self):
        h = '<math display="block" alttext="$x$"><mi>x</mi></math>'
        segs = segment_html_with_mixed_math(h)
        assert [s.kind for s in segs] == ["math"]
        assert segs[0].display_block is True

    def test_less_than_inside_tex_still_segments(self):
        segs = segment_html_with_mixed_math(r"$a < b$")
        assert [s.kind for s in segs] == ["tex"]
        assert segs[0].text == "a < b"

    def test_unspaced_less_than_inside_tex_before_a_tag_still_segments(self):
        h = "<p>$x<y$ and <i>z</i></p>"
        assert html_fragment_contains_tex_math(h) is True
        segs = segment_html_with_mixed_math(h)
        assert [s.kind for s in segs] == ["html", "tex", "html"]
        assert segs[1].text == "x<y"
