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
    assert all(h.embed_name for h in hits)


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
    text.insertControlCharacter(cur, 0, False)
    insert_writer_math_formula(doc, cur, "x", display_block=True)

    html = document_to_content(doc, ctx, services=None, scope="full")
    assert html
    assert "<math" not in html.lower()
    assert "frac" in html.lower() or "1" in html
    assert html.count("$") == 6
    assert "$$x$$" in html


@native_test
@with_native_doc("writer")
def test_body_table_and_header_math_order_uno(ctx: Any, doc: Any) -> None:
    """Document order: body formula, table cell formula, body formula. Header math must not shift body TeX."""
    _skip_windows_leftover_hidden_mathml()
    from plugin.writer.html_export import document_to_content

    # Header formula
    styles = doc.getStyleFamilies().getByName("PageStyles")
    style_name = "Standard" if styles.hasByName("Standard") else styles.getElementNames()[0]
    style = styles.getByName(style_name)
    style.setPropertyValue("HeaderIsOn", True)
    header_text = style.getPropertyValue("HeaderText")
    h_cur = header_text.createTextCursor()
    header_text.insertString(h_cur, "Header: ", False)
    insert_writer_math_formula(doc, h_cur, "z", display_block=False)

    # Body formula 1
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    text.insertString(cur, "First: ", False)
    insert_writer_math_formula(doc, cur, "a + 1", display_block=False)
    text.insertControlCharacter(cur, 0, False)

    # Table with cell A1 formula 2
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(1, 1)
    text.insertTextContent(cur, table, False)
    cell = table.getCellByName("A1")
    c_cur = cell.createTextCursor()
    cell.insertString(c_cur, "Cell: ", False)
    insert_writer_math_formula(doc, c_cur, "b + 2", display_block=False)

    # Body formula 3 after table
    cur.gotoEnd(False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Third: ", False)
    insert_writer_math_formula(doc, cur, "c + 3", display_block=False)

    # iter_writer_math_objects walks body and table cells, omitting header
    hits = list(iter_writer_math_objects(doc, ctx))
    assert len(hits) == 3, f"Expected 3 body math objects, got {len(hits)}"
    assert "a + 1" in hits[0].starmath or "a+1" in hits[0].starmath
    assert "b + 2" in hits[1].starmath or "b+2" in hits[1].starmath
    assert "c + 3" in hits[2].starmath or "c+3" in hits[2].starmath

    # Full document HTML export
    html = document_to_content(doc, ctx, services=None, scope="full")
    assert html
    assert "<math" not in html.lower()
    idx1 = html.find("a + 1") if "a + 1" in html else html.find("a+1")
    idx2 = html.find("b + 2") if "b + 2" in html else html.find("b+2")
    idx3 = html.find("c + 3") if "c + 3" in html else html.find("c+3")
    assert idx1 != -1 and idx2 != -1 and idx3 != -1, html
    assert idx1 < idx2 < idx3, html

