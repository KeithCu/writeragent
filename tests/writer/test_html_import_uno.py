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
