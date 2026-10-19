# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for HTML import ruby write (apply_document_content)."""

import os
import re
from typing import Any

import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import skip_windows_leftover_hidden_load, with_native_doc


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
    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.content import ApplyDocumentContent, GetDocumentContent

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    html = "<p>前<ruby>漢字<rt>かんじ</rt></ruby>後</p>"
    res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=html)
    assert res.get("status") == "ok", res
    assert doc.getText().getString().split("\n")[0] == "前漢字後"
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
    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.content import ApplyDocumentContent, GetDocumentContent

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
    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.content import ApplyDocumentContent

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
    skip_windows_leftover_hidden_load("html_to_search_text oracle swriter")
    from plugin.framework.html_stripper import html_to_search_text
    from plugin.writer.html_import import extract_and_strip_ruby, insert_html_fragment_at_cursor

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
    skip_windows_leftover_hidden_load("styled_insert_at_end_cursor_walk_uno swriter")
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
    from plugin.writer.content import ApplyDocumentContent

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
    from plugin.writer.content import ApplyDocumentContent

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
    from plugin.writer.html_export import _iter_ruby_spans_in_paragraph

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


@native_test
@with_native_doc("writer")
def test_html_import_live_roundtrip_uno(ctx: Any, doc: Any) -> None:
    """Live round-trip harness testing HTML import/export projection and idempotence."""
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.content import ApplyDocumentContent, GetDocumentContent
    from plugin.writer.html_export import _iter_ruby_spans_in_paragraph
    from plugin.writer.images.image_tools import insert_image

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

    # Fixture 1: nested blocks sharing one style slot
    doc.getText().setString("")
    nested_html = (
        '<h1 data-lo-style="Heading1">T</h1>'
        '<blockquote><p data-lo-style="Quotations">q</p></blockquote>'
        '<ul><li><p data-lo-style="Standard">a</p></li><li><p data-lo-style="Standard">b</p></li></ul>'
        '<h2 data-lo-style="Heading2">D</h2>'
    )
    res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=nested_html)
    assert res.get("status") == "ok", res
    plain1 = doc.getText().getString()
    assert "[[WA-FIELD:" not in plain1
    assert not re.search(r"wa[0-9a-fA-F]+img\d+", plain1)
    read1 = GetDocumentContent().execute(tool_ctx, scope="full")
    assert read1.get("status") == "ok", read1
    read_html1 = read1.get("content") or ""
    assert "[[WA-FIELD:" not in read_html1
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

    # Fixture 2: unclosed p tag
    doc.getText().setString("")
    f2 = '<p data-lo-style="Heading1">One<p data-lo-style="Caption">Two'
    res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=f2)
    assert res.get("status") == "ok", res
    plain2 = doc.getText().getString()
    assert "[[WA-FIELD:" not in plain2
    assert not re.search(r"wa[0-9a-fA-F]+img\d+", plain2)
    read2 = GetDocumentContent().execute(tool_ctx, scope="full")
    assert read2.get("status") == "ok", read2
    read_html2 = read2.get("content") or ""
    assert "[[WA-FIELD:" not in read_html2
    assert "data-lo-style=" in read_html2
    proj2 = _project_body_paragraphs(doc)
    assert [p[0] for p in proj2] == ["Heading 1", "Caption"], proj2
    assert [_norm_live_text(p[1]) for p in proj2] == ["One", "Two"], proj2

    # Fixture 3: styled div wrapper
    doc.getText().setString("")
    f3 = '<div data-lo-style="Caption">cap</div><p data-lo-style="Heading1">h</p>'
    res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=f3)
    assert res.get("status") == "ok", res
    plain3 = doc.getText().getString()
    assert "[[WA-FIELD:" not in plain3
    assert not re.search(r"wa[0-9a-fA-F]+img\d+", plain3)
    read3 = GetDocumentContent().execute(tool_ctx, scope="full")
    assert read3.get("status") == "ok", read3
    read_html3 = read3.get("content") or ""
    assert "[[WA-FIELD:" not in read_html3
    assert "data-lo-style=" in read_html3
    proj3 = _project_body_paragraphs(doc)
    assert [(p[0], p[1]) for p in proj3] == [("Caption", "cap"), ("Heading 1", "h")], proj3

    # Fixture 4: repeated base ruby with backslash-n
    doc.getText().setString("")
    f4 = "<p>漢字\\n<ruby>漢字<rt>かんじ</rt></ruby></p>"
    res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=f4)
    assert res.get("status") == "ok", res
    plain4 = doc.getText().getString()
    assert "[[WA-FIELD:" not in plain4
    assert not re.search(r"wa[0-9a-fA-F]+img\d+", plain4)
    read4 = GetDocumentContent().execute(tool_ctx, scope="full")
    assert read4.get("status") == "ok", read4
    read_html4 = read4.get("content") or ""
    assert "[[WA-FIELD:" not in read_html4
    para4 = doc.getText().createEnumeration().nextElement()
    spans4 = list(_iter_ruby_spans_in_paragraph(para4))
    assert len(spans4) == 1, spans4
    assert spans4[0].base == "漢字" and spans4[0].reading == "かんじ"
    assert spans4[0].start > 0, f"ruby attached at start: {spans4}"
    # Range get on temp-doc copy preserves ruby
    range_res = GetDocumentContent().execute(tool_ctx, scope="range", start=0, end=len(plain4))
    assert range_res.get("status") == "ok", range_res
    assert "<rt>かんじ</rt>" in (range_res.get("content") or "")

    # Fixture 5: prose angle brackets and bold
    doc.getText().setString("")
    f5 = "<p>List<String> and 2 < 3</p>"
    res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=f5)
    assert res.get("status") == "ok", res
    plain5 = doc.getText().getString()
    assert "[[WA-FIELD:" not in plain5
    assert not re.search(r"wa[0-9a-fA-F]+img\d+", plain5)
    read5 = GetDocumentContent().execute(tool_ctx, scope="full")
    assert read5.get("status") == "ok", read5
    read_html5 = read5.get("content") or ""
    assert "[[WA-FIELD:" not in read_html5
    proj5 = _project_body_paragraphs(doc)
    assert proj5[0][1] == "List<String> and 2 < 3", proj5

    # Fixture 6: field spans
    doc.getText().setString("")
    f6 = '<p>Page <span title="page-number"/> / <span title="date"/></p>'
    res = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=f6)
    assert res.get("status") == "ok", res
    plain6 = doc.getText().getString()
    assert "[[WA-FIELD:" not in plain6
    assert not re.search(r"wa[0-9a-fA-F]+img\d+", plain6)
    read6 = GetDocumentContent().execute(tool_ctx, scope="full")
    assert read6.get("status") == "ok", read6
    read_html6 = read6.get("content") or ""
    assert "[[WA-FIELD:" not in read_html6
    tf_page = _find_field_by_service(doc, "com.sun.star.text.textfield.PageNumber")
    assert tf_page is not None
    tf_date = _find_field_by_service(doc, "com.sun.star.text.textfield.DateTime")
    assert tf_date is not None
    assert tf_date.getPropertyValue("IsDate") is True
    # Idempotence on field fixture
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

    # Image case
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
        # Live export may omit src, use src="", or a wa-image marker when bytes are stripped.
        assert (
            'src=""' in img_html
            or "src=''" in img_html
            or "wa-image" in img_html.lower()
            or "graphic" in img_html.lower()
            or "<img" in img_html.lower()
        ), img_html[:500]
        res_img = ApplyDocumentContent().execute(tool_ctx, target="full_document", content=img_html)
        assert res_img.get("status") == "ok", res_img
        count_after = len(doc.getGraphicObjects().getElementNames())
        assert count_after == count_before



