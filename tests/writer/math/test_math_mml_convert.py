# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import MagicMock, patch

from plugin.writer.math.math_mml_convert import (
    MathConversionResult,
    collapse_starmath_newline_tokens_for_writer_embed,
    convert_latex_to_starmath,
    insert_writer_math_formula,
)


class _QuietExc(Exception):
    def __str__(self) -> str:
        return ""


def test_exception_message_non_empty_for_empty_str_uno_style() -> None:
    from plugin.writer.math import math_mml_convert as mmc

    assert mmc._exception_message(_QuietExc()) == "_QuietExc"
    assert "ValueError" in mmc._exception_message(ValueError("bad"))

    class _Uno(Exception):
        def __init__(self) -> None:
            super().__init__("")
            self.Message = "filter failed"

    assert mmc._exception_message(_Uno()) == "_Uno: filter failed"


def test_math_root_extraction() -> None:
    from plugin.writer.math.math_mml_convert import math_root

    assert math_root('<?xml version="1.0"?><math><mi>x</mi></math>') == '<math><mi>x</mi></math>'
    assert math_root('prefix <math display="block"><mi>y</mi></math> suffix') == '<math display="block"><mi>y</mi></math>'
    assert math_root('not a math root') is None
    assert math_root('') is None


def test_convert_mathml_to_starmath_rejects_non_math_root() -> None:
    from plugin.writer.math.math_mml_convert import convert_mathml_to_starmath

    res = convert_mathml_to_starmath(object(), "<html><body>not math</body></html>")
    assert res.ok is False
    assert res.error_message == "not_math_root"


def test_convert_mathml_to_starmath_closes_doc_on_property_error() -> None:
    from unittest.mock import MagicMock
    from plugin.writer.math.math_mml_convert import convert_mathml_to_starmath

    mock_doc = MagicMock()
    mock_doc.getPropertyValue.side_effect = RuntimeError("failed to read Formula")
    mock_desktop = MagicMock()
    mock_desktop.loadComponentFromURL.return_value = mock_doc

    with patch("plugin.writer.math.math_mml_convert.get_desktop", return_value=mock_desktop):
        res = convert_mathml_to_starmath(object(), "<math><mi>x</mi></math>")

    assert res.ok is False
    assert "failed to read Formula" in (res.error_message or "")
    mock_doc.close.assert_called_once_with(True)





class TestCollapseStarmathNewline:
    def test_collapses_spaced_newline_operators(self):
        raw = "a newline x ^ 2 newline + newline b"
        assert (collapse_starmath_newline_tokens_for_writer_embed(raw)) == ("a x ^ 2 + b")

    def test_formula_like_lo_quadratic(self):
        raw = (
            "x newline = newline { frac { { - b +- sqrt { b ^ 2 - 4 a c } } } "
            "{ { 2 a } } }"
        )
        out = collapse_starmath_newline_tokens_for_writer_embed(raw)
        assert ("newline") not in (out)
        assert ("x =") in (out)
        assert ("frac") in (out)

    def test_idempotent(self):
        s = "a + b"
        assert (collapse_starmath_newline_tokens_for_writer_embed(s)) == (s)


class TestConvertLatexToStarmath:
    def test_delegates_to_mathml_path(self):
        fake_ctx = object()
        with patch(
            "plugin.writer.math.math_mml_convert.convert_mathml_to_starmath"
        ) as mock_mml:
            mock_mml.return_value = MathConversionResult(True, "a + b", None)
            res = convert_latex_to_starmath(fake_ctx, "a+b", display_block=False)
            assert (res.ok)
            assert (res.starmath) == ("a + b")
            mock_mml.assert_called_once()
            call_ctx, mathml_arg = mock_mml.call_args[0]
            assert (call_ctx) is (fake_ctx)
            assert ("<math") in (mathml_arg.lower())
            assert ("http://www.w3.org/1998/Math/MathML") in (mathml_arg)


def test_math_formula_insert_uno_skips_windows_leftover_hidden_mml() -> None:
    """GHA 34678020608: leftover writer reuse then Hidden _blank .mml hung 30s."""
    from pathlib import Path

    src = Path(__file__).with_name("test_math_formula_insert_uno.py").read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_load" in src
    assert "math formula Hidden _blank .mml" in src
    assert "34678020608" in src


class _FakeMathText:
    def __init__(self) -> None:
        self.control_chars: list[tuple[object, int, bool]] = []
        self.contents: list[object] = []

    def insertControlCharacter(self, cursor: object, char: int, absorb: bool) -> None:
        self.control_chars.append((cursor, char, absorb))

    def insertTextContent(self, cursor: object, content: object, absorb: bool) -> None:
        self.contents.append(content)


class _FakeMathCursor:
    def __init__(self, text: _FakeMathText, initial_adjust: int) -> None:
        self._text = text
        self.initial_adjust = initial_adjust
        self.adjust_writes: list[object] = []

    def getText(self) -> _FakeMathText:
        return self._text

    def getPropertyValue(self, prop: str) -> object:
        if prop == "ParaAdjust":
            return self.initial_adjust
        return None

    def setPropertyValue(self, prop: str, val: object) -> None:
        if prop == "ParaAdjust":
            self.adjust_writes.append(val)


def test_display_math_restores_paragraph_alignment() -> None:
    text = _FakeMathText()
    cur = _FakeMathCursor(text, initial_adjust=1)
    model = MagicMock()

    insert_writer_math_formula(model, cur, "x = y", display_block=True)

    assert len(text.control_chars) == 2
    assert cur.adjust_writes == [3, 1]


def test_inline_math_leaves_paragraph_alignment_alone() -> None:
    text = _FakeMathText()
    cur = _FakeMathCursor(text, initial_adjust=2)
    model = MagicMock()

    insert_writer_math_formula(model, cur, "x = y", display_block=False)

    assert len(text.control_chars) == 0
    assert cur.adjust_writes == []



