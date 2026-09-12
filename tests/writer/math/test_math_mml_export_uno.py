# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests: StarMath → MathML → LaTeX export and document_to_content splice."""

from __future__ import annotations

from typing import Any

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import skip_windows_leftover_hidden_load, with_native_doc


def _skip_windows_leftover_hidden_mathml() -> None:
    """GHA 34678020608: leftover Hidden ``_blank`` ``.mml`` / ``smath`` hang."""
    skip_windows_leftover_hidden_load("math export Hidden _blank smath")
from plugin.writer.math.math_mml_convert import convert_latex_to_starmath, insert_writer_math_formula
from plugin.writer.math.math_mml_export import (
    convert_starmath_to_latex,
    convert_starmath_to_mathml,
    iter_writer_math_objects,
)


@native_test
@with_native_doc("writer")
def test_starmath_to_mathml_fraction(ctx: Any, doc: Any) -> None:
    _skip_windows_leftover_hidden_mathml()
    res = convert_starmath_to_mathml(ctx, "{a} over {b}")
    assert res.ok, res.error_message
    assert res.mathml
    low = res.mathml.lower()
    assert "<math" in low
    assert "frac" in low or "over" in low or "<mi>a</mi>" in low


@native_test
@with_native_doc("writer")
def test_starmath_to_latex_reimports(ctx: Any, doc: Any) -> None:
    _skip_windows_leftover_hidden_mathml()
    res = convert_starmath_to_latex(ctx, "{a} over {b}")
    assert res.ok, res.error_message
    assert res.latex
    assert "frac" in res.latex.lower() or "over" in res.latex.lower() or "/" in res.latex
    back = convert_latex_to_starmath(ctx, res.latex)
    assert back.ok, back.error_message
    assert back.starmath


@native_test
@with_native_doc("writer")
def test_iter_inline_and_display_math(ctx: Any, doc: Any) -> None:
    _skip_windows_leftover_hidden_mathml()
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    text.insertString(cur, "Hello ", False)
    insert_writer_math_formula(doc, cur, "a + b", display_block=False)
    text.insertString(cur, " World", False)
    insert_writer_math_formula(doc, cur, "x", display_block=True)

    hits = list(iter_writer_math_objects(doc, ctx))
    assert len(hits) >= 2
    inline = hits[0]
    assert inline.display_block is False
    assert "a" in inline.starmath and "b" in inline.starmath
    display = hits[-1]
    assert display.display_block is True


@native_test
@with_native_doc("writer")
def test_document_to_content_includes_tex(ctx: Any, doc: Any) -> None:
    _skip_windows_leftover_hidden_mathml()
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    text.insertString(cur, "Hi ", False)
    conv = convert_latex_to_starmath(ctx, r"\frac{1}{2}")
    assert conv.ok and conv.starmath, conv.error_message
    insert_writer_math_formula(doc, cur, conv.starmath, display_block=False)
    text.insertString(cur, " there", False)

    html = document_to_content(doc, ctx, services=None, scope="full")
    assert html
    assert "<math" not in html.lower()
    assert "frac" in html.lower() or "1" in html
    assert html.count("$") == 2
