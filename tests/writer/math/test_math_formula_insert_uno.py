# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for MathML-aware HTML import into Writer."""

from __future__ import annotations

from typing import Any

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import skip_windows_leftover_hidden_mathml, with_native_doc
from plugin.writer import format as format_support
from plugin.writer.math.math_mml_convert import MATH_CLSID, convert_mathml_to_starmath, insert_writer_math_formula


def _skip_windows_leftover_hidden_mathml() -> None:
    """GHA 34678020608: latex skip fired; this Hidden ``_blank`` ``.mml`` hung 30s."""
    skip_windows_leftover_hidden_mathml("math formula Hidden _blank .mml")


def _embed_count(doc: Any) -> int:
    eo = doc.getEmbeddedObjects()
    return len(eo.getElementNames())


def _first_math_formula(doc: Any) -> str:
    eo = doc.getEmbeddedObjects()
    names = eo.getElementNames()
    for n in names:
        obj = eo.getByName(n)
        try:
            if str(getattr(obj, "CLSID", "")).lower() == MATH_CLSID.lower():
                inner = obj.getEmbeddedObject()
                return str(inner.Formula)
        except Exception:
            continue
    return ""


@native_test
@with_native_doc("writer")
def test_convert_mathml_to_starmath_fraction(ctx: Any, doc: Any) -> None:
    _skip_windows_leftover_hidden_mathml()
    mml = (
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        "<mrow><mi>x</mi><mo>=</mo><mfrac><mn>1</mn><mn>2</mn></mfrac></mrow>"
        "</math>"
    )
    res = convert_mathml_to_starmath(ctx, mml)
    assert res.ok, res.error_message
    assert res.starmath
    assert "frac" in res.starmath.lower() or "=" in res.starmath


@native_test
@with_native_doc("writer")
def test_replace_full_document_html_plus_inline_math(ctx: Any, doc: Any) -> None:
    _skip_windows_leftover_hidden_mathml()
    html = (
        "<p>Hello</p>"
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        "<mrow><mi>t</mi></mrow>"
        "</math>"
        "<p>World</p>"
    )
    format_support.replace_full_document(doc, ctx, html, config_svc=None)
    assert _embed_count(doc) >= 1
    body = doc.getText().getString()
    assert "Hello" in body
    assert "World" in body


@native_test
@with_native_doc("writer")
def test_insert_formula_readable_formula_property(ctx: Any, doc: Any) -> None:
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoEnd(False)

    insert_writer_math_formula(
        doc, cur, "a + b", display_block=False
    )
    assert _embed_count(doc) >= 1
    f = _first_math_formula(doc)
    assert "a" in f and "b" in f


@native_test
@with_native_doc("writer")
def test_display_math_inserts_embed(ctx: Any, doc: Any) -> None:
    _skip_windows_leftover_hidden_mathml()
    m = (
        '<math display="block" xmlns="http://www.w3.org/1998/Math/MathML">'
        "<mrow><mi>z</mi></mrow></math>"
    )
    format_support.replace_full_document(doc, ctx, m, config_svc=None)
    assert _embed_count(doc) >= 1


@native_test
@with_native_doc("writer")
def test_apply_document_content_end_with_mathml(ctx: Any, doc: Any) -> None:
    """End-to-end: ``apply_document_content`` tool on a hidden doc with MathML HTML."""
    _skip_windows_leftover_hidden_mathml()
    from plugin.main import get_services, get_tools
    from plugin.framework.tool import ToolContext

    text = doc.getText()
    text.setString("")
    ctx_tool = ToolContext(doc, ctx, "writer", get_services(), "test")
    content = (
        "<p>Intro</p>"
        '<math xmlns="http://www.w3.org/1998/Math/MathML"><mrow><mi>q</mi></mrow></math>'
        "<p>Outro</p>"
    )
    res = get_tools().execute(
        "apply_document_content",
        ctx_tool,
        content=content,
        target="end",
    )
    assert res.get("status") == "ok", res
    body = doc.getText().getString()
    assert "Intro" in body and "Outro" in body
    assert _embed_count(doc) >= 1


@native_test
@with_native_doc("writer")
def test_replace_full_document_tex_inline(ctx: Any, doc: Any) -> None:
    _skip_windows_leftover_hidden_mathml()
    html = r"<p>Hi</p><p>\(x^2\)</p><p>Bye</p>"
    format_support.replace_full_document(doc, ctx, html, config_svc=None)
    assert _embed_count(doc) >= 1
    body = doc.getText().getString()
    assert "Hi" in body and "Bye" in body


@native_test
@with_native_doc("writer")
def test_replace_full_document_tex_display_dollars(ctx: Any, doc: Any) -> None:
    _skip_windows_leftover_hidden_mathml()
    html = r"<p>Intro</p>$$\frac{1}{2}$$<p>Outro</p>"
    format_support.replace_full_document(doc, ctx, html, config_svc=None)
    assert _embed_count(doc) >= 1
    body = doc.getText().getString()
    assert "Intro" in body and "Outro" in body


@native_test
@with_native_doc("writer")
def test_replace_full_document_mixed_mathml_and_tex(ctx: Any, doc: Any) -> None:
    _skip_windows_leftover_hidden_mathml()
    html = (
        r"<p>A</p>"
        r'<math xmlns="http://www.w3.org/1998/Math/MathML"><mrow><mi>t</mi></mrow></math>'
        r"<p>B</p>"
        r"\(y\)"
        r"<p>C</p>"
    )
    format_support.replace_full_document(doc, ctx, html, config_svc=None)
    assert _embed_count(doc) >= 2
    body = doc.getText().getString()
    assert "A" in body and "B" in body and "C" in body


@native_test
@with_native_doc("writer")
def test_insert_formula_display_block_centered(ctx: Any, doc: Any) -> None:
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    cur.gotoEnd(False)

    text.insertString(cur, "Before", False)
    insert_writer_math_formula(
        doc, cur, "x = y", display_block=True
    )
    text.insertString(cur, "After", False)

    paragraphs = []
    enum = text.createEnumeration()
    while enum.hasMoreElements():
        p = enum.nextElement()
        if p.supportsService("com.sun.star.text.Paragraph"):
            paragraphs.append(p)

    assert len(paragraphs) == 3, f"Expected 3 paragraphs, got {len(paragraphs)}"
    assert paragraphs[0].getString() == "Before"
    assert paragraphs[2].getString() == "After"
    assert paragraphs[1].getPropertyValue("ParaAdjust") == 3  # Center
    assert paragraphs[2].getPropertyValue("ParaAdjust") == 0  # Left / Standard

