# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for HTML import ruby write (apply_content)."""

import os
import re
from typing import Any

import uno  # noqa: F401

from plugin.doc.text_helpers import normalize_linebreaks
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


def _ruby_portions(doc: Any) -> list[tuple[str, str, str | None]]:
    """Return ``(kind, text, RubyText)`` for the first body paragraph."""
    para = doc.getText().createEnumeration().nextElement()
    out = []
    enum = para.createEnumeration()
    while enum.hasMoreElements():
        portion = enum.nextElement()
        kind = str(portion.getPropertyValue("TextPortionType") or "")
        text = portion.getString() or ""
        reading = None
        try:
            reading = portion.getPropertyValue("RubyText")
        except Exception:
            reading = None
        out.append((kind, text, reading))
    return out


@native_test
@with_native_doc("writer")
def test_apply_ruby_html_creates_portions_and_round_trips_uno(ctx: Any, doc: Any) -> None:
    """StarWriter concatenates <rt>; apply must paint RubyText and get must emit <rt>."""
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.get_document_content import GetDocumentContent

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    html = "<p>前<ruby>漢字<rt>かんじ</rt></ruby>後</p>"
    res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=html)
    assert res.get("status") == "ok", res
    assert normalize_linebreaks(doc.getText().getString()).split("\n")[0] == "前漢字後"
    assert "かんじ" not in doc.getText().getString()

    kinds = _ruby_portions(doc)
    assert any(kind == "Ruby" and reading == "かんじ" for kind, _text, reading in kinds), kinds
    assert any(kind == "Text" and text == "漢字" for kind, text, _reading in kinds), kinds

    got = GetDocumentContent().execute(tool_ctx, scope="full")
    assert got.get("status") == "ok", got
    content = got.get("content") or ""
    assert "漢字かんじ" not in content, content
    assert "<ruby>" in content and "<rt>かんじ</rt>" in content, content
    assert "前" in content and "後" in content, content


@native_test
@with_native_doc("writer")
def test_preserve_format_replace_keeps_existing_ruby_uno(ctx: Any, doc: Any) -> None:
    """Plain-text (format-preserving) replace of the base must not drop Ruby marks."""
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.get_document_content import GetDocumentContent

    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "漢字です", False)
    cur.gotoStart(False)
    cur.goRight(2, True)
    cur.setPropertyValue("RubyText", "かんじ")
    cur.setPropertyValue("RubyIsAbove", True)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyDocumentContent().execute(
        tool_ctx, target="search", old_content="漢字", content="漢字",
    )
    assert res.get("status") == "ok", res
    kinds = _ruby_portions(doc)
    assert any(kind == "Ruby" and reading == "かんじ" for kind, _text, reading in kinds), kinds
    got = GetDocumentContent().execute(tool_ctx, scope="full")
    assert "<rt>かんじ</rt>" in (got.get("content") or ""), got


@native_test
@with_native_doc("writer")
def test_apply_ruby_with_math_creates_ruby_on_both_bases_uno(ctx: Any, doc: Any) -> None:
    """A paragraph with a ruby, an inline formula, and a second ruby gets RubyText on both bases."""
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.apply_document_content import ApplyDocumentContent

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    html = "<p><ruby>東<rt>ひがし</rt></ruby> \\(x^2\\) <ruby>西<rt>にし</rt></ruby></p>"
    res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=html)
    assert res.get("status") == "ok", res

    kinds = _ruby_portions(doc)
    assert any(kind == "Ruby" and reading == "ひがし" for kind, _text, reading in kinds), kinds
    assert any(kind == "Ruby" and reading == "にし" for kind, _text, reading in kinds), kinds
    assert any(kind == "Text" and text == "東" for kind, text, _reading in kinds), kinds
    assert any(kind == "Text" and text == "西" for kind, text, _reading in kinds), kinds


@native_test
@with_native_doc("writer")
def test_html_to_search_text_oracle_uno(ctx: Any, doc: Any) -> None:
    """Compare scratch getString() with html_to_search_text after ruby strip."""
    from plugin.framework.html_stripper import html_to_search_text
    from plugin.writer.html_import import insert_html_fragment_at_cursor
    from plugin.writer.html_ruby import extract_and_strip_ruby

    fixtures = [
        "<p>First paragraph</p><p>Second paragraph</p>",
        "<p>Line 1<br>Line 2</p>",
        "<h1>Heading</h1><p>Body paragraph with <b>bold</b> text.</p>",
        "<p><ruby>漢字<rt>かんじ</rt></ruby> text</p>",
    ]
    text = doc.getText()
    for html in fixtures:
        text.setString("")
        cur = text.createTextCursor()
        base_html, _unused = extract_and_strip_ruby(html)
        insert_html_fragment_at_cursor(cur, base_html)
        scratch_text = text.getString().strip().replace("\r\n", "\n")
        search_text = html_to_search_text(base_html).strip()
        assert scratch_text == search_text


@native_test
@with_native_doc("writer")
def test_styled_insert_at_end_cursor_walk_uno(ctx: Any, doc: Any) -> None:
    """Twenty leading paragraphs, a one-paragraph data-lo-style insert at the end."""
    from plugin.writer.html_import import _insert_mixed_or_plain_html

    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    for i in range(20):
        text.insertString(cur, f"Paragraph {i}", False)
        text.insertControlCharacter(cur, 0, False)

    insert_cur = text.createTextCursor()
    insert_cur.gotoEnd(False)
    _insert_mixed_or_plain_html(
        doc, ctx, insert_cur,
        '<h1 data-lo-style="Heading1">New Heading</h1>',
        apply_styles=True,
    )

    enum = text.createEnumeration()
    paras = []
    while enum.hasMoreElements():
        el = enum.nextElement()
        if hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.Paragraph"):
            paras.append(el)

    assert len(paras) >= 21
    # Check that leading paragraphs did not receive Heading 1
    assert paras[0].getPropertyValue("ParaStyleName") != "Heading 1"
    # Check that the inserted heading paragraph did receive Heading 1
    assert paras[20].getPropertyValue("ParaStyleName") == "Heading 1"


def _find_field_by_service(doc: Any, service: str) -> Any | None:
    enum = doc.getTextFields().createEnumeration()
    while enum.hasMoreElements():
        tf = enum.nextElement()
        if hasattr(tf, "supportsService") and tf.supportsService(service):
            return tf
    return None


@native_test
@with_native_doc("writer")
def test_restore_fields_on_writer_inserts_uno(ctx: Any, doc: Any) -> None:
    """replace_full_document, beginning/end insert, and search replace restore live fields."""
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.apply_document_content import ApplyDocumentContent

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    field_html = '<p>Page <span title="page-number" data-subtype="NEXT" data-offset="1"/></p>'

    # 1. replace_full_document
    res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=field_html)
    assert res.get("status") == "ok", res
    tf = _find_field_by_service(doc, "com.sun.star.text.textfield.PageNumber")
    assert tf is not None
    assert getattr(tf.getPropertyValue("SubType"), "value", str(tf.getPropertyValue("SubType"))) == "NEXT"
    assert tf.getPropertyValue("Offset") == 1

    # 2. insert at beginning
    doc.getText().setString("body")
    res = ApplyDocumentContent().execute(tool_ctx, target="beginning", content=field_html)
    assert res.get("status") == "ok", res
    tf = _find_field_by_service(doc, "com.sun.star.text.textfield.PageNumber")
    assert tf is not None
    assert getattr(tf.getPropertyValue("SubType"), "value", str(tf.getPropertyValue("SubType"))) == "NEXT"
    assert tf.getPropertyValue("Offset") == 1

    # 3. insert at end
    doc.getText().setString("body")
    res = ApplyDocumentContent().execute(tool_ctx, target="end", content=field_html)
    assert res.get("status") == "ok", res
    tf = _find_field_by_service(doc, "com.sun.star.text.textfield.PageNumber")
    assert tf is not None
    assert getattr(tf.getPropertyValue("SubType"), "value", str(tf.getPropertyValue("SubType"))) == "NEXT"
    assert tf.getPropertyValue("Offset") == 1

    # 4. search replace with block field html
    doc.getText().setString("target to replace")
    res = ApplyDocumentContent().execute(tool_ctx, target="search", old_content="target to replace", content=field_html)
    assert res.get("status") == "ok", res
    tf = _find_field_by_service(doc, "com.sun.star.text.textfield.PageNumber")
    assert tf is not None
    assert getattr(tf.getPropertyValue("SubType"), "value", str(tf.getPropertyValue("SubType"))) == "NEXT"
    assert tf.getPropertyValue("Offset") == 1

    # 5. inline search replace with <span title="date"/>
    doc.getText().setString("today is DATE here")
    res = ApplyDocumentContent().execute(tool_ctx, target="search", old_content="DATE", content='<span title="date"/>')
    assert res.get("status") == "ok", res
    date_tf = _find_field_by_service(doc, "com.sun.star.text.textfield.DateTime")
    assert date_tf is not None
    assert date_tf.getPropertyValue("IsDate") is True


@native_test
@with_native_doc("writer")
def test_escape_prose_angle_brackets_survives_starwriter_filter_uno(ctx: Any, doc: Any) -> None:
    """Prose angle brackets survive without dropping text, while real HTML tags format."""
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.apply_document_content import ApplyDocumentContent

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content="<p>List<String> tail</p>")
    assert res.get("status") == "ok", res
    plain = doc.getText().getString().strip()
    assert plain == "List<String> tail"

    res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content="<p><b>bold</b></p>")
    assert res.get("status") == "ok", res
    para = doc.getText().createEnumeration().nextElement()
    portion = para.createEnumeration().nextElement()
    assert portion.getString() == "bold"
    assert portion.getPropertyValue("CharWeight") > 100


def _project_body_paragraphs(doc: Any) -> list[tuple[str, str, list[tuple[str, str]]]]:
    from plugin.writer.html_ruby import _iter_ruby_spans_in_paragraph

    enum = doc.getText().createEnumeration()
    paras = []
    while enum.hasMoreElements() is True:
        para = enum.nextElement()
        style = str(para.getPropertyValue("ParaStyleName") or "")
        text = str(para.getString() or "")
        rubies = [(s.base, s.reading) for s in _iter_ruby_spans_in_paragraph(para)]
        paras.append((style, text, rubies))
    if paras and not paras[-1][1]:
        paras.pop()
    return paras


def _norm_live_text(s: str) -> str:
    """Normalize LO live field/list quirks for round-trip compares.

    Replaces nbsp with space, collapses whitespace runs, strips edges and list bullets.
    """
    s = s.replace(" ", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip("• \t")


def _apply_and_read(ctx: Any, doc: Any, html: str) -> tuple[Any, str, str]:
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.get_document_content import GetDocumentContent

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    doc.getText().setString("")
    res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=html)
    assert res.get("status") == "ok", res
    plain = doc.getText().getString()
    assert "[[WA-FIELD:" not in plain
    assert not re.search(r"wa[0-9a-fA-F]+img\d+", plain)
    read = GetDocumentContent().execute(tool_ctx, scope="full")
    assert read.get("status") == "ok", read
    read_html = read.get("content") or ""
    assert "[[WA-FIELD:" not in read_html
    return tool_ctx, plain, read_html


@native_test
@with_native_doc("writer")
def test_html_import_live_roundtrip_nested_blocks_uno(ctx: Any, doc: Any) -> None:
    """Live round-trip for nested blocks sharing one style slot and idempotence."""
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.get_document_content import GetDocumentContent

    nested_html = (
        '<h1 data-lo-style="Heading1">T</h1>'
        '<blockquote><p data-lo-style="Quotations">q</p></blockquote>'
        '<ul><li><p data-lo-style="Standard">a</p></li><li><p data-lo-style="Standard">b</p></li></ul>'
        '<h2 data-lo-style="Heading2">D</h2>'
    )
    tool_ctx, _plain1, read_html1 = _apply_and_read(ctx, doc, nested_html)
    assert "data-lo-style=" in read_html1
    proj1 = _project_body_paragraphs(doc)
    assert [(p[0], p[1]) for p in proj1] == [
        ("Heading 1", "T"),
        ("Quotations", "q"),
        ("Standard", "a"),
        ("Standard", "b"),
        ("Heading 2", "D"),
    ], proj1
    # Idempotence on nested-style fixture. Live LO XHTML re-import of <ul><li>
    # often lands as Text body with a bullet/nbsp prefix instead of Standard.
    res_re = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=read_html1)
    assert res_re.get("status") == "ok", res_re
    read1_re = GetDocumentContent().execute(tool_ctx, scope="full")
    assert read1_re.get("status") == "ok", read1_re
    proj1_re = _project_body_paragraphs(doc)
    assert len(proj1_re) == len(proj1), f"proj1_re={proj1_re} != proj1={proj1}"
    assert [p[2] for p in proj1_re] == [p[2] for p in proj1], (
        f"proj1_re={proj1_re} != proj1={proj1}"
    )
    for ((s0, t0, _r0), (s1, t1, _r1)) in zip(proj1, proj1_re, strict=True):
        text0 = _norm_live_text(t0)
        text1 = _norm_live_text(t1)
        assert text0 == text1, f"proj1_re={proj1_re} != proj1={proj1}"
        if text0 in ("a", "b") and s0 == "Standard":
            assert s1 in ("Standard", "Text body"), f"list style {s1!r} for {text1!r}"
        else:
            assert s0 == s1, f"proj1_re={proj1_re} != proj1={proj1}"


@native_test
@with_native_doc("writer")
def test_html_import_live_roundtrip_unclosed_and_div_uno(ctx: Any, doc: Any) -> None:
    """Live round-trip for unclosed <p> tags and styled <div> wrappers."""
    _tc2, _p2, read_html2 = _apply_and_read(ctx, doc, '<p data-lo-style="Heading1">One<p data-lo-style="Caption">Two')
    assert "data-lo-style=" in read_html2
    proj2 = _project_body_paragraphs(doc)
    assert [p[0] for p in proj2] == ["Heading 1", "Caption"], proj2
    assert [_norm_live_text(p[1]) for p in proj2] == ["One", "Two"], proj2

    _tc3, _p3, read_html3 = _apply_and_read(ctx, doc, '<div data-lo-style="Caption">cap</div><p data-lo-style="Heading1">h</p>')
    assert "data-lo-style=" in read_html3
    proj3 = _project_body_paragraphs(doc)
    assert [(p[0], p[1]) for p in proj3] == [("Caption", "cap"), ("Heading 1", "h")], proj3


@native_test
@with_native_doc("writer")
def test_html_import_live_roundtrip_ruby_and_prose_angles_uno(ctx: Any, doc: Any) -> None:
    """Live round-trip for repeated-base ruby and prose angle brackets."""
    from plugin.writer.get_document_content import GetDocumentContent
    from plugin.writer.html_ruby import _iter_ruby_spans_in_paragraph

    tool_ctx, plain4, _rh4 = _apply_and_read(ctx, doc, "<p>漢字\\n<ruby>漢字<rt>かんじ</rt></ruby></p>")
    para4 = doc.getText().createEnumeration().nextElement()
    spans4 = list(_iter_ruby_spans_in_paragraph(para4))
    assert len(spans4) == 1, spans4
    assert spans4[0].base == "漢字" and spans4[0].reading == "かんじ"
    assert spans4[0].start > 0, f"ruby attached at start: {spans4}"
    range_res = GetDocumentContent().execute(tool_ctx, scope="range", start=0, end=len(plain4))
    assert range_res.get("status") == "ok", range_res
    assert "<rt>かんじ</rt>" in (range_res.get("content") or "")

    _apply_and_read(ctx, doc, "<p>List<String> and 2 < 3</p>")
    proj5 = _project_body_paragraphs(doc)
    assert proj5[0][1] == "List<String> and 2 < 3", proj5


@native_test
@with_native_doc("writer")
def test_html_import_live_roundtrip_fields_and_image_uno(ctx: Any, doc: Any) -> None:
    """Live round-trip for field spans (with idempotence) and linked image."""
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.get_document_content import GetDocumentContent
    from plugin.writer.images.image_tools import insert_image

    tool_ctx, _p6, read_html6 = _apply_and_read(
        ctx, doc, '<p>Page <span title="page-number"/> / <span title="date"/></p>'
    )
    tf_page = _find_field_by_service(doc, "com.sun.star.text.textfield.PageNumber")
    assert tf_page is not None
    tf_date = _find_field_by_service(doc, "com.sun.star.text.textfield.DateTime")
    assert tf_date is not None
    assert tf_date.getPropertyValue("IsDate") is True
    proj6 = _project_body_paragraphs(doc)
    res_re6 = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=read_html6)
    assert res_re6.get("status") == "ok", res_re6
    read6_re = GetDocumentContent().execute(tool_ctx, scope="full")
    assert read6_re.get("status") == "ok", read6_re
    proj6_re = _project_body_paragraphs(doc)
    assert len(proj6_re) == len(proj6), f"proj6_re={proj6_re} != proj6={proj6}"
    assert [p[0] for p in proj6_re] == [p[0] for p in proj6], (
        f"proj6_re={proj6_re} != proj6={proj6}"
    )
    assert [p[2] for p in proj6_re] == [p[2] for p in proj6], (
        f"proj6_re={proj6_re} != proj6={proj6}"
    )
    assert [_norm_live_text(p[1]) for p in proj6_re] == [_norm_live_text(p[1]) for p in proj6], (
        f"proj6_re={proj6_re} != proj6={proj6}"
    )

    directory = os.path.dirname(os.path.abspath(__file__))
    while directory and not os.path.exists(os.path.join(directory, "pyproject.toml")):
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    logo = os.path.join(directory, "extension", "assets", "logo_32.png")
    if os.path.isfile(logo):
        doc.getText().setString("")
        insert_image(ctx, doc, logo, 32, 32, add_to_gallery=False, add_frame=False)
        count_before = len(doc.getGraphicObjects().getElementNames())
        got_img = GetDocumentContent().execute(tool_ctx, scope="full", include_images=False)
        assert got_img.get("status") == "ok", got_img
        img_html = got_img.get("content") or ""
        # logo_32.png is a user path, so it is linked (_should_link_image_path); the export keeps the file URL and only strips data: payloads.
        assert (
            "<img" in img_html.lower()
            and 'id="Image' in img_html
            and "logo_32.png" in img_html
            and "data:image" not in img_html
        ), img_html[:500]
        res_img = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=img_html)
        assert res_img.get("status") == "ok", res_img
        count_after = len(doc.getGraphicObjects().getElementNames())
        assert count_after == count_before


def _assert_hanging_paragraph(para: Any) -> None:
    """The third paragraph of the round-trip test: space above, hanging indent, 1.5 lines.

    Writer stores these lengths in twips, so 0.5cm (500 in 1/100 mm) can read back as 499.
    """
    assert para.getString() == "Hanging"
    assert abs(para.getPropertyValue("ParaTopMargin") - 500) <= 1
    assert abs(para.getPropertyValue("ParaFirstLineIndent") + 500) <= 1
    spacing = para.getPropertyValue("ParaLineSpacing")
    assert (spacing.Mode, spacing.Height) == (0, 150), (spacing.Mode, spacing.Height)


@native_test
@with_native_doc("writer")
def test_apply_data_lo_para_direct_formatting_and_round_trip_uno(ctx: Any, doc: Any) -> None:
    """Paragraph direct formatting (alignment, margins, background) survives named style application."""
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.get_document_content import GetDocumentContent

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    html = (
        '<h1 data-lo-style="Heading1" data-lo-para="text-align:center; margin-left:2cm; background-color:#ffe4c4">Heading</h1>'
        '<blockquote><p data-lo-para="margin-left:1cm">Quoted text</p></blockquote>'
        '<p data-lo-para="margin-top:0.5cm; text-indent:-0.5cm; line-height:150%">Hanging</p>'
    )
    res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=html)
    assert res.get("status") == "ok", res
    assert "warnings" not in res, res

    enum = doc.getText().createEnumeration()
    p1 = enum.nextElement()
    p2 = enum.nextElement()
    p3 = enum.nextElement()

    solid_fill = uno.Enum("com.sun.star.drawing.FillStyle", "SOLID")
    assert p1.getPropertyValue("ParaAdjust") == 3
    assert p1.getPropertyValue("ParaLeftMargin") == 2000
    assert p1.getPropertyValue("FillStyle") == solid_fill
    assert p1.getPropertyValue("FillColor") == 0xFFE4C4
    got_p1_color = int(p1.getPropertyValue("ParaBackColor"))
    assert got_p1_color == 0xFFE4C4, (
        f"expected 0xFFE4C4, got {hex(got_p1_color)} (res={res})"
    )
    assert p2.getPropertyValue("ParaLeftMargin") == 1000
    _assert_hanging_paragraph(p3)

    got = GetDocumentContent().execute(tool_ctx, scope="full")
    assert got.get("status") == "ok", got
    content = got.get("content") or ""
    assert "data-lo-para" in content

    res2 = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=content)
    assert res2.get("status") == "ok", res2
    # Copying the document back may only warn about the read-only names. The read can report a
    # paragraph-level font where the import set one directly (CI's fonts give the <h1> a
    # font-family, local runs do not); every writable name must apply without a warning.
    from plugin.writer.html_import_styles import _READ_ONLY_PARA_NAMES

    # Match the property name explicitly so a changed warning format fails with a clear
    # assertion instead of IndexError from split("'")[1].
    warn_prop = re.compile(r"data-lo-para property '([^']+)' was not applied")
    warned = set()
    for w in res2.get("warnings", []):
        msg = w["message"] if isinstance(w, dict) else w
        m = warn_prop.fullmatch(msg)
        assert m is not None, f"unexpected warning format: {w!r}"
        warned.add(m.group(1))
    assert warned <= _READ_ONLY_PARA_NAMES, (res2, content)

    enum2 = doc.getText().createEnumeration()
    r1 = enum2.nextElement()
    r2 = enum2.nextElement()
    r3 = enum2.nextElement()

    assert r1.getPropertyValue("ParaAdjust") == 3
    assert r1.getPropertyValue("ParaLeftMargin") == 2000
    assert r1.getPropertyValue("FillStyle") == solid_fill
    assert r1.getPropertyValue("FillColor") == 0xFFE4C4
    got_r1_color = int(r1.getPropertyValue("ParaBackColor"))
    assert got_r1_color == 0xFFE4C4, (
        f"expected 0xFFE4C4, got {hex(got_r1_color)} (res2={res2})"
    )
    assert r2.getPropertyValue("ParaLeftMargin") == 1000
    _assert_hanging_paragraph(r3)


@native_test
@with_native_doc("writer")
def test_apply_data_lo_para_insert_at_end_with_background_color_uno(ctx: Any, doc: Any) -> None:
    """data-lo-para background-color is applied to the newly inserted paragraph at 'end' of non-empty doc."""
    import uno
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.apply_document_content import ApplyDocumentContent

    doc.getText().setString("Initial paragraph")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

    html = '<p data-lo-para="background-color:#ffe4c4">Appended paragraph</p>'
    res2 = ApplyDocumentContent().execute(tool_ctx, target="end", content=html)
    assert res2.get("status") == "ok", res2
    assert "warnings" not in res2, res2

    paras = []
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        el = enum.nextElement()
        if hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.Paragraph"):
            paras.append(el)

    p_initial = next((p for p in paras if "Initial paragraph" in p.getString()), None)
    assert p_initial is not None, [p.getString() for p in paras]
    assert int(p_initial.getPropertyValue("ParaBackColor")) != 0xFFE4C4

    p_appended = next((p for p in paras if "Appended paragraph" in p.getString()), None)
    assert p_appended is not None, [p.getString() for p in paras]
    solid_fill = uno.Enum("com.sun.star.drawing.FillStyle", "SOLID")
    assert p_appended.getPropertyValue("FillStyle") == solid_fill
    assert p_appended.getPropertyValue("FillColor") == 0xFFE4C4
    assert int(p_appended.getPropertyValue("ParaBackColor")) == 0xFFE4C4


@native_test
@with_native_doc("writer")
def test_apply_data_lo_para_insert_at_beginning_with_margin_uno(ctx: Any, doc: Any) -> None:
    """data-lo-para on a 'beginning' insert formats only the new first paragraph.

    The beginning target prepares an empty absorb paragraph before the import, so this is
    the path where an off-by-one in the cursor walk would put the margin on the paragraph
    that was already in the document.
    """
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.apply_document_content import ApplyDocumentContent

    doc.getText().setString("Existing paragraph")
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

    html = '<p data-lo-para="margin-left:1cm">Prepended paragraph</p>'
    res = ApplyDocumentContent().execute(tool_ctx, target="beginning", content=html)
    assert res.get("status") == "ok", res
    assert "warnings" not in res, res

    paras = []
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        el = enum.nextElement()
        if el.supportsService("com.sun.star.text.Paragraph"):
            paras.append(el)

    p_new = next((p for p in paras if "Prepended paragraph" in p.getString()), None)
    assert p_new is not None, [p.getString() for p in paras]
    assert p_new.getPropertyValue("ParaLeftMargin") == 1000

    p_old = next((p for p in paras if "Existing paragraph" in p.getString()), None)
    assert p_old is not None, [p.getString() for p in paras]
    assert p_old.getPropertyValue("ParaLeftMargin") == 0


def _body_paragraphs(doc: Any) -> list[Any]:
    """Return all paragraph objects in the document body."""
    paras = []
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        el = enum.nextElement()
        if hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.Paragraph"):
            paras.append(el)
    return paras


def _numbering_style_name(value: Any) -> str:
    """No list style is either None or "" depending on the property source."""
    if value is None:
        return ""
    return str(value).strip()


def _seed_direct_margins(doc: Any) -> tuple[Any, int, int, tuple[int, int], Any, str]:
    """Seed 'old' with direct margins, line spacing, borders, and numbering."""
    doc.getText().setString("old")
    cursor = doc.getText().createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEndOfParagraph(True)

    para_styles = doc.getStyleFamilies().getByName("ParagraphStyles")
    style_name = cursor.getPropertyValue("ParaStyleName")
    style = para_styles.getByName(style_name)
    style_top = style.getPropertyValue("ParaTopMargin")
    style_left = style.getPropertyValue("ParaLeftMargin")

    cursor.setPropertyValue("ParaTopMargin", 777)
    cursor.setPropertyValue("ParaLeftMargin", 888)

    line_spacing = uno.createUnoStruct("com.sun.star.style.LineSpacing")
    line_spacing.Mode = 0
    line_spacing.Height = 150
    cursor.setPropertyValue("ParaLineSpacing", line_spacing)

    border = uno.createUnoStruct("com.sun.star.table.BorderLine2")
    border.OuterLineWidth = 40
    border.Color = 0xFF0000
    for prop in ("LeftBorder", "RightBorder", "TopBorder", "BottomBorder"):
        cursor.setPropertyValue(prop, border)

    cursor.setPropertyValue("NumberingStyleName", "Numbering 123")

    readback_top = int(cursor.getPropertyValue("ParaTopMargin"))
    readback_left = int(cursor.getPropertyValue("ParaLeftMargin"))
    readback_ls = cursor.getPropertyValue("ParaLineSpacing")
    readback_spacing = (int(readback_ls.Mode), int(readback_ls.Height))
    readback_border = cursor.getPropertyValue("LeftBorder")
    readback_num = str(cursor.getPropertyValue("NumberingStyleName"))

    style_ls = style.getPropertyValue("ParaLineSpacing")
    style_spacing = (int(style_ls.Mode), int(style_ls.Height))
    style_border = style.getPropertyValue("LeftBorder")

    assert readback_top != style_top
    assert readback_left != style_left
    assert readback_spacing != style_spacing
    assert (int(readback_border.OuterLineWidth), int(readback_border.Color)) != (
        int(style_border.OuterLineWidth),
        int(style_border.Color),
    )
    assert _numbering_style_name(readback_num) != _numbering_style_name(style.getPropertyValue("NumberingStyleName"))

    return para_styles, readback_top, readback_left, readback_spacing, readback_border, readback_num


@native_test
@with_native_doc("writer")
def test_full_document_drops_old_first_paragraph_direct_margins_uno(ctx: Any, doc: Any) -> None:
    """full_document must clear absorb paragraph direct formatting before HTML import."""
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.apply_document_content import ApplyDocumentContent

    para_styles, *_ = _seed_direct_margins(doc)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content="<p>a</p><p>b</p>")
    assert res.get("status") == "ok", res

    paras = _body_paragraphs(doc)
    non_empty = [p for p in paras if p.getString() != ""]
    empty_paras = [p for p in paras if p.getString() == ""]

    assert [p.getString() for p in non_empty] == ["a", "b"]
    assert len(empty_paras) >= 1, "Expected trailing empty paragraph"

    for p in paras:
        p_style = para_styles.getByName(p.getPropertyValue("ParaStyleName"))
        assert p.getPropertyValue("ParaTopMargin") == p_style.getPropertyValue("ParaTopMargin")
        assert p.getPropertyValue("ParaLeftMargin") == p_style.getPropertyValue("ParaLeftMargin")

    border_names = ("LeftBorder", "RightBorder", "TopBorder", "BottomBorder")
    for p in non_empty:
        p_style = para_styles.getByName(p.getPropertyValue("ParaStyleName"))
        ls = p.getPropertyValue("ParaLineSpacing")
        style_ls = p_style.getPropertyValue("ParaLineSpacing")
        assert (int(ls.Mode), int(ls.Height)) == (int(style_ls.Mode), int(style_ls.Height))
        assert int(ls.Height) != 150

        for b_name in border_names:
            b = p.getPropertyValue(b_name)
            sb = p_style.getPropertyValue(b_name)
            assert int(b.OuterLineWidth) == int(sb.OuterLineWidth)
            assert int(b.Color) == int(sb.Color)

        assert _numbering_style_name(p.getPropertyValue("NumberingStyleName")) != "Numbering 123"
        assert _numbering_style_name(p.getPropertyValue("NumberingStyleName")) == _numbering_style_name(
            p_style.getPropertyValue("NumberingStyleName")
        )


@native_test
@with_native_doc("writer")
def test_full_document_review_margins_follow_new_style_and_reject_keeps_old_uno(ctx: Any, doc: Any) -> None:
    """In review mode, imported paragraphs drop absorb formatting and reject restores old formatting."""
    from plugin.writer.html_import import replace_full_document

    (
        para_styles,
        readback_top,
        readback_left,
        readback_spacing,
        readback_border,
        readback_num,
    ) = _seed_direct_margins(doc)

    doc.setPropertyValue("RecordChanges", True)
    assert replace_full_document(doc, ctx, "<p>a</p><p>b</p>").unapplied_para == []

    border_names = ("LeftBorder", "RightBorder", "TopBorder", "BottomBorder")
    paras = _body_paragraphs(doc)
    p_old = None
    for p in paras:
        text_val = p.getString()
        p_style = para_styles.getByName(p.getPropertyValue("ParaStyleName"))
        if "old" in text_val:
            p_old = p
            assert p.getPropertyValue("ParaTopMargin") == readback_top
            assert p.getPropertyValue("ParaLeftMargin") == readback_left
            ls = p.getPropertyValue("ParaLineSpacing")
            assert (int(ls.Mode), int(ls.Height)) == readback_spacing
            for b_name in border_names:
                b = p.getPropertyValue(b_name)
                assert int(b.OuterLineWidth) == int(readback_border.OuterLineWidth)
                assert int(b.Color) == int(readback_border.Color)
            assert str(p.getPropertyValue("NumberingStyleName")) == readback_num
        elif text_val in ("a", "b", ""):
            assert p.getPropertyValue("ParaTopMargin") == p_style.getPropertyValue("ParaTopMargin")
            assert p.getPropertyValue("ParaLeftMargin") == p_style.getPropertyValue("ParaLeftMargin")
            assert p.getPropertyValue("ParaTopMargin") != readback_top
            assert p.getPropertyValue("ParaLeftMargin") != readback_left
            if text_val in ("a", "b"):
                ls = p.getPropertyValue("ParaLineSpacing")
                style_ls = p_style.getPropertyValue("ParaLineSpacing")
                assert (int(ls.Mode), int(ls.Height)) == (int(style_ls.Mode), int(style_ls.Height))
                assert int(ls.Height) != 150
                for b_name in border_names:
                    b = p.getPropertyValue(b_name)
                    sb = p_style.getPropertyValue(b_name)
                    assert int(b.OuterLineWidth) == int(sb.OuterLineWidth)
                    assert int(b.Color) == int(sb.Color)
                    assert (int(b.OuterLineWidth), int(b.Color)) != (
                        int(readback_border.OuterLineWidth),
                        int(readback_border.Color),
                    )
                assert _numbering_style_name(p.getPropertyValue("NumberingStyleName")) != "Numbering 123"
                assert _numbering_style_name(p.getPropertyValue("NumberingStyleName")) == _numbering_style_name(
                    p_style.getPropertyValue("NumberingStyleName")
                )

    assert p_old is not None, "Expected paragraph containing old text"

    # Verify no format redlines were recorded on the absorb paragraph
    rl_enum = doc.getRedlines().createEnumeration()
    rl_types = []
    while rl_enum.hasMoreElements():
        rl = rl_enum.nextElement()
        rl_types.append(str(rl.getPropertyValue("RedlineType")))
    assert "Format" not in rl_types
    assert "ParagraphFormat" not in rl_types

    helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    frame = doc.getCurrentController().getFrame()
    helper.executeDispatch(frame, ".uno:RejectAllTrackedChanges", "", 0, ())

    assert "old" in doc.getText().getString()

    paras_after = _body_paragraphs(doc)
    p_restored = next((p for p in paras_after if "old" in p.getString()), None)
    assert p_restored is not None
    assert p_restored.getPropertyValue("ParaTopMargin") == readback_top
    assert p_restored.getPropertyValue("ParaLeftMargin") == readback_left
    ls_restored = p_restored.getPropertyValue("ParaLineSpacing")
    assert (int(ls_restored.Mode), int(ls_restored.Height)) == readback_spacing
    for b_name in border_names:
        b = p_restored.getPropertyValue(b_name)
        assert int(b.OuterLineWidth) == int(readback_border.OuterLineWidth)
        assert int(b.Color) == int(readback_border.Color)
    assert str(p_restored.getPropertyValue("NumberingStyleName")) == readback_num


def _seed_two_paras_and_place_caret(doc: Any, offset: int) -> None:
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    text.insertString(cur, "Alpha beta gamma.", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Second para.", False)
    vc = doc.getCurrentController().getViewCursor()
    vc.gotoStart(False)
    if offset:
        vc.goRight(offset, False)


def _para_texts_via_enum(text_obj: Any) -> list[str]:
    from plugin.framework.uno_context import enum_has_more

    enum = text_obj.createEnumeration()
    out: list[str] = []
    while enum_has_more(enum):
        el = enum.nextElement()
        if hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.Paragraph"):
            out.append(el.getString())
    return out


@native_test
@with_native_doc("writer")
def test_selection_caret_block_goes_after_paragraph_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.html_import import insert_content_at_position

    content = "<p>New A</p><p>New B</p>"
    _seed_two_paras_and_place_caret(doc, 6)
    report_mid = insert_content_at_position(doc, ctx, content, "selection")
    assert report_mid.snapped_to_paragraph is True
    assert _para_texts_via_enum(doc.getText()) == ["Alpha beta gamma.", "New A", "New B", "Second para."]

    _seed_two_paras_and_place_caret(doc, 17)
    report_end = insert_content_at_position(doc, ctx, content, "selection")
    assert report_end.snapped_to_paragraph is False
    assert _para_texts_via_enum(doc.getText()) == ["Alpha beta gamma.", "New A", "New B", "Second para."]

    _seed_two_paras_and_place_caret(doc, 0)
    report_start = insert_content_at_position(doc, ctx, content, "selection")
    assert report_start.snapped_to_paragraph is False
    assert _para_texts_via_enum(doc.getText()) == ["New A", "New B", "Alpha beta gamma.", "Second para."]


@native_test
@with_native_doc("writer")
def test_selection_caret_block_at_document_end_leaves_no_empty_paragraph_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.html_import import insert_content_at_position

    _seed_two_paras_and_place_caret(doc, 0)
    vc = doc.getCurrentController().getViewCursor()
    vc.gotoEnd(False)
    insert_content_at_position(doc, ctx, "<p>New A</p><p>New B</p>", "selection")
    assert _para_texts_via_enum(doc.getText()) == ["Alpha beta gamma.", "Second para.", "New A", "New B"]


@native_test
@with_native_doc("writer")
def test_selection_caret_inline_stays_in_paragraph_uno(ctx: Any, doc: Any) -> None:
    from plugin.framework.uno_context import enum_has_more
    from plugin.writer.html_import import insert_content_at_position

    _seed_two_paras_and_place_caret(doc, 6)
    first_para = doc.getText().createEnumeration().nextElement()
    first_para.setPropertyValue("ParaStyleName", "Heading 1")

    insert_content_at_position(doc, ctx, "new ", "selection")
    assert _para_texts_via_enum(doc.getText()) == ["Alpha new beta gamma.", "Second para."]
    first_para = doc.getText().createEnumeration().nextElement()
    assert first_para.getPropertyValue("ParaStyleName") == "Heading 1"

    _seed_two_paras_and_place_caret(doc, 6)
    insert_content_at_position(doc, ctx, "<b>bold</b> ", "selection")
    assert _para_texts_via_enum(doc.getText()) == ["Alpha bold beta gamma.", "Second para."]
    first_para = doc.getText().createEnumeration().nextElement()
    p_enum = first_para.createEnumeration()
    bold_weights = []
    while enum_has_more(p_enum):
        portion = p_enum.nextElement()
        if portion.getString() == "bold":
            bold_weights.append(portion.getPropertyValue("CharWeight"))
    assert bold_weights == [150]


@native_test
@with_native_doc("writer")
def test_selection_caret_block_in_table_cell_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.html_import import insert_content_at_position

    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(1, 1)
    text.insertTextContent(cur, table, False)
    cell = table.getCellByName("A1")
    cell.setString("Cell words here")

    vc = doc.getCurrentController().getViewCursor()
    cell_cur = cell.createTextCursor()
    cell_cur.gotoStart(False)
    cell_cur.goRight(5, False)
    vc.gotoRange(cell_cur, False)

    insert_content_at_position(doc, ctx, "<p>New A</p><p>New B</p>", "selection")
    assert _para_texts_via_enum(cell) == ["Cell words here", "New A", "New B"]
    assert doc.getTextTables().getCount() == 1


@native_test
@with_native_doc("writer")
def test_selection_caret_block_tracked_changes_uno(ctx: Any, doc: Any) -> None:
    from plugin.framework.uno_context import enum_has_more
    from plugin.writer.html_import import insert_content_at_position

    _seed_two_paras_and_place_caret(doc, 6)
    doc.setPropertyValue("RecordChanges", True)
    try:
        insert_content_at_position(doc, ctx, "<p>New A</p><p>New B</p>", "selection")
        assert _para_texts_via_enum(doc.getText()) == [
            "Alpha beta gamma.",
            "New A",
            "New B",
            "Second para.",
        ]
        rl_enum = doc.getRedlines().createEnumeration()
        rl_types = []
        while enum_has_more(rl_enum):
            rl = rl_enum.nextElement()
            rl_types.append(str(rl.getPropertyValue("RedlineType")))
        assert set(rl_types) == {"Insert"}, rl_types
    finally:
        doc.setPropertyValue("RecordChanges", False)


def _body_structure_via_enum(text_obj: Any) -> list[str]:
    from plugin.framework.uno_context import enum_has_more

    enum = text_obj.createEnumeration()
    out: list[str] = []
    while enum_has_more(enum):
        el = enum.nextElement()
        if hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.TextTable"):
            out.append("TextTable")
        elif hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.Paragraph"):
            out.append("Paragraph:" + (el.getString() or ""))
    return out


@native_test
@with_native_doc("writer")
def test_selection_caret_table_leaves_no_empty_paragraphs_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.html_import import insert_content_at_position

    table_html = "<table><tr><td>A</td><td>B</td></tr></table>"
    expected = ["Paragraph:Alpha beta gamma.", "TextTable", "Paragraph:Second para."]
    helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)

    # Without Track Changes: mid-paragraph (6) and mid-word (3)
    for offset in (6, 3):
        _seed_two_paras_and_place_caret(doc, offset)
        report = insert_content_at_position(doc, ctx, table_html, "selection")
        assert report.snapped_to_paragraph is True
        assert doc.getTextTables().getCount() == 1
        actual = _body_structure_via_enum(doc.getText())
        assert actual == expected, f"offset={offset}: actual={actual!r} expected={expected!r}"

    # With Track Changes + accept-all: mid-paragraph (6) and mid-word (3)
    for offset in (6, 3):
        _seed_two_paras_and_place_caret(doc, offset)
        doc.setPropertyValue("RecordChanges", True)
        try:
            report = insert_content_at_position(doc, ctx, table_html, "selection")
            assert report.snapped_to_paragraph is True
            helper.executeDispatch(
                doc.getCurrentController().getFrame(),
                ".uno:AcceptAllTrackedChanges",
                "",
                0,
                (),
            )
        finally:
            doc.setPropertyValue("RecordChanges", False)
        assert doc.getTextTables().getCount() == 1
        actual = _body_structure_via_enum(doc.getText())
        assert actual == expected, f"offset={offset}: actual={actual!r} expected={expected!r}"



def _seed_three_paras(doc: Any, *, third_style: str | None = None) -> None:
    text = doc.getText()
    doc.setPropertyValue("RecordChanges", False)
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep before", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Replace me", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", third_style or "Standard")
    text.insertString(cur, "Keep after", False)


def _body_style_and_text(doc: Any) -> list[tuple[str, str]]:
    from plugin.framework.uno_context import enum_has_more

    enum = doc.getText().createEnumeration()
    out: list[tuple[str, str]] = []
    while enum_has_more(enum):
        el = enum.nextElement()
        if hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.TextTable"):
            out.append(("TextTable", ""))
        elif hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.Paragraph"):
            out.append((str(el.getPropertyValue("ParaStyleName")), el.getString() or ""))
    return out


def _find_first(doc: Any, needle: str) -> Any:
    desc = doc.createSearchDescriptor()
    desc.SearchString = needle
    found = doc.findFirst(desc)
    assert found is not None, f"could not find {needle!r}"
    return found


@native_test
@with_native_doc("writer")
def test_whole_paragraph_block_replace_leaves_no_empty_paragraph_uno(ctx: Any, doc: Any) -> None:
    from plugin.doc.compound_undo import WriterCompoundUndo
    from plugin.writer.html_import import replace_single_range_with_content

    # Multi-paragraph block replace + undo
    _seed_three_paras(doc)
    with WriterCompoundUndo(doc, "test-replace"):
        replace_single_range_with_content(doc, _find_first(doc, "Replace me"), "<p>A</p><p>B</p>", ctx)
    assert [t for s, t in _body_style_and_text(doc) if s != "TextTable"] == [
        "Keep before",
        "A",
        "B",
        "Keep after",
    ]
    doc.getUndoManager().undo()
    assert [t for s, t in _body_style_and_text(doc) if s != "TextTable"] == [
        "Keep before",
        "Replace me",
        "Keep after",
    ]

    # Bullet list replace
    _seed_three_paras(doc)
    replace_single_range_with_content(doc, _find_first(doc, "Replace me"), "<ul><li>Item 1</li></ul>", ctx)
    assert [t for s, t in _body_style_and_text(doc) if s != "TextTable"] == [
        "Keep before",
        "Item 1",
        "Keep after",
    ]

    # Content ending with a table preserves following paragraph's Heading 1 style
    _seed_three_paras(doc, third_style="Heading 1")
    replace_single_range_with_content(
        doc,
        _find_first(doc, "Replace me"),
        "<p>A</p><table><tr><td>C</td></tr></table>",
        ctx,
    )
    structure = _body_style_and_text(doc)
    assert structure[0][1] == "Keep before"
    assert structure[1][1] == "A"
    assert structure[2] == ("TextTable", "")
    assert structure[3] == ("Heading 1", "Keep after")
    assert len(structure) == 4, structure

    # Replaced paragraph last in the text
    _seed_three_paras(doc)
    replace_single_range_with_content(doc, _find_first(doc, "Keep after"), "<p>End para</p>", ctx)
    assert [t for s, t in _body_style_and_text(doc) if s != "TextTable"] == [
        "Keep before",
        "Replace me",
        "End para",
    ]

    # Empty <p></p> keeps exactly one empty paragraph
    _seed_three_paras(doc)
    replace_single_range_with_content(doc, _find_first(doc, "Replace me"), "<p></p>", ctx)
    assert [t for s, t in _body_style_and_text(doc) if s != "TextTable"] == [
        "Keep before",
        "",
        "Keep after",
    ]


@native_test
@with_native_doc("writer")
def test_selection_whole_paragraph_replace_leaves_no_empty_paragraph_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.html_import import insert_content_at_position

    _seed_three_paras(doc)
    doc.getCurrentController().select(_find_first(doc, "Replace me"))
    insert_content_at_position(doc, ctx, "<p>plain words</p>", "selection")
    assert [t for s, t in _body_style_and_text(doc) if s != "TextTable"] == [
        "Keep before",
        "plain words",
        "Keep after",
    ]


def _dispatch(ctx: Any, doc: Any, cmd: str) -> None:
    helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    helper.executeDispatch(doc.getCurrentController().getFrame(), cmd, "", 0, ())


@native_test
@with_native_doc("writer")
def test_selection_inline_replace_stays_in_paragraph_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.html_import import insert_content_at_position

    def _setup_case(case: str) -> tuple[str, list[tuple[str, str]], str]:
        _seed_three_paras(doc)
        if case == "word":
            doc.getCurrentController().select(_find_first(doc, "Replace"))
            return "Swap", [("Standard", "Keep before"), ("Standard", "Swap me"), ("Standard", "Keep after")], "Standard"
        if case == "whole":
            mid = _find_first(doc, "Replace me")
            mid.setPropertyValue("ParaStyleName", "Heading 1")
            doc.getCurrentController().select(mid)
            return (
                "plain words",
                [("Standard", "Keep before"), ("Heading 1", "plain words"), ("Standard", "Keep after")],
                "Heading 1",
            )
        # shiftdown: selection ends at the next paragraph's start
        found = _find_first(doc, "Replace me")
        sel = doc.getText().createTextCursorByRange(found.getStart())
        sel.gotoNextParagraph(True)
        doc.getCurrentController().select(sel)
        return (
            "plain words",
            [("Standard", "Keep before"), ("Standard", "plain words"), ("Standard", "Keep after")],
            "Standard",
        )

    for case in ("word", "whole", "shiftdown"):
        content, expected, orig_style = _setup_case(case)
        insert_content_at_position(doc, ctx, content, "selection")
        actual = _body_style_and_text(doc)
        assert len(actual) == 3, f"case={case} rec=False: {actual!r}"
        assert actual == expected, f"case={case} rec=False: {actual!r}"

        for action_cmd, post_expected in (
            (".uno:AcceptAllTrackedChanges", expected),
            (
                ".uno:RejectAllTrackedChanges",
                [("Standard", "Keep before"), (orig_style, "Replace me"), ("Standard", "Keep after")],
            ),
        ):
            content, unused_expected, unused_style = _setup_case(case)
            doc.setPropertyValue("RecordChanges", True)
            try:
                insert_content_at_position(doc, ctx, content, "selection")
                assert len(_body_style_and_text(doc)) == 3, f"case={case} rec=True pre-{action_cmd}"
                _dispatch(ctx, doc, action_cmd)
            finally:
                doc.setPropertyValue("RecordChanges", False)
            actual = _body_style_and_text(doc)
            assert actual == post_expected, f"case={case} rec=True {action_cmd}: {actual!r}"


@native_test
@with_native_doc("writer")
def test_selection_inline_replace_in_table_cell_stays_in_paragraph_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.html_import import insert_content_at_position

    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(1, 1)
    text.insertTextContent(cur, table, False)
    cell = table.getCellByName("A1")
    cell.setString("Cell words here")

    cell_cur = cell.createTextCursor()
    cell_cur.gotoStart(False)
    cell_cur.goRight(5, False)
    cell_cur.goRight(5, True)
    doc.getCurrentController().select(cell_cur)

    insert_content_at_position(doc, ctx, "books", "selection")
    assert _para_texts_via_enum(cell) == ["Cell books here"]
    assert doc.getTextTables().getCount() == 1


@native_test
@with_native_doc("writer")
def test_selection_ending_at_next_paragraph_start_applies_style_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.html_import import insert_content_at_position

    _seed_three_paras(doc, third_style="Heading 1")
    found = _find_first(doc, "Replace me")
    sel = doc.getText().createTextCursorByRange(found.getStart())
    sel.gotoNextParagraph(True)
    doc.getCurrentController().select(sel)

    report = insert_content_at_position(
        doc, ctx, '<p data-lo-style="Heading2">New heading</p>', "selection"
    )
    assert report.style_skips == []
    assert _body_style_and_text(doc) == [
        ("Standard", "Keep before"),
        ("Heading 2", "New heading"),
        ("Heading 1", "Keep after"),
    ]


@native_test
@with_native_doc("writer")
def test_without_trailing_break_leaves_range_ending_after_table_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.html_import import _without_trailing_break

    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    text.insertString(cur, "Keep before", False)
    text.insertControlCharacter(cur, 0, False)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    text.insertTextContent(cur, tbl, False)
    cur.gotoEnd(False)
    text.insertString(cur, "Keep after", False)
    assert _body_structure_via_enum(text) == ["Paragraph:Keep before", "TextTable", "Paragraph:Keep after"]

    keep_after = _find_first(doc, "Keep after")
    rng = text.createTextCursorByRange(text.getStart())
    rng.gotoRange(keep_after.getStart(), True)
    assert _without_trailing_break(rng) is rng

    # Plain "Replace me\n" range trims the trailing break
    _seed_three_paras(doc)
    found = _find_first(doc, "Replace me")
    plain_rng = text.createTextCursorByRange(found.getStart())
    plain_rng.gotoNextParagraph(True)
    trimmed = _without_trailing_break(plain_rng)
    assert trimmed is not plain_rng
    assert trimmed.getString() == "Replace me"


@native_test
@with_native_doc("writer")
def test_full_document_review_mode_keeps_trailing_table_uno(ctx: Any, doc: Any) -> None:
    from plugin.writer.html_import import replace_full_document

    table_html = "<table><tr><td>A</td><td>B</td></tr></table>"
    content = f"<h1>H</h1><p>p</p>{table_html}"
    helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    text = doc.getText()

    def _seed(body: str) -> None:
        doc.setPropertyValue("RecordChanges", False)
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.gotoEnd(True)
        cur.setString("")
        if body:
            text.insertString(cur, body, False)

    # Untracked baseline
    _seed("Old para")
    replace_full_document(doc, ctx, content)
    untracked = _body_structure_via_enum(text)
    assert untracked == ["Paragraph:H", "Paragraph:p", "TextTable", "Paragraph:"]

    # Review mode: trailing table survives and Accept All matches untracked
    _seed("Old para")
    doc.setPropertyValue("RecordChanges", True)
    try:
        replace_full_document(doc, ctx, content)
        assert doc.getTextTables().getCount() == 1
        assert _body_structure_via_enum(text) == [
            "Paragraph:H",
            "Paragraph:p",
            "TextTable",
            "Paragraph:Old para",
        ]
        helper.executeDispatch(
            doc.getCurrentController().getFrame(), ".uno:AcceptAllTrackedChanges", "", 0, ()
        )
        assert _body_structure_via_enum(text) == untracked

        # Empty document + table-only under recording, then Reject All
        _seed("")
        doc.setPropertyValue("RecordChanges", True)
        replace_full_document(doc, ctx, table_html)
        assert doc.getTextTables().getCount() == 1
        helper.executeDispatch(
            doc.getCurrentController().getFrame(), ".uno:RejectAllTrackedChanges", "", 0, ()
        )
        assert doc.getTextTables().getCount() == 0
        assert text.getString() == ""
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_table_import_no_stray_empty_paragraphs_uno(ctx: Any, doc: Any) -> None:
    from plugin.framework.config import get_config, save
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from tests.writer.test_apply_document_content_uno import native_writer_tool_context

    tctx = native_writer_tool_context(doc, ctx)
    helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    frame = doc.getCurrentController().getFrame()
    text = doc.getText()
    flag = "doc.agent_edit_review_mode"
    prev = get_config(flag)

    def _seed_three() -> None:
        doc.setPropertyValue("RecordChanges", False)
        while doc.getTextTables().getCount() > 0:
            doc.getTextTables().getByIndex(0).dispose()
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.gotoEnd(True)
        cur.setString("")
        cur.gotoStart(False)
        cur.setPropertyValue("ParaStyleName", "Standard")
        text.insertString(cur, "Intro", False)
        text.insertControlCharacter(cur, 0, False)
        cur.setPropertyValue("ParaStyleName", "Heading 2")
        text.insertString(cur, "Body", False)
        text.insertControlCharacter(cur, 0, False)
        cur.setPropertyValue("ParaStyleName", "Standard")
        text.insertString(cur, "Outro", False)

    tbl = "<table><tr><td>A</td><td>B</td></tr></table>"
    lead_tbl = f"<p>Lead</p>{tbl}"
    trail_tbl = f"{tbl}<p>Trail</p>"

    cases: list[tuple[str, dict[str, Any], str, list[str]]] = [
        ("end+tbl", {"target": "end"}, tbl, ["Paragraph:Intro", "Paragraph:Body", "Paragraph:Outro", "TextTable", "Paragraph:"]),
        ("end+lead", {"target": "end"}, lead_tbl, ["Paragraph:Intro", "Paragraph:Body", "Paragraph:Outro", "Paragraph:Lead", "TextTable", "Paragraph:"]),
        ("end+trail", {"target": "end"}, trail_tbl, ["Paragraph:Intro", "Paragraph:Body", "Paragraph:Outro", "TextTable", "Paragraph:Trail", "Paragraph:"]),
        ("beginning+tbl", {"target": "beginning"}, tbl, ["TextTable", "Paragraph:Intro", "Paragraph:Body", "Paragraph:Outro"]),
        ("beginning+lead", {"target": "beginning"}, lead_tbl, ["Paragraph:Lead", "TextTable", "Paragraph:Intro", "Paragraph:Body", "Paragraph:Outro"]),
        ("full+tbl", {"target": "full_document"}, tbl, ["TextTable", "Paragraph:"]),
        ("full+lead", {"target": "full_document"}, lead_tbl, ["Paragraph:Lead", "TextTable", "Paragraph:"]),
        ("full+trail", {"target": "full_document"}, trail_tbl, ["TextTable", "Paragraph:Trail", "Paragraph:"]),
        ("after+tbl", {"target": "search", "old_content": "Body", "position": "after"}, tbl, ["Paragraph:Intro", "Paragraph:Body", "TextTable", "Paragraph:Outro"]),
        ("after+lead", {"target": "search", "old_content": "Body", "position": "after"}, lead_tbl, ["Paragraph:Intro", "Paragraph:Body", "Paragraph:Lead", "TextTable", "Paragraph:Outro"]),
        ("after+trail", {"target": "search", "old_content": "Body", "position": "after"}, trail_tbl, ["Paragraph:Intro", "Paragraph:Body", "TextTable", "Paragraph:Trail", "Paragraph:Outro"]),
        ("before+tbl", {"target": "search", "old_content": "Body", "position": "before"}, tbl, ["Paragraph:Intro", "TextTable", "Paragraph:Body", "Paragraph:Outro"]),
        ("replace+tbl", {"target": "search", "old_content": "Body"}, tbl, ["Paragraph:Intro", "TextTable", "Paragraph:Outro"]),
        ("replace_first+tbl", {"target": "search", "old_content": "Intro"}, tbl, ["TextTable", "Paragraph:Body", "Paragraph:Outro"]),
        ("anchors+tbl", {"target": "search", "start_anchor": "Bo", "end_anchor": "dy"}, tbl, ["Paragraph:Intro", "TextTable", "Paragraph:Outro"]),
    ]

    try:
        for label, kwargs, html, expected in cases:
            save({flag: "off"})
            _seed_three()
            res = ApplyDocumentContent().execute(tctx, content=[html], **kwargs)
            assert res.get("status") == "ok", (label, "off", res)
            actual_off = _body_structure_via_enum(text)
            assert actual_off == expected, (label, "off", actual_off, expected)

            save({flag: "record"})
            _seed_three()
            res = ApplyDocumentContent().execute(tctx, content=[html], **kwargs)
            assert res.get("status") == "ok", (label, "record-accept", res)
            helper.executeDispatch(frame, ".uno:AcceptAllTrackedChanges", "", 0, ())
            actual_accept = _body_structure_via_enum(text)
            # In record mode, replace_single_range_with_content keeps tracked-deleted old text
            # in the import-start paragraph (so getString() != "" and before-table cleanup
            # skips it) and sets drop_leftover=False; Accept then empties both paragraphs.
            if label in ("replace+tbl", "anchors+tbl"):
                expected_accept = ["Paragraph:Intro", "Paragraph:", "TextTable", "Paragraph:", "Paragraph:Outro"]
            elif label == "replace_first+tbl":
                expected_accept = ["Paragraph:", "TextTable", "Paragraph:", "Paragraph:Body", "Paragraph:Outro"]
            else:
                expected_accept = expected
            assert actual_accept == expected_accept, (label, "record-accept", actual_accept, expected_accept)

            _seed_three()
            res = ApplyDocumentContent().execute(tctx, content=[html], **kwargs)
            assert res.get("status") == "ok", (label, "record-reject", res)
            helper.executeDispatch(frame, ".uno:RejectAllTrackedChanges", "", 0, ())
            actual_reject = _body_structure_via_enum(text)
            assert actual_reject == ["Paragraph:Intro", "Paragraph:Body", "Paragraph:Outro"], (
                label,
                "record-reject",
                actual_reject,
            )
    finally:
        save({flag: prev})
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_table_import_edge_cases_and_non_table_unchanged_uno(ctx: Any, doc: Any) -> None:
    from plugin.framework.config import get_config, save
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from tests.writer.test_apply_document_content_uno import native_writer_tool_context

    tctx = native_writer_tool_context(doc, ctx)
    text = doc.getText()
    flag = "doc.agent_edit_review_mode"
    prev = get_config(flag)

    def _seed(paras: list[str]) -> None:
        doc.setPropertyValue("RecordChanges", False)
        while doc.getTextTables().getCount() > 0:
            doc.getTextTables().getByIndex(0).dispose()
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.gotoEnd(True)
        cur.setString("")
        cur.gotoStart(False)
        cur.setPropertyValue("ParaStyleName", "Standard")
        for idx, p in enumerate(paras):
            if idx:
                text.insertControlCharacter(cur, 0, False)
            if p:
                text.insertString(cur, p, False)

    tbl = "<table><tr><td>A</td></tr></table>"
    nested_tbl = "<table><tr><td><table><tr><td>In</td></tr></table></td></tr></table>"
    two_tbls = "<table><tr><td>1</td></tr></table><table><tr><td>2</td></tr></table>"

    try:
        save({flag: "off"})

        # Non-table content unchanged
        _seed(["Intro", "Outro"])
        res = ApplyDocumentContent().execute(tctx, content=["<p>One</p><p>Two</p>"], target="search", old_content="Intro", position="after")
        assert res.get("status") == "ok", res
        assert _body_structure_via_enum(text) == ["Paragraph:Intro", "Paragraph:One", "Paragraph:Two", "Paragraph:Outro"]

        _seed(["Intro", "Outro"])
        res = ApplyDocumentContent().execute(tctx, content=["<ul><li>i1</li><li>i2</li></ul>"], target="end")
        assert res.get("status") == "ok", res
        assert _body_structure_via_enum(text) == ["Paragraph:Intro", "Paragraph:Outro", "Paragraph:i1", "Paragraph:i2", "Paragraph:"], _body_structure_via_enum(text)

        # Nested table and adjacent tables
        _seed(["Intro", "Outro"])
        res = ApplyDocumentContent().execute(tctx, content=[nested_tbl], target="search", old_content="Intro", position="after")
        assert res.get("status") == "ok", res
        assert _body_structure_via_enum(text) == ["Paragraph:Intro", "TextTable", "Paragraph:Outro"]

        _seed(["Intro", "Outro"])
        res = ApplyDocumentContent().execute(tctx, content=[two_tbls], target="search", old_content="Intro", position="after")
        assert res.get("status") == "ok", res
        assert _body_structure_via_enum(text) == ["Paragraph:Intro", "TextTable", "TextTable", "Paragraph:Outro"]

        # Document already starting with a table: insert at beginning and before/after following para
        _seed([""])
        res = ApplyDocumentContent().execute(tctx, content=[f"{tbl}<p>After</p>"], target="full_document")
        assert res.get("status") == "ok", res
        res = ApplyDocumentContent().execute(tctx, content=[tbl], target="beginning")
        assert res.get("status") == "ok", res
        assert _body_structure_via_enum(text) == ["TextTable", "TextTable", "Paragraph:After", "Paragraph:"], _body_structure_via_enum(text)

        # Empty document + table at end: on a 1-paragraph blank doc, SwHTMLParser opens an
        # extra split node before the table; cleanup removes the split node and keeps the user's own.
        _seed([""])
        res = ApplyDocumentContent().execute(tctx, content=[tbl], target="end")
        assert res.get("status") == "ok", res
        assert _body_structure_via_enum(text) == ["Paragraph:", "TextTable", "Paragraph:"], _body_structure_via_enum(text)

        # Replacing with <p></p> keeps the emptied paragraph
        _seed(["Intro", "Body", "Outro"])
        res = ApplyDocumentContent().execute(tctx, content=["<p></p>"], target="search", old_content="Body")
        assert res.get("status") == "ok", res
        assert _body_structure_via_enum(text) == ["Paragraph:Intro", "Paragraph:", "Paragraph:Outro"], _body_structure_via_enum(text)

        # Styled leading table + styled trailing paragraph
        _seed(["Intro", "Outro"])
        res = ApplyDocumentContent().execute(
            tctx,
            content=[f'{tbl}<p data-lo-style="Heading 1">Head</p>'],
            target="search",
            old_content="Intro",
            position="after",
        )
        assert res.get("status") == "ok", res
        assert _body_structure_via_enum(text) == ["Paragraph:Intro", "TextTable", "Paragraph:Head", "Paragraph:Outro"], _body_structure_via_enum(text)

        # Import-start paragraph with a bookmark or anchored graphic is not disposed
        import uno
        from pathlib import Path
        from plugin.writer.html_import import _drop_empty_paragraph_before_table

        _seed(["", "Outro"])
        cur = text.createTextCursor()
        cur.gotoStart(False)
        bm = doc.createInstance("com.sun.star.text.Bookmark")
        bm.setName("kept_bm")
        text.insertTextContent(cur, bm, False)
        res = ApplyDocumentContent().execute(tctx, content=[tbl], target="search", old_content="Outro", position="before")
        assert res.get("status") == "ok", res
        assert _body_structure_via_enum(text) == ["Paragraph:", "TextTable", "Paragraph:Outro"]
        _drop_empty_paragraph_before_table(text, text.getStart())
        assert doc.getBookmarks().getCount() == 1
        assert _body_structure_via_enum(text) == ["Paragraph:", "TextTable", "Paragraph:Outro"], _body_structure_via_enum(text)

        _seed(["", "Outro"])
        cur = text.createTextCursor()
        cur.gotoStart(False)
        graphic = doc.createInstance("com.sun.star.text.TextGraphicObject")
        graphic.setPropertyValue("AnchorType", uno.Enum("com.sun.star.text.TextContentAnchorType", "AS_CHARACTER"))
        graphic.setPropertyValue("GraphicURL", Path("extension/assets/logo_32.png").resolve().as_uri())
        text.insertTextContent(cur, graphic, False)
        res = ApplyDocumentContent().execute(tctx, content=[tbl], target="search", old_content="Outro", position="before")
        assert res.get("status") == "ok", res
        assert _body_structure_via_enum(text) == ["Paragraph:", "TextTable", "Paragraph:Outro"]
        _drop_empty_paragraph_before_table(text, text.getStart())
        assert doc.getGraphicObjects().getCount() == 1
        assert _body_structure_via_enum(text) == ["Paragraph:", "TextTable", "Paragraph:Outro"], _body_structure_via_enum(text)
    finally:
        save({flag: prev})
        doc.setPropertyValue("RecordChanges", False)
