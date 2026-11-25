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
def test_mathml_without_default_xmlns_converts_uno(ctx: Any, doc: Any) -> None:
    for mml in (
        "<math><mi>x</mi><mo>+</mo><mi>y</mi></math>",
        "<math xmlns=''><mi>x</mi><mo>+</mo><mi>y</mi></math>",
    ):
        res = convert_mathml_to_starmath(ctx, mml)
        assert res.ok, res.error_message
        assert res.starmath.strip() == "x + y", res.starmath

    html_import.replace_full_document(doc, ctx, "<p>a <math><mi>x</mi><mo>+</mo><mi>y</mi></math> b</p>")
    assert _embed_count(doc) == 1
    assert _first_math_formula(doc).strip() == "x + y"
    assert "[Math import failed]" not in doc.getText().getString()


@native_test
@with_native_doc("writer")
def test_mixed_math_import_paragraphs_styles_lists_cells_and_inline_uno(ctx: Any, doc: Any) -> None:
    from unittest.mock import patch

    from plugin.app import get_services, get_tools
    from plugin.framework.tool import ToolContext
    from plugin.framework.uno_context import execute_dispatch, uno_same
    from plugin.writer.math.math_mml_convert import MathConversionResult

    text = doc.getText()
    ctx_tool = ToolContext(doc, ctx, "writer", get_services(), "test")

    def _paras(xtext: Any = text) -> list[Any]:
        out = []
        te = xtext.createEnumeration()
        while enum_has_more(te):
            el = te.nextElement()
            if hasattr(el, "getString"):
                out.append(el)
        return out

    def _portions(para: Any) -> list[str]:
        out = []
        pe = para.createEnumeration()
        while enum_has_more(pe):
            p = pe.nextElement()
            ptype = p.getPropertyValue("TextPortionType")
            if ptype == "Redline":
                continue
            if ptype == "Frame":
                out.append("Frame")
            elif p.getString():
                s = p.getString()
                if out and out[-1] != "Frame":
                    out[-1] += s
                else:
                    out.append(s)
        return out

    def _clear_untracked() -> None:
        doc.setPropertyValue("RecordChanges", False)
        text.setString("")
        for i in range(doc.getTextTables().getCount() - 1, -1, -1):
            text.removeTextContent(doc.getTextTables().getByIndex(i))
        doc.setPropertyValue("RecordChanges", rec)

    for rec in (False, True):
        # Recording opens an absorb break at target='end' so Reject keeps the empty doc's style.
        extra = 1 if rec else 0
        try:
            # 1. Multiple formulas in one paragraph -> 1 paragraph with exact order
            _clear_untracked()
            r1 = get_tools().execute("apply_content", ctx_tool, content=r"<p>pre \(x\) and \(y\) post</p>", target="end")
            assert r1.get("status") == "ok", r1
            assert _embed_count(doc) == 2
            paras1 = _paras()
            assert len(paras1) == 1 + extra
            assert _portions(paras1[extra]) == ["pre ", "Frame", " and ", "Frame", " post"]
            if rec:
                execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
                assert _embed_count(doc) == 0 and not text.getString().strip()

            # 2. Bare inline math -> 1 paragraph with exact order
            _clear_untracked()
            r2 = get_tools().execute("apply_content", ctx_tool, content=r"pre \(x\) post", target="end")
            assert r2.get("status") == "ok", r2
            assert _embed_count(doc) == 1
            paras2 = _paras()
            assert len(paras2) == 1 + extra
            assert _portions(paras2[extra]) == ["pre ", "Frame", " post"]
            if rec:
                execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
                assert _embed_count(doc) == 0 and not text.getString().strip()

            # 3. Two paragraphs -> 2 paragraphs with exact text
            _clear_untracked()
            r3 = get_tools().execute("apply_content", ctx_tool, content=r"<p>a \(x\)</p><p>b</p>", target="end")
            assert r3.get("status") == "ok", r3
            paras3 = _paras()
            assert len(paras3) == 2 + extra
            assert _portions(paras3[extra]) == ["a ", "Frame"]
            assert _portions(paras3[extra + 1]) == ["b"]
            if rec:
                execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
                assert _embed_count(doc) == 0 and not text.getString().strip()

            # 4. Heading style, two-item list, table cell (anchor inside A1), and \theta
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
            a1 = tbl.getCellByName("A1")
            assert "cell" in a1.getString()
            paras4 = _paras()
            assert paras4[extra].getPropertyValue("ParaStyleName") == "Heading 1"
            list_paras = [
                p
                for p in paras4
                if (p.getPropertyValue("NumberingStyleName") or "").strip().lower() not in ("", "outline")
            ]
            assert len(list_paras) == 2
            embeds = doc.getEmbeddedObjects()
            assert any(uno_same(embeds.getByIndex(i).getAnchor().getText(), a1) for i in range(embeds.getCount()))
            formulas = [embeds.getByIndex(i).getEmbeddedObject().Formula or "" for i in range(embeds.getCount())]
            assert any(("theta" in f or "\u03b8" in f) for f in formulas), formulas
            if rec:
                execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
                assert _embed_count(doc) == 0 and not text.getString().strip()

            # 5. Display math between paragraphs -> no empty neighbours, centered
            _clear_untracked()
            r5 = get_tools().execute("apply_content", ctx_tool, content="a\n\n$$x$$\n\nb", target="end")
            assert r5.get("status") == "ok", r5
            paras5 = _paras()[extra:]
            assert [p.getString().strip() for p in paras5] == ["a", "", "b"]
            assert paras5[1].getPropertyValue("ParaAdjust") == 3
            if rec:
                execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
                assert _embed_count(doc) == 0 and not text.getString().strip()

            # 6. Inline search replace mid-paragraph (with and without edge whitespace, Accept and Reject)
            for new_c, expected_portions in (
                (r"\(x\)", ["hello ", "Frame", " world"]),
                (r" \(x\) ", ["hello  ", "Frame", "  world"]),
            ):
                _clear_untracked()
                doc.setPropertyValue("RecordChanges", False)
                text.setString("hello MATCH world")
                doc.setPropertyValue("RecordChanges", rec)
                r6 = get_tools().execute(
                    "apply_content", ctx_tool, target="search", old_content="MATCH", content=new_c
                )
                assert r6.get("status") == "ok", r6
                paras6 = _paras()
                assert len(paras6) == 1
                if rec:
                    execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
                    paras6 = _paras()
                assert _portions(paras6[0]) == expected_portions, (new_c, _portions(paras6[0]))

                if rec:
                    _clear_untracked()
                    doc.setPropertyValue("RecordChanges", False)
                    text.setString("hello MATCH world")
                    doc.setPropertyValue("RecordChanges", True)
                    r6_rej = get_tools().execute(
                        "apply_content", ctx_tool, target="search", old_content="MATCH", content=new_c
                    )
                    assert r6_rej.get("status") == "ok", r6_rej
                    execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
                    assert text.getString() == "hello MATCH world"
                    assert _embed_count(doc) == 0

            # 6b. Heading inline replace with trailing newline does not add a line break or split
            _clear_untracked()
            doc.setPropertyValue("RecordChanges", False)
            text.setString("Old title")
            cur_h = text.createTextCursor()
            cur_h.gotoStart(False)
            cur_h.gotoEnd(True)
            cur_h.setPropertyValue("ParaStyleName", "Heading 1")
            doc.setPropertyValue("RecordChanges", rec)
            r6h = get_tools().execute(
                "apply_content", ctx_tool, target="search", old_content="Old title", content="<b>New title</b>\n"
            )
            assert r6h.get("status") == "ok", r6h
            if rec:
                execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
            paras6h = _paras()
            assert len(paras6h) == 1
            assert paras6h[0].getString() == "New title"
            assert paras6h[0].getPropertyValue("ParaStyleName") == "Heading 1"

            # 7. Inline search replace inside a table cell keeps cell paragraph and anchor
            _clear_untracked()
            doc.setPropertyValue("RecordChanges", False)
            tbl7 = doc.createInstance("com.sun.star.text.TextTable")
            tbl7.initialize(1, 2)
            text.insertTextContent(text.getEnd(), tbl7, False)
            tbl7.getCellByName("A1").setString("cell MATCH end")
            doc.setPropertyValue("RecordChanges", rec)
            r7 = get_tools().execute(
                "apply_content", ctx_tool, target="search", old_content="MATCH", content=r"\(x\)"
            )
            assert r7.get("status") == "ok", r7
            a1_7 = tbl7.getCellByName("A1")
            if rec:
                execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
            a1_paras = _paras(a1_7)
            assert len(a1_paras) == 1
            assert _portions(a1_paras[0]) == ["cell ", "Frame", " end"]

            # 8. Mid-body failed conversion stays in place and keeps following paragraphs
            from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK

            _clear_untracked()
            doc.setPropertyValue("RecordChanges", False)
            cur8 = text.createTextCursor()
            text.insertString(cur8, "first", False)
            text.insertControlCharacter(cur8, PARAGRAPH_BREAK, False)
            text.insertString(cur8, "second MATCH third", False)
            text.insertControlCharacter(cur8, PARAGRAPH_BREAK, False)
            text.insertString(cur8, "fourth", False)
            doc.setPropertyValue("RecordChanges", rec)
            failed = MathConversionResult(False, None, "bad tex")
            with patch("plugin.writer.html_import.convert_latex_to_starmath", return_value=failed):
                r8 = get_tools().execute(
                    "apply_content", ctx_tool, target="search", old_content="MATCH", content=r"\(bad\)"
                )
            assert r8.get("status") == "ok", r8
            if rec:
                execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
            paras8 = [p.getString() for p in _paras()]
            assert paras8 == ["first", "second [Math import failed] bad third", "fourth"], paras8
        finally:
            doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_position_before_after_math_uno(ctx: Any, doc: Any) -> None:
    from plugin.framework.uno_context import execute_dispatch
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.apply_document_content import ApplyDocumentContent

    apply_tool = ApplyDocumentContent()
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")
    text = doc.getText()

    def _reset(body: str) -> None:
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.gotoEnd(True)
        cur.setString(body)

    def _para_portions(para: Any) -> list[str]:
        out: list[str] = []
        pe = para.createEnumeration()
        while enum_has_more(pe):
            p = pe.nextElement()
            ptype = p.getPropertyValue("TextPortionType")
            if ptype == "Redline":
                continue
            out.append("Frame" if ptype == "Frame" else p.getString())
        return out

    def _paras() -> list[Any]:
        out: list[Any] = []
        pe = text.createEnumeration()
        while enum_has_more(pe):
            el = pe.nextElement()
            if el.supportsService("com.sun.star.text.Paragraph"):
                out.append(el)
        return out

    for rec in (False, True):
        try:
            for pos in ("before", "after"):
                # 1. Inline math
                doc.setPropertyValue("RecordChanges", False)
                _reset("mid MATCH end")
                base_embeds = _embed_count(doc)
                doc.setPropertyValue("RecordChanges", rec)
                res = apply_tool.execute(
                    tool_ctx,
                    target="search", old_content="MATCH", content=r"\(x^2\)", position=pos,
                )
                assert res.get("status") == "ok", res
                assert res.get("inserted") is True, res
                assert len(res.get("math_objects") or []) == 1, res
                paras = _paras()
                assert len(paras) == 1
                expected_inline = ["mid ", "Frame", "MATCH end"] if pos == "before" else ["mid MATCH", "Frame", " end"]
                assert _para_portions(paras[0]) == expected_inline, (rec, pos, _para_portions(paras[0]))
                assert _embed_count(doc) == base_embeds + 1
                if rec:
                    execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
                else:
                    doc.getUndoManager().undo()
                assert text.getString() == "mid MATCH end"
                assert [p.getString() for p in _paras()] == ["mid MATCH end"]
                assert _embed_count(doc) == base_embeds

                # 2. Block HTML with math
                doc.setPropertyValue("RecordChanges", False)
                _reset("mid MATCH end")
                base_embeds = _embed_count(doc)
                doc.setPropertyValue("RecordChanges", rec)
                res_blk = apply_tool.execute(
                    tool_ctx,
                    target="search", old_content="MATCH", content=r"<p>A \(x\) B</p>", position=pos,
                )
                assert res_blk.get("status") == "ok", res_blk
                assert len(res_blk.get("math_objects") or []) == 1, res_blk
                paras = _paras()
                assert len(paras) == 2
                math_idx, orig_idx = (0, 1) if pos == "before" else (1, 0)
                assert _para_portions(paras[math_idx]) == ["A ", "Frame", " B"]
                assert paras[orig_idx].getString() == "mid MATCH end"
                assert _embed_count(doc) == base_embeds + 1
                if rec:
                    execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
                else:
                    doc.getUndoManager().undo()
                assert text.getString() == "mid MATCH end"
                assert [p.getString() for p in _paras()] == ["mid MATCH end"]
                assert _embed_count(doc) == base_embeds

                # 3. Display math centered paragraph
                doc.setPropertyValue("RecordChanges", False)
                _reset("mid MATCH end")
                base_embeds = _embed_count(doc)
                doc.setPropertyValue("RecordChanges", rec)
                res_disp = apply_tool.execute(
                    tool_ctx,
                    target="search", old_content="MATCH", content="$$y=1$$", position=pos,
                )
                assert res_disp.get("status") == "ok", res_disp
                assert len(res_disp.get("math_objects") or []) == 1, res_disp
                paras = _paras()
                expected_disp = ["mid ", "", "MATCH end"] if pos == "before" else ["mid MATCH", "", " end"]
                assert [p.getString() for p in paras] == expected_disp, (rec, pos, [p.getString() for p in paras])
                assert _para_portions(paras[1]) == ["Frame"]
                assert paras[1].getPropertyValue("ParaAdjust") == 3  # Center
                assert _embed_count(doc) == base_embeds + 1
                if rec:
                    execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
                else:
                    doc.getUndoManager().undo()
                assert text.getString() == "mid MATCH end"
                assert [p.getString() for p in _paras()] == ["mid MATCH end"]
                assert _embed_count(doc) == base_embeds
        finally:
            doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_math_tokens_across_soft_page_break_uno(ctx: Any, doc: Any) -> None:
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.apply_document_content import ApplyDocumentContent

    page_styles = doc.getStyleFamilies().getByName("PageStyles")
    std_page = page_styles.getByName("Standard")
    page_props = ("Width", "Height", "LeftMargin", "RightMargin", "TopMargin", "BottomMargin")
    saved_page = {k: std_page.getPropertyValue(k) for k in page_props}

    para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
    para_props = ("CharHeight", "ParaWidows", "ParaOrphans")
    saved_para: list[tuple[Any, dict[str, Any]]] = []
    for style_name in ("Standard", "Text body"):
        if para_styles.hasByName(style_name):
            st = para_styles.getByName(style_name)
            saved_para.append((st, {k: st.getPropertyValue(k) for k in para_props}))

    try:
        std_page.setPropertyValue("Width", 6000)
        std_page.setPropertyValue("Height", 6000)
        for margin in ("LeftMargin", "RightMargin", "TopMargin", "BottomMargin"):
            std_page.setPropertyValue(margin, 500)
        for st, _saved in saved_para:
            st.setPropertyValue("CharHeight", 30.0)
            st.setPropertyValue("ParaWidows", 0)
            st.setPropertyValue("ParaOrphans", 0)

        tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")

        content = " ".join(rf"word\(x_{{{i}}}\)" for i in range(12))
        res = ApplyDocumentContent().execute(tool_ctx, target="end", content=content)
        assert res.get("status") == "ok", res
        math_objs = res.get("math_objects") or []
        assert len(math_objs) == 12, res
        assert all(m.get("ok") for m in math_objs), math_objs
        assert _embed_count(doc) == 12
        assert "[[WA-MATH" not in doc.getText().getString()
    finally:
        for k, v in saved_page.items():
            std_page.setPropertyValue(k, v)
        for st, saved in saved_para:
            for k, v in saved.items():
                st.setPropertyValue(k, v)

