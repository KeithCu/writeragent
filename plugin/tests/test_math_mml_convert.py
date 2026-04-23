# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest

from plugin.modules.writer.math_mml_convert import (
    collapse_starmath_newline_tokens_for_writer_embed,
)


class TestCollapseStarmathNewline(unittest.TestCase):
    def test_collapses_spaced_newline_operators(self):
        raw = "a newline x ^ 2 newline + newline b"
        self.assertEqual(
            collapse_starmath_newline_tokens_for_writer_embed(raw),
            "a x ^ 2 + b",
        )

    def test_formula_like_lo_quadratic(self):
        raw = (
            "x newline = newline { frac { { - b +- sqrt { b ^ 2 - 4 a c } } } "
            "{ { 2 a } } }"
        )
        out = collapse_starmath_newline_tokens_for_writer_embed(raw)
        self.assertNotIn("newline", out)
        self.assertIn("x =", out)
        self.assertIn("frac", out)

    def test_idempotent(self):
        s = "a + b"
        self.assertEqual(collapse_starmath_newline_tokens_for_writer_embed(s), s)


if __name__ == "__main__":
    unittest.main()
