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
    cur = vc.getText().createTextCursorByRange(vc.getStart())
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

    # 3. Calc reference dollars: not math, embedded-object count stays 1
    res_ref = tool.execute(
        tool_ctx,
        target="end",
        content="Select $A:$B in Calc.",
    )
    assert res_ref.get("status") == "ok", res_ref
    assert doc.getEmbeddedObjects().getCount() == 1
    assert "Select $A:$B in Calc." in text.getString()


@native_test
@with_native_doc("writer")
def test_heading_links_resolve_table_frame_and_variants_uno(ctx, doc) -> None:
    """Bare links resolve against table-cell and frame headings, case-insensitively, and by a__ id."""
    text = doc.getText()
    text.setString("")

    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(1, 1)
    text.insertTextContent(text.getEnd(), table, False)
    cell = table.getCellByName("A1")
    cell.setString("Cell Heading")
    ccur = cell.createTextCursor()
    ccur.gotoStart(False)
    ccur.gotoEnd(True)
    ccur.setPropertyValue("ParaStyleName", "Heading 2")

    frame = doc.createInstance("com.sun.star.text.TextFrame")
    frame.setName("HeadingFrame")
    frame.setSize(uno.createUnoStruct("com.sun.star.awt.Size", 5000, 2000))
    text.insertTextContent(text.getEnd(), frame, False)
    ftext = frame.getText()
    ftext.setString("Frame Heading")
    fcur = ftext.createTextCursor()
    fcur.gotoStart(False)
    fcur.gotoEnd(True)
    fcur.setPropertyValue("ParaStyleName", "Heading 3")

    bcur = text.createTextCursor()
    bcur.gotoEnd(False)
    text.insertControlCharacter(bcur, 0, False)
    text.insertString(bcur, "Body Heading", False)
    bcur.gotoStartOfParagraph(True)
    bcur.setPropertyValue("ParaStyleName", "Heading 1")

    tool = ApplyDocumentContent()
    tool_ctx = native_writer_tool_context(doc, ctx)
    res = tool.execute(
        tool_ctx,
        target="end",
        content=(
            '<p><a href="#Cell Heading">cell</a> '
            '<a href="#Frame Heading">frame</a> '
            '<a href="#body heading">case</a> '
            '<a href="#a__Body_Heading">id</a></p>'
        ),
    )
    assert res.get("status") == "ok", res
    assert not res.get("warnings"), res

    portion_urls = [url for _text, url in _read_portion_urls(doc)]
    assert portion_urls == [
        "#Cell Heading|outline",
        "#Frame Heading|outline",
        "#Body Heading|outline",
        "#Body Heading|outline",
    ], portion_urls

    _jump_to_mark(ctx, doc, "Cell Heading|outline")
    assert _view_cursor_paragraph_text(doc) == "Cell Heading"

    _jump_to_mark(ctx, doc, "Frame Heading|outline")
    assert _view_cursor_paragraph_text(doc) == "Frame Heading"


@native_test
@with_native_doc("writer", reuse=False)
def test_heading_links_resolve_text_box_heading_uno(ctx, doc) -> None:
    from com.sun.star.awt import Point, Size

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Body start.", False)

    shape = doc.createInstance("com.sun.star.drawing.RectangleShape")
    shape.setName("HeadingBox")
    shape.setSize(Size(5000, 2000))
    shape.setPosition(Point(1000, 1000))
    text.insertTextContent(cur, shape, False)
    shape.setPropertyValue("TextBox", True)
    box_text = shape.getPropertyValue("TextBoxContent").getText()
    box_text.setString("Box Heading")
    bcur = box_text.createTextCursor()
    bcur.gotoStart(False)
    bcur.gotoEnd(True)
    bcur.setPropertyValue("ParaStyleName", "Heading 2")

    tool_ctx = native_writer_tool_context(doc, ctx)
    res = ApplyDocumentContent().execute(
        tool_ctx,
        target="end",
        content='<p><a href="#Box Heading">box link</a></p>',
    )
    assert res.get("status") == "ok", res
    assert not res.get("warnings"), res

    portion_urls = [url for _text, url in _read_portion_urls(doc)]
    assert portion_urls == ["#Box Heading|outline"], portion_urls

    _jump_to_mark(ctx, doc, "Box Heading|outline")
    assert _view_cursor_paragraph_text(doc) == "Box Heading"


def _dispatch(ctx, doc, command: str) -> None:
    smgr = ctx.getServiceManager()
    helper = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    helper.executeDispatch(doc.getCurrentController().getFrame(), command, "", 0, ())


def _body_paragraphs(doc) -> list[str]:
    out = []
    e = doc.getText().createEnumeration()
    while e.hasMoreElements():
        p = e.nextElement()
        if p.supportsService("com.sun.star.text.Paragraph"):
            out.append(str(p.getString() or ""))
    return out


@native_test
@with_native_doc("writer", reuse=False)
def test_heading_rename_retargets_links_uno(ctx, doc) -> None:
    tool_ctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()

    # Initial document with a styled/named/targeted split link (bold half) and a second heading.
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">Old Alpha</h1>',
            '<p>See <a href="#Old Alpha|outline" name="L1" target="_top">link <b>half</b></a>.</p>',
            '<h2 data-lo-style="Heading 2">Old Beta</h2>',
            '<p>Also <a href="#Old Beta|outline">beta link</a>.</p>',
        ],
    )
    assert res.get("status") == "ok", res

    # Color the link run to verify color/weight/name/target survive.
    e = doc.getText().createEnumeration()
    while e.hasMoreElements():
        p = e.nextElement()
        if not p.supportsService("com.sun.star.text.Paragraph"):
            continue
        pe = p.createEnumeration()
        while pe.hasMoreElements():
            por = pe.nextElement()
            if str(por.getPropertyValue("HyperLinkURL") or "") == "#Old Alpha|outline":
                por.setPropertyValue("CharColor", 0x112233)

    # 1. old_content plain rename of Old Alpha
    res = tool.execute(tool_ctx, target="search", old_content="Old Alpha", content="New Alpha")
    assert res.get("status") == "ok", res
    assert res.get("links_retargeted", 0) >= 1, res
    urls = _read_portion_urls(doc)
    assert all(url == "#New Alpha|outline" for text, url in urls if text in ("link ", "half")), urls
    _jump_to_mark(ctx, doc, "New Alpha|outline")
    assert _view_cursor_paragraph_text(doc) == "New Alpha"

    # Verify bold half kept weight, color, name, target.
    half_props = None
    e = doc.getText().createEnumeration()
    while e.hasMoreElements():
        p = e.nextElement()
        if not p.supportsService("com.sun.star.text.Paragraph"):
            continue
        pe = p.createEnumeration()
        while pe.hasMoreElements():
            por = pe.nextElement()
            if str(por.getString() or "") == "half":
                half_props = (
                    por.getPropertyValue("CharWeight"),
                    por.getPropertyValue("CharColor"),
                    por.getPropertyValue("HyperLinkName"),
                    por.getPropertyValue("HyperLinkTarget"),
                )
    assert half_props is not None
    assert half_props[0] > 100.0
    assert half_props[1] == 0x112233
    assert half_props[2] == "L1"
    assert half_props[3] == "_top"

    # One undo restores Old Alpha and #Old Alpha|outline.
    doc.getUndoManager().undo()
    assert any(url == "#Old Alpha|outline" for _t, url in _read_portion_urls(doc))

    # 2. anchor span rename
    res = tool.execute(
        tool_ctx,
        target="search",
        start_anchor="Old",
        end_anchor="Alpha",
        content="Anchor Alpha",
    )
    assert res.get("status") == "ok", res
    assert any(url == "#Anchor Alpha|outline" for _t, url in _read_portion_urls(doc))

    # 3. old_content HTML rename
    res = tool.execute(
        tool_ctx,
        target="search",
        old_content="Old Beta",
        content='<h2 data-lo-style="Heading 2">Html Beta</h2>',
    )
    assert res.get("status") == "ok", res
    assert any(url == "#Html Beta|outline" for _t, url in _read_portion_urls(doc))

    # 4. all_matches rename
    res = tool.execute(
        tool_ctx,
        target="search",
        old_content="Html Beta",
        content="All Beta",
        all_matches=True,
    )
    assert res.get("status") == "ok", res
    assert any(url == "#All Beta|outline" for _t, url in _read_portion_urls(doc))

    # 5. full_document rename
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">Final Alpha</h1>',
            '<p>See <a href="#Anchor Alpha|outline">link half</a>.</p>',
            '<h2 data-lo-style="Heading 2">All Beta</h2>',
            '<p>Also <a href="#All Beta|outline">beta link</a>.</p>',
        ],
    )
    assert res.get("status") == "ok", res
    assert any(url == "#Final Alpha|outline" for _t, url in _read_portion_urls(doc))


@native_test
@with_native_doc("writer", reuse=False)
def test_heading_rename_reaches_cells_frames_notes_header_uno(ctx, doc) -> None:
    from com.sun.star.awt import Size

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Target Heading", False)
    cur.gotoStartOfParagraph(False)
    cur.gotoEndOfParagraph(True)
    cur.setPropertyValue("ParaStyleName", "Heading 1")
    cur.gotoEnd(False)
    text.insertControlCharacter(cur, 0, False)

    # Table cell link
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    text.insertTextContent(cur, tbl, False)
    cell_text = tbl.getCellByName("A1").getText()
    cc = cell_text.createTextCursor()
    cell_text.insertString(cc, "cell", False)
    cc.gotoStart(False)
    cc.gotoEnd(True)
    cc.setPropertyValue("HyperLinkURL", "#Target Heading|outline")

    # Text frame link
    frame = doc.createInstance("com.sun.star.text.TextFrame")
    frame.setSize(Size(4000, 1500))
    text.insertTextContent(cur, frame, False)
    ft = frame.getText()
    fc = ft.createTextCursor()
    ft.insertString(fc, "frame", False)
    fc.gotoStart(False)
    fc.gotoEnd(True)
    fc.setPropertyValue("HyperLinkURL", "#Target Heading|outline")

    # Footnote link
    fn = doc.createInstance("com.sun.star.text.Footnote")
    text.insertTextContent(cur, fn, False)
    fnt = fn.getText()
    fnc = fnt.createTextCursor()
    fnt.insertString(fnc, "note", False)
    fnc.gotoStart(False)
    fnc.gotoEnd(True)
    fnc.setPropertyValue("HyperLinkURL", "#Target Heading|outline")

    # Header link
    pstyle = doc.getStyleFamilies().getByName("PageStyles").getByName("Standard")
    pstyle.setPropertyValue("HeaderIsOn", True)
    ht = pstyle.getPropertyValue("HeaderText")
    hc = ht.createTextCursor()
    ht.insertString(hc, "hdr", False)
    hc.gotoStart(False)
    hc.gotoEnd(True)
    hc.setPropertyValue("HyperLinkURL", "#Target Heading|outline")

    res = ApplyDocumentContent().execute(
        native_writer_tool_context(doc, ctx),
        target="search",
        old_content="Target Heading",
        content="Renamed Target",
    )
    assert res.get("status") == "ok", res
    assert res.get("links_retargeted") == 4, res

    for container in (cell_text, ft, fnt, ht):
        e = container.createEnumeration()
        p = e.nextElement()
        pe = p.createEnumeration()
        found_new = False
        while pe.hasMoreElements():
            if str(pe.nextElement().getPropertyValue("HyperLinkURL") or "") == "#Renamed Target|outline":
                found_new = True
        assert found_new


@native_test
@with_native_doc("writer", reuse=False)
def test_heading_rename_while_user_records_uno(ctx, doc) -> None:
    tool_ctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">One</h1>',
            '<p><a href="#One|outline">whole para link</a></p>',
            '<p>end link <a href="#Two|outline">to two</a></p>',
            '<h2 data-lo-style="Heading 2">Two</h2>',
        ],
    )
    assert res.get("status") == "ok", res
    initial_paras = len(_body_paragraphs(doc))

    doc.setPropertyValue("RecordChanges", True)
    doc.setPropertyValue("ShowChanges", False)
    res = tool.execute(tool_ctx, target="search", old_content="One", content="One B")
    assert res.get("status") == "ok", res
    assert res.get("links_retargeted", 0) >= 1, res

    res = tool.execute(tool_ctx, target="search", old_content="Two", content="Two B")
    assert res.get("status") == "ok", res
    assert res.get("links_retargeted", 0) >= 1, res

    # Rename a second time while recording is still active.
    res = tool.execute(tool_ctx, target="search", old_content="One B", content="One C")
    assert res.get("status") == "ok", res

    doc.setPropertyValue("ShowChanges", True)
    _dispatch(ctx, doc, ".uno:RejectAllTrackedChanges")
    assert len(_body_paragraphs(doc)) == initial_paras
    assert [u for _t, u in _read_portion_urls(doc)] == ["#One|outline", "#Two|outline"]

    # Re-apply and Accept All.
    res = tool.execute(tool_ctx, target="search", old_content="One", content="One B")
    assert res.get("status") == "ok", res
    res = tool.execute(tool_ctx, target="search", old_content="Two", content="Two B")
    assert res.get("status") == "ok", res
    _dispatch(ctx, doc, ".uno:AcceptAllTrackedChanges")
    assert len(_body_paragraphs(doc)) == initial_paras
    assert [u for _t, u in _read_portion_urls(doc)] == ["#One B|outline", "#Two B|outline"]


@native_test
@with_native_doc("writer", reuse=False)
def test_heading_rename_in_review_mode_uno(ctx, doc) -> None:
    from unittest.mock import patch
    from plugin.writer.inline_review import resolve_all_with_feedback

    tool_ctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">Rev Old</h1>',
            '<p>Go <a href="#Rev Old|outline">here</a>.</p>',
        ],
    )
    assert res.get("status") == "ok", res

    with patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=True):
        res = tool.execute(tool_ctx, target="search", old_content="Rev Old", content="Rev New")
        assert res.get("status") == "ok", res

    # Link redlines carry wa-review: tokens.
    redlines = doc.getRedlines()
    comments = [str(redlines.getByIndex(i).getPropertyValue("RedlineComment") or "") for i in range(redlines.getCount())]
    assert any(c.startswith("wa-review:") for c in comments), comments

    count, msg = resolve_all_with_feedback(doc, ctx, True)
    assert count > 0 and msg == "", (count, msg)
    assert doc.getRedlines().getCount() == 0
    assert [u for _t, u in _read_portion_urls(doc)] == ["#Rev New|outline"]

    with patch("plugin.writer.apply_document_content.review_recording_enabled", return_value=True):
        res = tool.execute(tool_ctx, target="search", old_content="Rev New", content="Rev Third")
        assert res.get("status") == "ok", res

    count, msg = resolve_all_with_feedback(doc, ctx, False)
    assert count > 0 and msg == "", (count, msg)
    assert doc.getRedlines().getCount() == 0
    assert [u for _t, u in _read_portion_urls(doc)] == ["#Rev New|outline"]


@native_test
@with_native_doc("writer", reuse=False)
def test_field_link_while_recording_warns_uno(ctx, doc) -> None:
    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Field Heading", False)
    cur.gotoStartOfParagraph(False)
    cur.gotoEndOfParagraph(True)
    cur.setPropertyValue("ParaStyleName", "Heading 1")
    cur.gotoEnd(False)
    text.insertControlCharacter(cur, 0, False)

    # Paragraph 1: link over a PageNumber field.
    field = doc.createInstance("com.sun.star.text.TextField.PageNumber")
    field.setPropertyValue("NumberingType", 4)
    field.setPropertyValue("SubType", uno.Enum("com.sun.star.text.PageNumberType", "CURRENT"))
    text.insertTextContent(cur, field, False)
    doc.getTextFields().refresh()
    fcur = text.createTextCursorByRange(cur.getStart())
    fcur.gotoStartOfParagraph(False)
    fcur.gotoEndOfParagraph(True)
    fcur.setPropertyValue("HyperLinkURL", "#Field Heading|outline")
    cur.gotoEnd(False)
    text.insertControlCharacter(cur, 0, False)

    # Paragraph 2: plain link with the same URL.
    text.insertString(cur, "plain link", False)
    pcur = text.createTextCursorByRange(cur.getStart())
    pcur.gotoStartOfParagraph(False)
    pcur.gotoEndOfParagraph(True)
    pcur.setPropertyValue("HyperLinkURL", "#Field Heading|outline")

    doc.setPropertyValue("RecordChanges", True)
    res = ApplyDocumentContent().execute(
        native_writer_tool_context(doc, ctx),
        target="search",
        old_content="Field Heading",
        content="Field Renamed",
    )
    assert res.get("status") == "ok", res
    warnings = res.get("warnings") or []
    assert any(w.get("code") == "LINK_TARGET_STALE" and w.get("link_target") == "#Field Heading|outline" for w in warnings), (warnings, _read_portion_urls(doc), res)

    urls = _read_portion_urls(doc)
    assert ("plain link", "#Field Renamed|outline") in urls, urls
    assert any(u == "#Field Heading|outline" for _t, u in urls), urls


@native_test
@with_native_doc("writer", reuse=False)
def test_recorded_full_document_lists_no_deleted_headings_uno(ctx, doc) -> None:
    from plugin.writer.apply_document_content import _document_headings

    tool_ctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">Old One</h1>',
            '<h2 data-lo-style="Heading 2">Old Two</h2>',
            '<h2 data-lo-style="Heading 2">Old Three</h2>',
        ],
    )
    assert res.get("status") == "ok", res

    doc.setPropertyValue("RecordChanges", True)
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">New One</h1>',
            '<h2 data-lo-style="Heading 2">Deux</h2>',
        ],
    )
    assert res.get("status") == "ok", res
    assert _document_headings(doc) == [(1, "New One"), (2, "Deux")]


@native_test
@with_native_doc("writer", reuse=False)
def test_body_edit_leaves_links_alone_uno(ctx, doc) -> None:
    tool_ctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">Keep Heading</h1>',
            '<p>Body text with <a href="#Keep Heading|outline">link</a>.</p>',
        ],
    )
    assert res.get("status") == "ok", res

    res = tool.execute(tool_ctx, target="search", old_content="Body text", content="Updated body")
    assert res.get("status") == "ok", res
    assert "links_retargeted" not in res, res
    assert _read_portion_urls(doc) == [("link", "#Keep Heading|outline")]
