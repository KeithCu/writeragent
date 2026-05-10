# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest
from unittest.mock import patch

from plugin.modules.writer.math_mml_convert import (
    MathConversionResult,
    collapse_starmath_newline_tokens_for_writer_embed,
    convert_latex_to_starmath,
)


class _QuietExc(Exception):
    def __str__(self) -> str:
        return ""


def test_exception_message_non_empty_for_empty_str_uno_style() -> None:
    from plugin.modules.writer import math_mml_convert as mmc

    assert mmc._exception_message(_QuietExc()) == "_QuietExc"
    assert "ValueError" in mmc._exception_message(ValueError("bad"))



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


class TestConvertLatexToStarmath(unittest.TestCase):
    def test_delegates_to_mathml_path(self):
        fake_ctx = object()
        with patch(
            "plugin.modules.writer.math_mml_convert.convert_mathml_to_starmath"
        ) as mock_mml:
            mock_mml.return_value = MathConversionResult(True, "a + b", None)
            res = convert_latex_to_starmath(fake_ctx, "a+b", display_block=False)
            self.assertTrue(res.ok)
            self.assertEqual(res.starmath, "a + b")
            mock_mml.assert_called_once()
            call_ctx, mathml_arg = mock_mml.call_args[0]
            self.assertIs(call_ctx, fake_ctx)
            self.assertIn("<math", mathml_arg.lower())
            self.assertIn("http://www.w3.org/1998/Math/MathML", mathml_arg)


if __name__ == "__main__":
    unittest.main()
