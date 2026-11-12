# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""UNO integration tests for heading link resolution and dollar math on import."""

from __future__ import annotations

import uno  # noqa: F401
from com.sun.star.beans import PropertyValue

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import native_writer_tool_context, with_native_doc
from plugin.writer.apply_document_content import ApplyDocumentContent


def _read_portion_urls(doc) -> list[tuple[str, str]]:
    """Collect (text, HyperLinkURL) for all visible portions in the body."""
    urls = []
    e = doc.getText().createEnumeration()
    while e.hasMoreElements():
        para = e.nextElement()
        if not para.supportsService("com.sun.star.text.Paragraph"):
            continue
        portions = para.createEnumeration()
        while portions.hasMoreElements():
            portion = portions.nextElement()
            url = str(portion.getPropertyValue("HyperLinkURL") or "")
            text = str(portion.getString() or "")
            if url:
                urls.append((text, url))
    return urls


def _jump_to_mark(ctx, doc, mark_name: str) -> None:
    """Dispatch .uno:JumpToMark with Bookmark parameter to simulate a link click."""
    smgr = ctx.getServiceManager()
    helper = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    frame = doc.getCurrentController().getFrame()
    prop = PropertyValue()
    prop.Name = "Bookmark"
    prop.Value = mark_name
    helper.executeDispatch(frame, ".uno:JumpToMark", "", 0, (prop,))


def _view_cursor_paragraph_text(doc) -> str:
    """Return the trimmed paragraph text at the view cursor."""
    vc = doc.getCurrentController().getViewCursor()
    cur = doc.getText().createTextCursorByRange(vc.getStart())
    cur.gotoStartOfParagraph(False)
    cur.gotoEndOfParagraph(True)
    return str(cur.getString() or "").strip()


@native_test
@with_native_doc("writer")
def test_heading_links_full_document_uno(ctx, doc) -> None:
    """Bare heading links rewrite to |outline; bookmarks and id targets remain unchanged."""
    tool = ApplyDocumentContent()
    tool_ctx = native_writer_tool_context(doc, ctx)

    html = (
        "<h1>Getting Started</h1>"
        '<h2 id="intro">Intro &amp; More</h2>'
        "<p>"
        '<a href="#Getting Started">Link A</a> '
        '<a href="#Nowhere">Link B</a> '
        '<a href="#intro">Link C</a> '
        '<a href="#Intro &amp; More">Link D</a>'
        "</p>"
    )
    res = tool.execute(tool_ctx, target="full_document", content=html)
    assert res.get("status") == "ok", res

    warnings = res.get("warnings") or []
    assert len(warnings) == 1
    assert warnings[0].get("code") == "LINK_TARGET_NOT_FOUND"
    assert warnings[0].get("link_target") == "#Nowhere"

    portion_urls = [url for _text, url in _read_portion_urls(doc)]
    assert portion_urls == [
        "#Getting Started|outline",
        "#Nowhere",
        "#intro",
        "#Intro & More|outline",
    ]

    # Verify navigation works for outline links via .uno:JumpToMark
    _jump_to_mark(ctx, doc, portion_urls[0][1:])
    assert _view_cursor_paragraph_text(doc) == "Getting Started"

    _jump_to_mark(ctx, doc, portion_urls[3][1:])
    assert _view_cursor_paragraph_text(doc) == "Intro & More"


@native_test
@with_native_doc("writer")
def test_heading_links_target_end_against_existing_headings_uno(ctx, doc) -> None:
    """target='end' resolves bare links against pre-existing document headings."""
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    text.insertString(cur, "Overview", False)

    pcur = text.createTextCursorByRange(text.getStart())
    pcur.gotoEnd(True)
    pcur.setPropertyValue("ParaStyleName", "Heading 1")

    tool = ApplyDocumentContent()
    tool_ctx = native_writer_tool_context(doc, ctx)

    res = tool.execute(
        tool_ctx,
        target="end",
        content='<p><a href="#Overview">back</a></p>',
    )
    assert res.get("status") == "ok", res
    assert "warnings" not in res or not res["warnings"]

    portion_urls = [url for _text, url in _read_portion_urls(doc)]
    assert portion_urls == ["#Overview|outline"]

    _jump_to_mark(ctx, doc, "Overview|outline")
    assert _view_cursor_paragraph_text(doc) == "Overview"


@native_test
@with_native_doc("writer")
def test_tagless_dollar_math_vs_currency_search_uno(ctx, doc) -> None:
    """Tag-less content with TeX dollar math inserts Math objects; currency remains literal."""
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("Placeholder paragraph one.")
    cur.gotoStart(False)

    tool = ApplyDocumentContent()
    tool_ctx = native_writer_tool_context(doc, ctx)

    # 1. Complete $...$ math: routes to HTML import and inserts embedded formula object
    res_math = tool.execute(
        tool_ctx,
        target="search",
        old_content="Placeholder paragraph one.",
        content="Body $x^2$ one.",
    )
    assert res_math.get("status") == "ok", res_math
    assert "(formatting preserved)" not in str(res_math.get("message", ""))
    math_objects = res_math.get("math_objects") or []
    assert len(math_objects) >= 1
    assert math_objects[0].get("ok") is True
    assert doc.getEmbeddedObjects().getCount() == 1

    # 2. Currency dollars: not math, routes to format-preserving plain replace
    res_curr = tool.execute(
        tool_ctx,
        target="search",
        old_content="Body ",
        content="costs $5 and $7 ",
    )
    assert res_curr.get("status") == "ok", res_curr
    assert "(formatting preserved)" in str(res_curr.get("message", ""))
    assert doc.getEmbeddedObjects().getCount() == 1
    assert "costs $5 and $7" in text.getString()
