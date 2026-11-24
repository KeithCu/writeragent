# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for MathML-aware HTML import into Writer."""

from __future__ import annotations

from typing import Any

from plugin.framework.uno_context import enum_has_more
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.writer import html_import
from plugin.writer.math.math_mml_convert import MATH_CLSID, convert_mathml_to_starmath, insert_writer_math_formula


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
    mml = (
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        "<mrow><mi>x</mi><mo>=</mo><mfrac><mn>1</mn><mn>2</mn></mfrac></mrow>"
        "</math>"
    )
    res = convert_mathml_to_starmath(ctx, mml)
    assert res.ok, res.error_message
    assert res.starmath.strip() == "{ x = { frac { 1 } { 2 } } }", res.starmath


@native_test
@with_native_doc("writer")
def test_replace_full_document_html_plus_inline_math(ctx: Any, doc: Any) -> None:
    html = (
        "<p>Hello</p>"
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        "<mrow><mi>t</mi></mrow>"
        "</math>"
        "<p>World</p>"
    )
    html_import.replace_full_document(doc, ctx, html)
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
    m = (
        '<math display="block" xmlns="http://www.w3.org/1998/Math/MathML">'
        "<mrow><mi>z</mi></mrow></math>"
    )
    html_import.replace_full_document(doc, ctx, m)
    assert _embed_count(doc) >= 1


@native_test
@with_native_doc("writer")
def test_apply_document_content_end_with_mathml(ctx: Any, doc: Any) -> None:
    """End-to-end: ``apply_content`` tool on a hidden doc with MathML HTML."""
    from plugin.app import get_services, get_tools
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
        "apply_content",
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
    html = r"<p>Hi</p><p>\(x^2\)</p><p>Bye</p>"
    html_import.replace_full_document(doc, ctx, html)
    assert _embed_count(doc) >= 1
    body = doc.getText().getString()
    assert "Hi" in body and "Bye" in body


@native_test
@with_native_doc("writer")
def test_replace_full_document_tex_display_dollars(ctx: Any, doc: Any) -> None:
    html = r"<p>Intro</p>$$\frac{1}{2}$$<p>Outro</p>"
    html_import.replace_full_document(doc, ctx, html)
    assert _embed_count(doc) >= 1
    body = doc.getText().getString()
    assert "Intro" in body and "Outro" in body


@native_test
@with_native_doc("writer")
def test_replace_full_document_mixed_mathml_and_tex(ctx: Any, doc: Any) -> None:
    html = (
        r"<p>A</p>"
        r'<math xmlns="http://www.w3.org/1998/Math/MathML"><mrow><mi>t</mi></mrow></math>'
        r"<p>B</p>"
        r"\(y\)"
        r"<p>C</p>"
    )
    html_import.replace_full_document(doc, ctx, html)
    assert _embed_count(doc) >= 2
    body = doc.getText().getString()
    assert "A" in body and "B" in body and "C" in body


@native_test
@with_native_doc("writer")
def test_insert_formula_display_block_centered_and_keeps_alignment(ctx: Any, doc: Any) -> None:
    for initial_adjust in (None, 1):
        text = doc.getText()
        text.setString("")
        cur = text.createTextCursor()
        cur.gotoEnd(False)

        text.insertString(cur, "Before", False)
        if initial_adjust is not None:
            cur.setPropertyValue("ParaAdjust", initial_adjust)
        insert_writer_math_formula(doc, cur, "x = y", display_block=True)
        text.insertString(cur, "After", False)

        paragraphs = []
        enum = text.createEnumeration()
        while enum_has_more(enum):
            p = enum.nextElement()
            if p.supportsService("com.sun.star.text.Paragraph"):
                paragraphs.append(p)

        assert len(paragraphs) == 3, f"Expected 3 paragraphs, got {len(paragraphs)}"
        assert paragraphs[0].getString() == "Before"
        assert paragraphs[2].getString() == "After"
        assert paragraphs[1].getPropertyValue("ParaAdjust") == 3  # Center
        # Trailing paragraph keeps the alignment it had before the formula.
        assert paragraphs[2].getPropertyValue("ParaAdjust") == paragraphs[0].getPropertyValue("ParaAdjust")
        if initial_adjust is not None:
            assert paragraphs[0].getPropertyValue("ParaAdjust") == initial_adjust
            assert paragraphs[2].getPropertyValue("ParaAdjust") == initial_adjust  # Right (not 0)


@native_test
@with_native_doc("writer")
def test_recorded_formula_inserts_reject_and_accept_uno(ctx: Any, doc: Any) -> None:
    from plugin.framework.uno_context import execute_dispatch

    text = doc.getText()
    text.setString("hello")
    doc.setPropertyValue("RecordChanges", True)
    try:
        # Formula-only insert -> Reject removes it.
        cur = text.createTextCursor()
        cur.gotoEnd(False)
        insert_writer_math_formula(doc, cur, "x^2", display_block=False)
        assert _embed_count(doc) == 1
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert _embed_count(doc) == 0
        assert text.getString() == "hello"

        # Inline formula with surrounding text -> Accept keeps "pre" / Frame / "post" order.
        text.setString("")
        cur = text.createTextCursor()
        html_import.replace_single_range_with_content(doc, cur, "<p>pre \\(a + b\\) post</p>", ctx)
        assert _embed_count(doc) == 1
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        assert _embed_count(doc) == 1
        para = text.createEnumeration().nextElement()
        portions = []
        pe = para.createEnumeration()
        while enum_has_more(pe):
            p = pe.nextElement()
            ptype = p.getPropertyValue("TextPortionType")
            if ptype == "Frame":
                portions.append("Frame")
            elif p.getString():
                portions.append(p.getString())
        assert [p.strip() for p in portions] == ["pre", "Frame", "post"], portions

        # Display formula -> Reject removes it.
        cur = text.createTextCursor()
        cur.gotoEnd(False)
        insert_writer_math_formula(doc, cur, "c = d", display_block=True)
        assert _embed_count(doc) == 2
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert _embed_count(doc) == 1
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_apply_content_inline_math_keeps_space_after_formula_uno(ctx: Any, doc: Any) -> None:
    from plugin.app import get_services, get_tools
    from plugin.framework.tool import ToolContext
    from plugin.framework.uno_context import execute_dispatch

    text = doc.getText()
    ctx_tool = ToolContext(doc, ctx, "writer", get_services(), "test")

    for rec in (False, True):
        text.setString("")
        doc.setPropertyValue("RecordChanges", rec)
        try:
            res = get_tools().execute(
                "apply_content",
                ctx_tool,
                content=r"<p>pre \(x\) post</p>",
                target="end",
            )
            assert res.get("status") == "ok", res
            para_portions = []
            te = text.createEnumeration()
            while enum_has_more(te):
                para = te.nextElement()
                if not para.supportsService("com.sun.star.text.Paragraph"):
                    continue
                portions = []
                pe = para.createEnumeration()
                while enum_has_more(pe):
                    p = pe.nextElement()
                    ptype = p.getPropertyValue("TextPortionType")
                    if ptype == "Frame":
                        portions.append("Frame")
                    elif p.getString():
                        portions.append(p.getString())
                if "Frame" in portions:
                    para_portions = portions
                    break
            assert "Frame" in para_portions, (rec, text.getString())
            frame_idx = para_portions.index("Frame")
            assert para_portions[frame_idx + 1 :] == [" post"], para_portions
            if rec:
                execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
                assert _embed_count(doc) == 0
                assert text.getString() == ""
        finally:
            doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_mixed_math_import_paragraphs_styles_lists_cells_and_inline_uno(ctx: Any, doc: Any) -> None:
    from plugin.app import get_services, get_tools
    from plugin.framework.tool import ToolContext
    from plugin.framework.uno_context import execute_dispatch

    text = doc.getText()
    ctx_tool = ToolContext(doc, ctx, "writer", get_services(), "test")

    def _paras() -> list[Any]:
        out = []
        te = text.createEnumeration()
        while enum_has_more(te):
            el = te.nextElement()
            if hasattr(el, "getString"):
                out.append(el)
        return out

    def _clear_untracked() -> None:
        doc.setPropertyValue("RecordChanges", False)
        text.setString("")
        doc.setPropertyValue("RecordChanges", rec)

    for rec in (False, True):
        # Recording opens an absorb break at target='end' so Reject keeps the empty doc's style.
        extra = 1 if rec else 0
        try:
            # 1. Multiple formulas in one paragraph -> 1 paragraph
            _clear_untracked()
            r1 = get_tools().execute("apply_content", ctx_tool, content=r"<p>pre \(x\) and \(y\) post</p>", target="end")
            assert r1.get("status") == "ok", r1
            assert _embed_count(doc) == 2
            assert len(_paras()) == 1 + extra

            # 2. Bare inline math -> 1 paragraph
            _clear_untracked()
            r2 = get_tools().execute("apply_content", ctx_tool, content=r"pre \(x\) post", target="end")
            assert r2.get("status") == "ok", r2
            assert _embed_count(doc) == 1
            assert len(_paras()) == 1 + extra

            # 3. Two paragraphs -> 2 paragraphs
            _clear_untracked()
            r3 = get_tools().execute("apply_content", ctx_tool, content=r"<p>a \(x\)</p><p>b</p>", target="end")
            assert r3.get("status") == "ok", r3
            assert len(_paras()) == 2 + extra

            # 4. Heading style, list, table cell, and \theta
            _clear_untracked()
            r4 = get_tools().execute(
                "apply_content",
                ctx_tool,
                content=(
                    r'<h1 data-lo-style="Heading1">Title \(\theta\)</h1>'
                    r'<ul><li>one \(x\)</li><li>two</li></ul>'
                    r'<table><tr><td>cell \(y\)</td><td>right</td></tr></table>'
                ),
                target="end",
            )
            assert r4.get("status") == "ok", r4
            assert doc.getTextTables().getCount() == 1
            tbl = doc.getTextTables().getByIndex(0)
            assert "cell" in tbl.getCellByName("A1").getString()
            paras4 = _paras()
            assert paras4[extra].getPropertyValue("ParaStyleName") == "Heading 1"
            assert any(bool(p.getPropertyValue("NumberingStyleName")) for p in paras4)
            embeds = doc.getEmbeddedObjects()
            formulas = [embeds.getByIndex(i).getEmbeddedObject().Formula or "" for i in range(embeds.getCount())]
            assert any(("theta" in f or "\u03b8" in f) for f in formulas), formulas

            # 5. Display math between paragraphs -> no empty neighbours, centered
            _clear_untracked()
            r5 = get_tools().execute("apply_content", ctx_tool, content="a\n\n$$x$$\n\nb", target="end")
            assert r5.get("status") == "ok", r5
            paras5 = _paras()[extra:]
            assert [p.getString().strip() for p in paras5] == ["a", "", "b"]
            assert paras5[1].getPropertyValue("ParaAdjust") == 3

            # 6. Inline search replace mid-paragraph keeps host paragraph; Reject restores text
            doc.setPropertyValue("RecordChanges", False)
            text.setString("hello MATCH world")
            doc.setPropertyValue("RecordChanges", rec)
            r6 = get_tools().execute(
                "apply_content", ctx_tool, target="search", old_content="MATCH", content=r"\(x\)"
            )
            assert r6.get("status") == "ok", r6
            assert len(_paras()) == 1
            if rec:
                execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
                assert text.getString() == "hello MATCH world"
        finally:
            doc.setPropertyValue("RecordChanges", False)

