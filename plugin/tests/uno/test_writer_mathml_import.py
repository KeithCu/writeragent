# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for MathML-aware HTML import into Writer."""

from __future__ import annotations

from typing import Any

from plugin.framework.uno_context import get_desktop
from plugin.modules.writer import format_support
from plugin.modules.writer.math_formula_insert import MATH_CLSID
from plugin.modules.writer.math_mml_convert import convert_mathml_to_starmath
from plugin.testing_runner import native_test, setup, teardown

_test_doc: Any = None
_test_ctx: Any = None


@setup
def setup_mathml_tests(ctx: Any) -> None:
    global _test_doc, _test_ctx
    _test_ctx = ctx
    import uno

    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    desktop = get_desktop(ctx)
    _test_doc = desktop.loadComponentFromURL(
        "private:factory/swriter", "_blank", 0, (hidden_prop,)
    )
    assert _test_doc is not None


@teardown
def teardown_mathml_tests(ctx: Any) -> None:
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


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
def test_convert_mathml_to_starmath_fraction() -> None:
    assert _test_ctx is not None
    mml = (
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        "<mrow><mi>x</mi><mo>=</mo><mfrac><mn>1</mn><mn>2</mn></mfrac></mrow>"
        "</math>"
    )
    res = convert_mathml_to_starmath(_test_ctx, mml)
    assert res.ok, res.error_message
    assert res.starmath
    assert "frac" in res.starmath.lower() or "=" in res.starmath


@native_test
def test_replace_full_document_html_plus_inline_math() -> None:
    assert _test_doc is not None and _test_ctx is not None
    html = (
        "<p>Hello</p>"
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        "<mrow><mi>t</mi></mrow>"
        "</math>"
        "<p>World</p>"
    )
    format_support.replace_full_document(_test_doc, _test_ctx, html, config_svc=None)
    assert _embed_count(_test_doc) >= 1
    body = _test_doc.getText().getString()
    assert "Hello" in body
    assert "World" in body


@native_test
def test_insert_formula_readable_formula_property() -> None:
    assert _test_doc is not None
    text = _test_doc.getText()
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    from plugin.modules.writer.math_formula_insert import insert_writer_math_formula

    insert_writer_math_formula(
        _test_doc, cur, "a + b", display_block=False
    )
    assert _embed_count(_test_doc) >= 1
    f = _first_math_formula(_test_doc)
    assert "a" in f and "b" in f


@native_test
def test_display_math_inserts_embed() -> None:
    assert _test_doc is not None and _test_ctx is not None
    m = (
        '<math display="block" xmlns="http://www.w3.org/1998/Math/MathML">'
        "<mrow><mi>z</mi></mrow></math>"
    )
    format_support.replace_full_document(_test_doc, _test_ctx, m, config_svc=None)
    assert _embed_count(_test_doc) >= 1


@native_test
def test_apply_document_content_end_with_mathml() -> None:
    """End-to-end: ``apply_document_content`` tool on a hidden doc with MathML HTML."""
    assert _test_doc is not None and _test_ctx is not None
    from plugin.main import get_services, get_tools
    from plugin.framework.tool import ToolContext

    text = _test_doc.getText()
    text.setString("")
    ctx_tool = ToolContext(_test_doc, _test_ctx, "writer", get_services(), "test")
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
    body = _test_doc.getText().getString()
    assert "Intro" in body and "Outro" in body
    assert _embed_count(_test_doc) >= 1


@native_test
def test_replace_full_document_tex_inline() -> None:
    assert _test_doc is not None and _test_ctx is not None
    html = r"<p>Hi</p><p>\(x^2\)</p><p>Bye</p>"
    format_support.replace_full_document(_test_doc, _test_ctx, html, config_svc=None)
    assert _embed_count(_test_doc) >= 1
    body = _test_doc.getText().getString()
    assert "Hi" in body and "Bye" in body


@native_test
def test_replace_full_document_tex_display_dollars() -> None:
    assert _test_doc is not None and _test_ctx is not None
    html = r"<p>Intro</p>$$\frac{1}{2}$$<p>Outro</p>"
    format_support.replace_full_document(_test_doc, _test_ctx, html, config_svc=None)
    assert _embed_count(_test_doc) >= 1
    body = _test_doc.getText().getString()
    assert "Intro" in body and "Outro" in body


@native_test
def test_replace_full_document_mixed_mathml_and_tex() -> None:
    assert _test_doc is not None and _test_ctx is not None
    html = (
        r"<p>A</p>"
        r'<math xmlns="http://www.w3.org/1998/Math/MathML"><mrow><mi>t</mi></mrow></math>'
        r"<p>B</p>"
        r"\(y\)"
        r"<p>C</p>"
    )
    format_support.replace_full_document(_test_doc, _test_ctx, html, config_svc=None)
    assert _embed_count(_test_doc) >= 2
    body = _test_doc.getText().getString()
    assert "A" in body and "B" in body and "C" in body
