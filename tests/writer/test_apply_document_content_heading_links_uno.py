# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""UNO integration tests for heading link resolution and dollar math on import."""

from __future__ import annotations

import unittest
from typing import Any

import uno  # noqa: F401
from com.sun.star.beans import PropertyValue

from plugin.framework.uno_context import enum_has_more, execute_dispatch
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import native_writer_tool_context, with_native_doc
from plugin.writer.apply_document_content import ApplyDocumentContent
from tests.writer.doc_builders import accept_all_changes, reject_all_changes


def _read_portion_urls(doc) -> list[tuple[str, str]]:
    """Collect (text, HyperLinkURL) for all visible portions in the body."""
    urls = []
    e = doc.getText().createEnumeration()
    while enum_has_more(e):
        para = e.nextElement()
        if not para.supportsService("com.sun.star.text.Paragraph"):
            continue
        portions = para.createEnumeration()
        while enum_has_more(portions):
            portion = portions.nextElement()
            url = str(portion.getPropertyValue("HyperLinkURL") or "")
            text = str(portion.getString() or "")
            if url:
                urls.append((text, url))
    return urls


def _jump_to_mark(ctx, doc, mark_name: str) -> None:
    """Dispatch .uno:JumpToMark with Bookmark parameter to simulate a link click."""
    prop = PropertyValue()
    prop.Name = "Bookmark"
    prop.Value = mark_name
    execute_dispatch(ctx, doc.getCurrentController(), ".uno:JumpToMark", (prop,))


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


def _snapshot_paragraphs(doc) -> list[tuple[str, list[tuple[str, str, str, str, Any, Any]]]]:
    """Capture paragraph text and per-Text-portion link/formatting properties."""
    out = []
    e = doc.getText().createEnumeration()
    while enum_has_more(e):
        p = e.nextElement()
        if not p.supportsService("com.sun.star.text.Paragraph"):
            continue
        runs = []
        pe = p.createEnumeration()
        while enum_has_more(pe):
            por = pe.nextElement()
            if str(por.getPropertyValue("TextPortionType") or "") != "Text":
                continue
            runs.append((
                str(por.getString() or ""),
                str(por.getPropertyValue("HyperLinkURL") or ""),
                str(por.getPropertyValue("HyperLinkName") or ""),
                str(por.getPropertyValue("HyperLinkTarget") or ""),
                por.getPropertyValue("CharColor"),
                por.getPropertyValue("CharWeight"),
            ))
        out.append((str(p.getString() or ""), runs))
    return out


def _office_author(ctx) -> tuple[str, str]:
    from plugin.writer import review_authors

    prior = review_authors.begin(ctx)
    review_authors.end(ctx, prior)
    return prior


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
    while enum_has_more(e):
        p = e.nextElement()
        if not p.supportsService("com.sun.star.text.Paragraph"):
            continue
        pe = p.createEnumeration()
        while enum_has_more(pe):
            por = pe.nextElement()
            if str(por.getPropertyValue("HyperLinkURL") or "") == "#Old Alpha|outline":
                por.setPropertyValue("CharColor", 0x112233)

    start_snap = _snapshot_paragraphs(doc)

    # 1. old_content plain rename of Old Alpha
    res = tool.execute(tool_ctx, target="search", old_content="Old Alpha", content="New Alpha")
    assert res.get("status") == "ok", res
    assert res.get("links_retargeted", 0) >= 1, res
    assert _read_portion_urls(doc) == [
        ("link ", "#New Alpha|outline"),
        ("half", "#New Alpha|outline"),
        ("beta link", "#Old Beta|outline"),
    ]
    _jump_to_mark(ctx, doc, "New Alpha|outline")
    assert _view_cursor_paragraph_text(doc) == "New Alpha"

    # Verify bold half kept weight, color, name, target.
    half_props = None
    e = doc.getText().createEnumeration()
    while enum_has_more(e):
        p = e.nextElement()
        if not p.supportsService("com.sun.star.text.Paragraph"):
            continue
        pe = p.createEnumeration()
        while enum_has_more(pe):
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

    # One undo restores Old Alpha and #Old Alpha|outline, matching start state.
    doc.getUndoManager().undo()
    assert _snapshot_paragraphs(doc) == start_snap

    # 2. anchor span rename
    res = tool.execute(
        tool_ctx,
        target="search",
        start_anchor="Old",
        end_anchor="Alpha",
        content="Anchor Alpha",
    )
    assert res.get("status") == "ok", res
    assert _read_portion_urls(doc) == [
        ("link ", "#Anchor Alpha|outline"),
        ("half", "#Anchor Alpha|outline"),
        ("beta link", "#Old Beta|outline"),
    ]
    _jump_to_mark(ctx, doc, "Anchor Alpha|outline")
    assert _view_cursor_paragraph_text(doc) == "Anchor Alpha"

    # 3. old_content HTML rename
    res = tool.execute(
        tool_ctx,
        target="search",
        old_content="Old Beta",
        content='<h2 data-lo-style="Heading 2">Html Beta</h2>',
    )
    assert res.get("status") == "ok", res
    assert _read_portion_urls(doc) == [
        ("link ", "#Anchor Alpha|outline"),
        ("half", "#Anchor Alpha|outline"),
        ("beta link", "#Html Beta|outline"),
    ]
    _jump_to_mark(ctx, doc, "Html Beta|outline")
    assert _view_cursor_paragraph_text(doc) == "Html Beta"

    # 4. all_matches rename
    res = tool.execute(
        tool_ctx,
        target="search",
        old_content="Html Beta",
        content="All Beta",
        all_matches=True,
    )
    assert res.get("status") == "ok", res
    assert _read_portion_urls(doc) == [
        ("link ", "#Anchor Alpha|outline"),
        ("half", "#Anchor Alpha|outline"),
        ("beta link", "#All Beta|outline"),
    ]
    _jump_to_mark(ctx, doc, "All Beta|outline")
    assert _view_cursor_paragraph_text(doc) == "All Beta"

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
    assert _read_portion_urls(doc) == [
        ("link half", "#Final Alpha|outline"),
        ("beta link", "#All Beta|outline"),
    ]
    _jump_to_mark(ctx, doc, "Final Alpha|outline")
    assert _view_cursor_paragraph_text(doc) == "Final Alpha"


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
        while enum_has_more(pe):
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
            '<p>end link <a href="#Two part|outline" name="L2" target="_top">to <b>two</b></a></p>',
            '<h2 data-lo-style="Heading 2">Two <b>part</b></h2>',
        ],
    )
    assert res.get("status") == "ok", res

    # Apply direct colour on the split link so Reject/Accept formatting preservation is pinned.
    e = doc.getText().createEnumeration()
    while enum_has_more(e):
        p = e.nextElement()
        if not p.supportsService("com.sun.star.text.Paragraph"):
            continue
        pe = p.createEnumeration()
        while enum_has_more(pe):
            por = pe.nextElement()
            if str(por.getPropertyValue("HyperLinkURL") or "") == "#Two part|outline":
                por.setPropertyValue("CharColor", 0x224466)

    start_snap = _snapshot_paragraphs(doc)
    user_author = _office_author(ctx)

    doc.setPropertyValue("RecordChanges", True)
    doc.setPropertyValue("ShowChanges", False)
    res = tool.execute(tool_ctx, target="search", old_content="One", content="One B")
    assert res.get("status") == "ok", res
    assert res.get("links_retargeted", 0) >= 1, res

    res = tool.execute(tool_ctx, target="search", old_content="Two part", content="Two B")
    assert res.get("status") == "ok", res
    assert res.get("links_retargeted", 0) >= 2, res

    # Verify link redlines are authored WriterAgent, heading redlines keep the user's author,
    # and the office author was restored afterwards.
    assert _office_author(ctx) == user_author
    redlines = doc.getRedlines()
    link_authors = []
    heading_authors = []
    for i in range(redlines.getCount()):
        rl = redlines.getByIndex(i)
        author = str(rl.getPropertyValue("RedlineAuthor") or "")
        cur = rl.getPropertyValue("RedlineStart").getText().createTextCursorByRange(rl.getPropertyValue("RedlineStart"))
        cur.gotoRange(rl.getPropertyValue("RedlineEnd"), True)
        rtext = str(cur.getString() or "")
        if rtext in ("whole para link", "to ", "two"):
            link_authors.append(author)
        elif rtext in ("One", "One B", "Two part", "Two B"):
            heading_authors.append(author)
    assert link_authors and all(a == "WriterAgent" for a in link_authors), link_authors
    assert heading_authors and all(a != "WriterAgent" for a in heading_authors), heading_authors

    # Rename a second time while recording is still active.
    res = tool.execute(tool_ctx, target="search", old_content="One B", content="One C")
    assert res.get("status") == "ok", res

    doc.setPropertyValue("ShowChanges", True)
    reject_all_changes(doc, ctx)
    assert _snapshot_paragraphs(doc) == start_snap

    # Also reject once with the renames made under ShowChanges=True, and verify one Undo.
    res = tool.execute(tool_ctx, target="search", old_content="One", content="One B")
    assert res.get("status") == "ok", res
    doc.getUndoManager().undo()
    assert _snapshot_paragraphs(doc) == start_snap

    res = tool.execute(tool_ctx, target="search", old_content="One", content="One B")
    assert res.get("status") == "ok", res
    res = tool.execute(tool_ctx, target="search", old_content="Two part", content="Two B")
    assert res.get("status") == "ok", res
    reject_all_changes(doc, ctx)
    assert _snapshot_paragraphs(doc) == start_snap

    # Re-apply and Accept All: check paragraphs, URLs, and preserved link look.
    res = tool.execute(tool_ctx, target="search", old_content="One", content="One B")
    assert res.get("status") == "ok", res
    res = tool.execute(tool_ctx, target="search", old_content="Two part", content="Two B")
    assert res.get("status") == "ok", res
    accept_all_changes(doc, ctx)
    accepted = _snapshot_paragraphs(doc)
    assert len(accepted) == len(start_snap)
    assert [u for _t, u in _read_portion_urls(doc)] == [
        "#One B|outline",
        "#Two B|outline",
        "#Two B|outline",
    ]
    two_runs = [r for _pt, runs in accepted for r in runs if r[0] in ("to ", "two")]
    assert len(two_runs) == 2
    for text_val, url_val, name_val, target_val, color_val, weight_val in two_runs:
        assert url_val == "#Two B|outline"
        assert name_val == "L2"
        assert target_val == "_top"
        assert color_val == 0x224466
        if text_val == "two":
            assert weight_val > 100.0


@native_test
@with_native_doc("writer", reuse=False)
def test_heading_rename_in_review_mode_uno(ctx, doc) -> None:
    from unittest.mock import patch
    from plugin.writer.edit_review import EditReviewSession
    from plugin.writer.hyperlink_fixup import retarget_outline_links
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

    # Every redline covering the link text ("here") carries a wa-review: token.
    redlines = doc.getRedlines()
    link_comments = []
    for i in range(redlines.getCount()):
        rl = redlines.getByIndex(i)
        cur = rl.getPropertyValue("RedlineStart").getText().createTextCursorByRange(rl.getPropertyValue("RedlineStart"))
        cur.gotoRange(rl.getPropertyValue("RedlineEnd"), True)
        if str(cur.getString() or "") == "here":
            link_comments.append(str(rl.getPropertyValue("RedlineComment") or ""))
    assert len(link_comments) >= 2 and all(c.startswith("wa-review:") for c in link_comments), link_comments

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

    # Direct EditReviewSession.tag_side_effects check: tokens are stamped, no ChangeRecord is added,
    # and _pending_tokens does not wait for them.
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.tag_side_effects(lambda: retarget_outline_links(doc, {"Rev New": "Rev Direct"}))
        assert session.changes == []
        pending, reliable = session._pending_tokens()
        assert pending == set() and reliable is True
    assert doc.getRedlines().getCount() >= 2
    resolve_all_with_feedback(doc, ctx, True)


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
    from plugin.writer.heading_links import document_headings

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
    assert document_headings(doc) == [(1, "New One"), (2, "Deux")]


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


@native_test
@with_native_doc("writer", reuse=False)
def test_selection_target_heading_rename_retargets_links_uno(ctx, doc) -> None:
    tool_ctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">Old Heading</h1>',
            '<p>See <a href="#Old Heading|outline">go</a>.</p>',
        ],
    )
    assert res.get("status") == "ok", res

    cur = doc.getText().createTextCursor()
    cur.gotoStart(False)
    cur.gotoEndOfParagraph(True)
    doc.getCurrentController().select(cur)
    res = tool.execute(tool_ctx, target="selection", content="New Heading")
    assert res.get("status") == "ok", res
    assert res.get("links_retargeted") == 1, res
    assert _read_portion_urls(doc) == [("go", "#New Heading|outline")]


@native_test
@with_native_doc("writer", reuse=False)
def test_numbered_and_encoded_outline_urls_retargeted_and_jump_uno(ctx, doc) -> None:
    tool_ctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">Old title</h1>',
            "<p>Body</p>",
        ],
    )
    assert res.get("status") == "ok", res

    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    for label, url in (
        ("L1", "#1.Old title|outline"),
        ("L2", "#Old%20title|outline"),
        ("L3", "#1.Old%20title|outline"),
        ("L4", "#Old%20title%7Coutline"),
    ):
        text.insertString(cur, " ", False)
        text.insertString(cur, label, True)
        cur.setPropertyValue("HyperLinkURL", url)
        cur.collapseToEnd()

    res = tool.execute(tool_ctx, target="search", old_content="Old title", content="New title")
    assert res.get("status") == "ok", res
    assert res.get("links_retargeted") == 4, res
    assert _read_portion_urls(doc) == [
        ("L1", "#1.New title|outline"),
        ("L2", "#New%20title|outline"),
        ("L3", "#1.New%20title|outline"),
        ("L4", "#New%20title%7Coutline"),
    ]
    _jump_to_mark(ctx, doc, "1.New%20title|outline")
    assert _view_cursor_paragraph_text(doc) == "New title"


@native_test
@with_native_doc("writer", reuse=False)
def test_unrecorded_rename_undo_restores_heading_and_link_uno(ctx, doc) -> None:
    tool_ctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">Old Heading</h1>',
            '<p>See link here.</p>',
        ],
    )
    assert res.get("status") == "ok", res

    search = doc.createSearchDescriptor()
    search.SearchString = "link"
    found = doc.findFirst(search)
    found.setPropertyValue("HyperLinkURL", "#Old Heading|outline")
    found.setPropertyValue("HyperLinkName", "lnk")
    found.setPropertyValue("HyperLinkTarget", "_top")
    found.setPropertyValue("CharColor", 0x112233)

    res = tool.execute(tool_ctx, target="search", old_content="Old Heading", content="New Heading")
    assert res.get("status") == "ok", res
    assert _read_portion_urls(doc) == [("link", "#New Heading|outline")]

    doc.getUndoManager().undo()
    assert _read_portion_urls(doc) == [("link", "#Old Heading|outline")]
    found = doc.findFirst(search)
    assert found.getPropertyValue("HyperLinkName") == "lnk"
    assert found.getPropertyValue("HyperLinkTarget") == "_top"
    assert found.getPropertyValue("CharColor") == 0x112233


@native_test
@with_native_doc("writer", reuse=False)
def test_streamed_rewrite_and_append_on_heading_retargets_links_uno(ctx, doc) -> None:
    from plugin.writer.edit_review import WriterStreamedAppendSession, WriterStreamedRewriteSession

    tool_ctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()

    def _setup(heading_text: str):
        res = tool.execute(
            tool_ctx,
            target="full_document",
            content=[
                f'<h1 data-lo-style="Heading 1">{heading_text}</h1>',
                f'<p>See <a href="#{heading_text}|outline">go</a>.</p>',
            ],
        )
        assert res.get("status") == "ok", res
        cur = doc.getText().createTextCursor()
        cur.gotoStart(False)
        cur.gotoEndOfParagraph(True)
        return cur

    def _first_para() -> str:
        cur = doc.getText().createTextCursor()
        cur.gotoStart(False)
        cur.gotoEndOfParagraph(True)
        return str(cur.getString() or "")

    for kind, cls, chunk, expected in (
        ("rewrite", WriterStreamedRewriteSession, "New Head", "New Head"),
        ("append", WriterStreamedAppendSession, " Two", "Old Head Two"),
    ):
        for mode in ("plain", "recording", "review"):
            outcomes = ("plain",) if mode == "plain" else ("accept", "reject", "undo")
            for outcome in outcomes:
                cur = _setup("Old Head")
                if mode == "recording":
                    doc.setPropertyValue("RecordChanges", True)
                try:
                    sess = cls(doc, cur, "Old Head", track_reviewable=(mode == "review"))
                    sess.append_chunk(chunk)
                    warn = sess.finish()
                    assert warn is None, (kind, mode, outcome, warn)
                    assert doc.getPropertyValue("RecordChanges") is (mode == "recording")
                    if outcome == "plain":
                        assert _first_para() == expected
                        assert _read_portion_urls(doc) == [("go", f"#{expected}|outline")]
                        doc.getUndoManager().undo()
                        assert _first_para() == "Old Head"
                        assert _read_portion_urls(doc) == [("go", "#Old Head|outline")]
                    elif outcome == "accept":
                        accept_all_changes(doc, ctx)
                        assert _first_para() == expected
                        assert _read_portion_urls(doc) == [("go", f"#{expected}|outline")]
                    elif outcome == "reject":
                        reject_all_changes(doc, ctx)
                        assert _first_para() == "Old Head"
                        assert _read_portion_urls(doc) == [("go", "#Old Head|outline")]
                    else:
                        doc.getUndoManager().undo()
                        assert _first_para() == "Old Head"
                        assert _read_portion_urls(doc) == [("go", "#Old Head|outline")]
                finally:
                    doc.setPropertyValue("RecordChanges", False)


def _set_header_outline_links(doc, titles: list[str]) -> Any:
    """Turn the page header on, insert ``to-<title>`` outline links, and return the HeaderText."""
    page_style = doc.getStyleFamilies().getByName("PageStyles").getByName("Standard")
    page_style.setPropertyValue("HeaderIsOn", True)
    header_text = page_style.getPropertyValue("HeaderText")
    cur = header_text.createTextCursor()
    cur.gotoEnd(False)
    for idx, title in enumerate(titles):
        if idx > 0:
            header_text.insertString(cur, " ", False)
        header_text.insertString(cur, f"to-{title}", True)
        cur.setPropertyValue("HyperLinkURL", f"#{title}|outline")
        cur.collapseToEnd()
    return header_text


def _read_text_portion_urls(text_obj) -> list[tuple[str, str]]:
    """Collect (text, HyperLinkURL) for all visible portions in *text_obj*."""
    urls = []
    e = text_obj.createEnumeration()
    while enum_has_more(e):
        para = e.nextElement()
        if not para.supportsService("com.sun.star.text.Paragraph"):
            continue
        portions = para.createEnumeration()
        while enum_has_more(portions):
            portion = portions.nextElement()
            url = str(portion.getPropertyValue("HyperLinkURL") or "")
            text = str(portion.getString() or "")
            if url:
                urls.append((text, url))
    return urls


@native_test
@with_native_doc("writer", reuse=False)
def test_full_document_with_two_retitled_headings_warns_uno(ctx, doc) -> None:
    tool_ctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">Project scope</h1>',
            "<p>Body A</p>",
            '<h1 data-lo-style="Heading 1">Delivery costs</h1>',
            "<p>Body B</p>",
        ],
    )
    assert res.get("status") == "ok", res

    header_text = _set_header_outline_links(doc, ["Project scope", "Delivery costs"])

    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">Total delivery costs</h1>',
            "<p>Body B</p>",
            '<h1 data-lo-style="Heading 1">Agreed project scope</h1>',
            "<p>Body A</p>",
        ],
    )
    assert res.get("status") == "ok", res
    assert "links_retargeted" not in res, res
    assert _read_text_portion_urls(header_text) == [
        ("to-Project scope", "#Project scope|outline"),
        ("to-Delivery costs", "#Delivery costs|outline"),
    ]
    warnings = res.get("warnings") or []
    for title in ("Project scope", "Delivery costs"):
        assert any(
            w.get("code") == "LINK_TARGET_STALE" and w.get("link_target") == f"#{title}|outline"
            for w in warnings
        ), warnings


@native_test
@with_native_doc("writer", reuse=False)
def test_all_matches_renames_several_headings_uno(ctx, doc) -> None:
    tool_ctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">Vendor duties</h1>',
            '<h1 data-lo-style="Heading 1">Vendor fees</h1>',
            '<p>The Vendor: <a href="#Vendor duties|outline">a</a> <a href="#Vendor fees|outline">b</a></p>',
        ],
    )
    assert res.get("status") == "ok", res

    res = tool.execute(
        tool_ctx,
        target="search",
        old_content="Vendor",
        content="Supplier",
        all_matches=True,
    )
    assert res.get("status") == "ok", res
    assert res.get("links_retargeted") == 2, res
    assert not any(w.get("code") == "LINK_TARGET_STALE" for w in (res.get("warnings") or [])), res
    assert _read_portion_urls(doc) == [
        ("a", "#Supplier duties|outline"),
        ("b", "#Supplier fees|outline"),
    ]

    doc.getCurrentController().getViewCursor().gotoStart(False)
    _jump_to_mark(ctx, doc, "Supplier fees|outline")
    assert _view_cursor_paragraph_text(doc) == "Supplier fees"


@native_test
@with_native_doc("writer", reuse=False)
def test_rename_that_adds_a_heading_warns_uno(ctx, doc) -> None:
    tool_ctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">Scope</h1>',
            '<h1 data-lo-style="Heading 1">Fees</h1>',
            '<p><a href="#Scope|outline">one</a> <a href="#Fees|outline">two</a></p>',
        ],
    )
    assert res.get("status") == "ok", res

    res = tool.execute(
        tool_ctx,
        target="search",
        old_content="Scope",
        content=[
            '<h1 data-lo-style="Heading 1">Aim of work</h1>',
            "<p>x</p>",
            '<h2 data-lo-style="Heading 2">Sub</h2>',
        ],
    )
    assert res.get("status") == "ok", res
    assert _read_portion_urls(doc) == [
        ("one", "#Scope|outline"),
        ("two", "#Fees|outline"),
    ]
    stale_targets = [
        w.get("link_target")
        for w in (res.get("warnings") or [])
        if w.get("code") == "LINK_TARGET_STALE"
    ]
    assert stale_targets == ["#Scope|outline"], res

    doc.getCurrentController().getViewCursor().gotoStart(False)
    _jump_to_mark(ctx, doc, "Fees|outline")
    assert _view_cursor_paragraph_text(doc) == "Fees"


@native_test
@with_native_doc("writer", reuse=False)
def test_lost_heading_warns_only_for_live_link_while_recording_uno(ctx, doc) -> None:
    tool_ctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">Deleted Live</h1>',
            '<h1 data-lo-style="Heading 1">Deleted Struck</h1>',
            '<h1 data-lo-style="Heading 1">Kept Heading</h1>',
            '<p>Live <a href="#Deleted Live|outline">link1</a> and Struck <a href="#Deleted Struck|outline">link2</a>.</p>',
        ],
    )
    assert res.get("status") == "ok", res

    doc.setPropertyValue("RecordChanges", True)
    try:
        search = doc.createSearchDescriptor()
        search.SearchString = "link2"
        found = doc.findFirst(search)
        assert found is not None
        found.setString("")

        cur = doc.getText().createTextCursor()
        cur.gotoStart(False)
        cur.gotoNextParagraph(True)
        cur.gotoEndOfParagraph(True)
        doc.getCurrentController().select(cur)
        res = tool.execute(tool_ctx, target="selection", content="<p>Replacement</p>")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    assert res.get("status") == "ok", res
    warnings = res.get("warnings") or []
    stale_targets = [w.get("link_target") for w in warnings if w.get("code") == "LINK_TARGET_STALE"]
    assert stale_targets == ["#Deleted Live|outline"], warnings
    assert _read_portion_urls(doc) == [
        ("link1", "#Deleted Live|outline"),
        ("link2", "#Deleted Struck|outline"),
    ]


@native_test
@with_native_doc("writer", reuse=False)
def test_failed_edit_rolls_back_in_review_mode_uno(ctx, doc) -> None:
    from unittest.mock import patch

    from plugin.writer import apply_document_content as adc
    from plugin.writer.review_authors import office_author

    tool_ctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=(
            "<h1>Old Heading</h1>"
            '<p>See <a href="#Old Heading|outline">go</a>.</p>'
            "<table><tbody><tr><td>only</td></tr></tbody></table>"
        ),
    )
    assert res.get("status") == "ok", res
    doc.getUndoManager().clear()

    def _select_heading() -> None:
        cur = doc.getText().createTextCursor()
        cur.gotoStart(False)
        cur.gotoEndOfParagraph(True)
        doc.getCurrentController().select(cur)

    def _snapshot() -> tuple[Any, ...]:
        return (
            doc.getText().getString(),
            list(doc.getTextTables().getElementNames()),
            doc.getRedlines().getCount(),
            list(doc.getUndoManager().getAllUndoActionTitles()),
            bool(doc.getPropertyValue("RecordChanges")),
            office_author(ctx),
            sorted(doc.getBookmarks().getElementNames()),
        )

    _select_heading()
    before = _snapshot()

    def _fail_retarget(*_args: Any, **_kwargs: Any) -> Any:
        assert doc.getRedlines().getCount() > 0
        raise RuntimeError("boom")

    real_record_table_deletions = adc.record_table_deletions

    def _fail_after_table_delete(session: Any, d: Any, uno_ctx: Any, doomed: Any) -> bool:
        assert real_record_table_deletions(session, d, uno_ctx, doomed)
        assert d.getRedlines().getCount() > 0
        raise RuntimeError("boom")

    with patch.object(adc, "review_recording_enabled", return_value=True):
        for kwargs in (
            {"target": "search", "old_content": "Old Heading", "content": "New Heading"},
            {"target": "selection", "content": "<h1>New Heading</h1>"},
            {"target": "full_document", "content": "<h1>New Heading</h1>"},
        ):
            _select_heading()
            with patch.object(adc, "retarget_renamed_links", side_effect=_fail_retarget):
                with unittest.TestCase().assertRaisesRegex(RuntimeError, "boom"):
                    tool.execute(tool_ctx, **kwargs)
            assert _snapshot() == before

        with patch.object(adc, "record_table_deletions", side_effect=_fail_after_table_delete):
            with unittest.TestCase().assertRaisesRegex(RuntimeError, "boom"):
                tool.execute(tool_ctx, target="search", old_content="only", content="")
        assert _snapshot() == before


@native_test
@with_native_doc("writer", reuse=False)
def test_two_links_in_one_paragraph_while_recording_uno(ctx, doc) -> None:
    from plugin.writer.hyperlink_fixup import retarget_outline_links

    text = doc.getText()
    cur = text.createTextCursor()
    for idx, (label, sep) in enumerate([("go0", " . "), ("go1", " . ")]):
        if idx:
            text.insertString(cur, sep, False)
        text.insertString(cur, label, True)
        cur.setPropertyValue("HyperLinkURL", "#Old|outline")
        cur.collapseToEnd()
    text.insertString(cur, " . ", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "go2", True)
    cur.setPropertyValue("HyperLinkURL", "#Old|outline")
    cur.collapseToEnd()

    original_text = text.getString()
    doc.setPropertyValue("RecordChanges", True)

    assert retarget_outline_links(doc, {"Old": "New"}) == (3, {})
    assert retarget_outline_links(doc, {"Old": "New"}) == (0, {})

    accept_all_changes(doc, ctx)

    assert text.getString() == original_text
    urls = _read_portion_urls(doc)
    assert urls == [("go0", "#New|outline"), ("go1", "#New|outline"), ("go2", "#New|outline")]


@native_test
@with_native_doc("writer", reuse=False)
def test_rename_leaves_link_to_kept_numbered_title_uno(ctx, doc) -> None:
    tool_ctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">1 Scope</h1>',
            '<h1 data-lo-style="Heading 1">2.1 Scope</h1>',
            '<p><a href="#1 Scope|outline">first</a> and <a href="#2.1 Scope|outline">second</a></p>',
        ],
    )
    assert res.get("status") == "ok", res

    res = tool.execute(tool_ctx, target="search", old_content="1 Scope", content="Aim")
    assert res.get("status") == "ok", res
    assert res.get("links_retargeted") == 1, res
    assert _read_portion_urls(doc) == [
        ("first", "#Aim|outline"),
        ("second", "#2.1 Scope|outline"),
    ]
    doc.getCurrentController().getViewCursor().gotoEnd(False)
    _jump_to_mark(ctx, doc, "2.1 Scope|outline")
    assert _view_cursor_paragraph_text(doc) == "2.1 Scope"

    res = tool.execute(
        tool_ctx,
        target="full_document",
        content=[
            '<h1 data-lo-style="Heading 1">Scope</h1>',
            '<p><a href="#Scope|outline">one</a> and <a href="#Scope|outline">two</a></p>',
        ],
    )
    assert res.get("status") == "ok", res

    res = tool.execute(tool_ctx, target="search", old_content="Scope", content="1.Scope")
    assert res.get("status") == "ok", res
    assert res.get("links_retargeted") == 2, res
    assert _read_portion_urls(doc) == [
        ("one", "#1.Scope|outline"),
        ("two", "#1.Scope|outline"),
    ]
    doc.getCurrentController().getViewCursor().gotoEnd(False)
    _jump_to_mark(ctx, doc, "1.Scope|outline")
    assert _view_cursor_paragraph_text(doc) == "1.Scope"
