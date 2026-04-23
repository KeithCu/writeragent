# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for MathML HTML segmentation (no LibreOffice required)."""

import unittest

from plugin.modules.writer.html_math_segment import (
    html_fragment_contains_mathml,
    segment_html_with_mathml,
)


class TestHtmlMathSegment(unittest.TestCase):
    def test_contains_mathml(self):
        self.assertFalse(html_fragment_contains_mathml(""))
        self.assertFalse(html_fragment_contains_mathml("hello"))
        self.assertTrue(html_fragment_contains_mathml("<math><mi>x</mi></math>"))
        self.assertTrue(html_fragment_contains_mathml("<MATH xmlns=...>"))

    def test_plain_html_single_segment(self):
        segs = segment_html_with_mathml("<p>hi</p>")
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].kind, "html")
        self.assertEqual(segs[0].text, "<p>hi</p>")

    def test_order_mixed_inline(self):
        html = '<p>Before <math><mi>a</mi></math> after.</p>'
        segs = segment_html_with_mathml(html)
        self.assertEqual([s.kind for s in segs], ["html", "math", "html"])
        self.assertEqual(segs[0].text, "<p>Before ")
        self.assertTrue(segs[1].text.lower().startswith("<math"))
        self.assertTrue(segs[1].text.lower().endswith("</math>"))
        self.assertFalse(segs[1].display_block)
        self.assertEqual(segs[2].text, " after.</p>")

    def test_display_block_attribute(self):
        m = '<math display="block"><mi>x</mi></math>'
        segs = segment_html_with_mathml(m)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].kind, "math")
        self.assertTrue(segs[0].display_block)

    def test_mode_display_legacy(self):
        m = '<math mode="display"><mi>x</mi></math>'
        segs = segment_html_with_mathml(m)
        self.assertTrue(segs[0].display_block)

    def test_katex_wrapper_preserved(self):
        wrapped = (
            '<span class="katex">'
            '<math><semantics><mi>x</mi></semantics></math>'
            "</span>"
        )
        segs = segment_html_with_mathml(wrapped)
        kinds = [s.kind for s in segs]
        self.assertEqual(kinds, ["html", "math", "html"])
        self.assertIn("semantics", segs[1].text)

    def test_unclosed_math_becomes_html_tail(self):
        segs = segment_html_with_mathml("<p>a<math><mi>x</mi>")
        self.assertEqual([s.kind for s in segs], ["html", "html"])
        self.assertIn("<math", segs[1].text)

    def test_multiple_formulas(self):
        h = "<p><math><mi>a</mi></math>+<math><mi>b</mi></math></p>"
        segs = segment_html_with_mathml(h)
        self.assertEqual(segs[0].kind, "html")
        self.assertEqual(segs[1].kind, "math")
        self.assertEqual(segs[2].kind, "html")
        self.assertEqual(segs[3].kind, "math")
        self.assertEqual(segs[4].kind, "html")


if __name__ == "__main__":
    unittest.main()
