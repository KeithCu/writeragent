# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for HTML import ruby write (apply_document_content)."""

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


