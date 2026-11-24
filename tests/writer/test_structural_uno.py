# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

import uno  # noqa: F401
from typing import Any

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc, writer_services_tool_context
from plugin.writer.structural import GetPageObjects, CloneHeadingBlock


@native_test
@with_native_doc("writer")
def test_structural_tools_execution(ctx, doc):
    mock_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")

    # Test BookmarkList via registry
    from plugin.app import get_tools
    registry = get_tools()
    list_bm_tool = registry.get("bookmark_list")
    assert list_bm_tool is not None, "list_bookmarks tool not found in registry"
    
    bm_res = list_bm_tool.execute(mock_ctx)
    assert bm_res["status"] == "ok", f"BookmarkList failed: {bm_res}"
    assert isinstance(bm_res["bookmarks"], list), "BookmarkList should return a list"

    list_sec_tool = registry.get("section_list")
    assert list_sec_tool is not None, "list_sections (structural domain) should be registered"
    sec_res = list_sec_tool.execute(mock_ctx)
    assert sec_res["status"] == "ok", f"SectionList failed: {sec_res}"
    assert isinstance(sec_res["sections"], list), "SectionList should return a list"


@native_test
@with_native_doc("writer")
def test_get_page_objects_with_table_at_page_end_uno(ctx, doc):
    """Regression: page_get_objects from a table-cell cursor.

    Cloning the view cursor through doc.getText() after jumpToEndOfPage used to raise
    UNO RuntimeException "End of content node doesn't have the proper start node".
    lockControllers() made gotoRange/getPage fail when the cursor started in a cell;
    leave via body getStart() first, unlock before restore. Table-anchor hops while
    locked leave getPage() at 0 — that is stale layout, not an empty page.
    Must list the outer table and a nested table, then restore the cell cursor.
    """
    text = doc.getText()
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(6, 3)
    text.insertTextContent(text.getEnd(), tbl, False)
    for name in tbl.getCellNames():
        tbl.getCellByName(name).setString("cell " + name)
    inner = doc.createInstance("com.sun.star.text.TextTable")
    inner.initialize(2, 2)
    host = tbl.getCellByName("B2")
    host.insertTextContent(host.getStart(), inner, False)
    for name in inner.getCellNames():
        inner.getCellByName(name).setString("inner " + name)

    cell = tbl.getCellByName("A1")
    vc = doc.getCurrentController().getViewCursor()
    vc.gotoRange(cell, False)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = GetPageObjects().execute(tool_ctx, page=0)
    assert res.get("status") == "ok", res
    names = [t.get("name") for t in res.get("tables") or []]
    assert tbl.getName() in names, res
    assert inner.getName() in names, res
    # Save/restore must leave the view cursor in the nested cell XText.
    restored = vc.getPropertyValue("TextTable")
    assert restored is not None
    assert restored.getName() == tbl.getName()


@native_test
@with_native_doc("writer")
def test_get_page_objects_from_table_cell_resolves_page_two_uno(ctx, doc):
    """Repro #1422: page_get_objects with locator from a table-cell cursor.

    When the view cursor sits in a table cell, saving the cursor via doc.getText()
    raised RuntimeException, falling back to page 1 instead of page 2.
    """
    import time
    from com.sun.star.style.BreakType import PAGE_BEFORE

    text = doc.getText()
    cursor = text.createTextCursor()

    # Page 1: table
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 2)
    text.insertTextContent(cursor, tbl, False)

    # Move cursor past table and insert Page 2 heading
    cursor.gotoEnd(False)
    cursor.setPropertyValue("BreakType", PAGE_BEFORE)
    cursor.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertString(cursor, "Target Heading on Page Two", False)

    time.sleep(0.5)

    # Place view cursor inside table cell on Page 1
    cell = tbl.getCellByName("A1")
    vc = doc.getCurrentController().getViewCursor()
    vc.gotoRange(cell, False)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = GetPageObjects().execute(tool_ctx, locator="heading_text:Target Heading on Page Two")
    assert res.get("status") == "ok", res
    assert res.get("page") == 1, res
    restored = vc.getPropertyValue("TextTable")
    assert restored is not None
    assert restored.getName() == tbl.getName()


@native_test
@with_native_doc("writer")
def test_clone_heading_block_without_tracked_deletions_uno(ctx, doc):
    text = doc.getText()
    cursor = text.createTextCursor()

    # 1. Setup a heading and some body text
    cursor.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertString(cursor, "My Heading", False)
    text.insertControlCharacter(cursor, 0, False) # PARAGRAPH_BREAK

    cursor.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cursor, "This is good text.", False)

    # Enable track changes
    doc.RecordChanges = True

    # Delete " good" using track changes
    cursor.gotoStartOfParagraph(False)
    cursor.goRight(7, False) # "This is"
    cursor.goRight(5, True) # " good"
    cursor.setString("")

    # Let's ensure track changes was effective - it should be "This is text." without tracking,
    # but the underlying string (getString()) still returns "This is good text."

    doc.RecordChanges = False

    # Build tree service so we can clone it
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    from plugin.writer.specialized.bookmarks import BookmarkService
    from plugin.writer.tree import TreeService

    tool_ctx.services.register("writer_bookmarks", BookmarkService(tool_ctx.services))
    tool_ctx.services.register("writer_tree", TreeService(tool_ctx.services))

    clone_res = CloneHeadingBlock().execute(tool_ctx, paragraph_index=0)
    assert clone_res.get("status") == "ok", clone_res

    # We started with 2 paragraphs, cloning should append 2 more
    # The last paragraph should now be the cloned body paragraph.
    # Its text should NOT include " good"

    enum = text.createEnumeration()
    last_para = None
    count = 0
    while enum.hasMoreElements():
        last_para = enum.nextElement()
        count += 1

    assert count == 4, f"Expected 4 paragraphs, got {count}"
    assert last_para is not None
    assert last_para.getString() == "This is text.", f"Tracked deletions were cloned! Got: '{last_para.getString()}'"


@native_test
@with_native_doc("writer")
def test_clone_heading_block_inserts_before_next_heading_uno(ctx, doc):
    """Clone lands between the source block and the next heading.

    insertControlCharacter at the end of the block retargets that paragraph's
    UNO range onto the new paragraph. Re-reading it cloned the heading twice.
    gotoNextParagraph from this cursor skips the new paragraph and writes into
    the following heading. Source getString() keeps the tracked deletion;
    the cloned body does not.
    """
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertString(cursor, "My Heading", False)
    text.insertControlCharacter(cursor, 0, False)  # PARAGRAPH_BREAK
    cursor.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cursor, "This is good text.", False)
    text.insertControlCharacter(cursor, 0, False)
    cursor.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertString(cursor, "Next", False)
    text.insertControlCharacter(cursor, 0, False)
    cursor.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cursor, "AFTER", False)

    doc.RecordChanges = True
    enum = text.createEnumeration()
    body = None
    while enum.hasMoreElements():
        el = enum.nextElement()
        if el.getString() == "This is good text.":
            body = el
    assert body is not None
    body_cursor = text.createTextCursorByRange(body)
    body_cursor.gotoStartOfParagraph(False)
    body_cursor.goRight(7, False)  # "This is"
    body_cursor.goRight(5, True)  # " good"
    body_cursor.setString("")
    doc.RecordChanges = False

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    from plugin.writer.specialized.bookmarks import BookmarkService
    from plugin.writer.tree import TreeService

    tool_ctx.services.register("writer_bookmarks", BookmarkService(tool_ctx.services))
    tool_ctx.services.register("writer_tree", TreeService(tool_ctx.services))
    clone_res = CloneHeadingBlock().execute(tool_ctx, paragraph_index=0)
    assert clone_res.get("status") == "ok", clone_res

    rows = []
    enum = text.createEnumeration()
    while enum.hasMoreElements():
        el = enum.nextElement()
        rows.append((el.getString(), el.getPropertyValue("ParaStyleName")))
    assert rows == [
        ("My Heading", "Heading 1"),
        ("This is good text.", "Standard"),
        ("My Heading", "Heading 1"),
        ("This is text.", "Standard"),
        ("Next", "Heading 1"),
        ("AFTER", "Standard"),
    ], rows


def _rect(doc, text=""):
    shape = doc.createInstance("com.sun.star.drawing.RectangleShape")
    doc.getDrawPage().add(shape)
    if text:
        shape.setString(text)
    return shape


@native_test
@with_native_doc("writer")
def test_get_page_objects_reads_draw_shape_text_uno(ctx, doc):
    """GetPageObjects extracts draw-shape text through the getString short-circuit."""
    for _idx in range(6):
        _rect(doc)
    _rect(doc, "one")
    _rect(doc, "alpha\nbeta")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = GetPageObjects().execute(tool_ctx, page=0)
    assert res.get("status") == "ok", res
    shapes = res.get("shapes") or []
    assert len(shapes) == 8, res
    texts = [s.get("text") for s in shapes]
    assert texts[:6] == [""] * 6
    assert texts[6] == "one"
    assert texts[7] == "alpha\nbeta"


@native_test
@with_native_doc("writer")
def test_page_get_objects_and_image_get_agree_on_first_page_uno(ctx, doc):
    """page_get_objects, image_get, nav_goto_page, and page:N locators all use 0-based pages."""
    import base64
    import io
    import pytest
    from PIL import Image
    from com.sun.star.awt import Point, Size
    from com.sun.star.style.BreakType import PAGE_BEFORE
    from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK
    from com.sun.star.text.TextContentAnchorType import AT_PARAGRAPH

    from plugin.doc.document_helpers import resolve_locator
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer.get_image import GetImage
    from plugin.writer.structural import NavGotoPage

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "First page", False)

    shape = doc.createInstance("com.sun.star.drawing.RectangleShape")
    shape.setSize(Size(6000, 6000))
    shape.setPosition(Point(2000, 2000))
    shape.setPropertyValue("AnchorType", AT_PARAGRAPH)
    shape.setPropertyValue("FillColor", 0xFF0000)
    shape.setPropertyValue("LineColor", 0xFF0000)
    shape.Name = "RedBox"
    doc.getDrawPage().add(shape)

    text.insertControlCharacter(cur, PARAGRAPH_BREAK, False)
    cur.setPropertyValue("BreakType", PAGE_BEFORE)
    text.insertString(cur, "Second page", False)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")

    obj0 = GetPageObjects().execute(tool_ctx, page=0)
    assert obj0.get("status") == "ok", obj0
    assert obj0.get("page") == 0, obj0
    assert [s.get("name") for s in obj0.get("shapes") or []] == ["RedBox"]

    obj1 = GetPageObjects().execute(tool_ctx, page=1)
    assert obj1.get("status") == "ok", obj1
    assert obj1.get("page") == 1, obj1
    assert obj1.get("shapes") == []

    def _red_pixels(page_idx: int) -> int:
        res = GetImage().execute(tool_ctx, page=page_idx)
        assert res.get("status") == "ok", res
        raw = base64.b64decode(res["_mcp_image"]["data"])
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        return sum(1 for r, g, b in img.getdata() if r > 200 and g < 50 and b < 50)

    assert _red_pixels(0) > 100
    assert _red_pixels(1) == 0

    nav0 = NavGotoPage().execute(tool_ctx, page=0)
    assert nav0 == {"status": "ok", "page": 0}
    vc = doc.getCurrentController().getViewCursor()
    assert vc.getPage() == 1
    nav_bad = NavGotoPage().execute(tool_ctx, page=2)
    assert nav_bad.get("status") == "error"
    assert "2 page(s)" in nav_bad.get("message", "")
    assert vc.getPage() == 1

    obj_bad = GetPageObjects().execute(tool_ctx, page=2)
    assert obj_bad.get("status") == "error"
    assert "2 page(s)" in obj_bad.get("message", "")

    assert resolve_locator(doc, "page:1")["para_index"] == 1
    with pytest.raises(ToolExecutionError, match="2 page\\(s\\)"):
        resolve_locator(doc, "page:2")


@native_test
@with_native_doc("writer", reuse=False)
def test_page_get_objects_frames_include_text_boxes_uno(ctx, doc):
    from com.sun.star.awt import Point, Size
    from com.sun.star.style.BreakType import PAGE_BEFORE
    from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_PAGE, AT_PARAGRAPH

    from plugin.writer.specialized.textframes import FrameGetInfo

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Page zero body", False)

    def _add_box(name: str, box_text: str, anchor_type: Any, anchor_range: Any = None, page_no: int | None = None) -> None:
        shape = doc.createInstance("com.sun.star.drawing.RectangleShape")
        shape.setSize(Size(4000, 2000))
        shape.setPosition(Point(1000, 1000))
        shape.Name = name
        if anchor_range is not None:
            shape.setPropertyValue("AnchorType", anchor_type)
            anchor_range.getText().insertTextContent(anchor_range, shape, False)
        else:
            doc.getDrawPage().add(shape)
            shape.setPropertyValue("AnchorType", anchor_type)
            if page_no is not None:
                shape.setPropertyValue("AnchorPageNo", page_no)
        shape.setPropertyValue("TextBox", True)
        shape.setString(box_text)

    _add_box("ParaBox0", "Box on page 0", AT_PARAGRAPH)

    text.insertControlCharacter(cur, PARAGRAPH_BREAK, False)
    cur.setPropertyValue("BreakType", PAGE_BEFORE)
    text.insertString(cur, "Page one body ", False)
    cur.gotoEnd(False)

    _add_box("AsCharBox1", "AsChar box", AS_CHARACTER, anchor_range=cur)
    _add_box("AtPageBox1", "AtPage box", AT_PAGE, page_no=2)

    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    text.insertTextContent(text.getEnd(), tbl, False)
    cell = tbl.getCellByName("A1")
    cell_cur = cell.createTextCursor()
    _add_box("CellBox1", "Cell box", AT_PARAGRAPH, anchor_range=cell_cur)

    plain = doc.createInstance("com.sun.star.text.TextFrame")
    plain.setName("PlainFrame1")
    plain.setSize(Size(4000, 2000))
    plain.setPropertyValue("AnchorType", AT_PARAGRAPH)
    text.insertTextContent(text.getEnd(), plain, False)

    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    p0 = GetPageObjects().execute(tctx, page=0)
    assert p0.get("status") == "ok", p0
    assert {f.get("shape_name") for f in p0["frames"]} == {"ParaBox0"}, p0

    p1 = GetPageObjects().execute(tctx, page=1)
    assert p1.get("status") == "ok", p1
    plain_entries = [f for f in p1["frames"] if f["name"] == "PlainFrame1"]
    assert len(plain_entries) == 1, p1
    assert "shape_index" not in plain_entries[0], plain_entries[0]

    box_entries = [f for f in p1["frames"] if "shape_name" in f]
    assert {f["shape_name"] for f in box_entries} == {"AsCharBox1", "AtPageBox1", "CellBox1"}, p1

    target_name = next(f["name"] for f in box_entries if f["shape_name"] == "AtPageBox1")
    info = FrameGetInfo().execute(tctx, name=target_name)
    assert info.get("status") == "ok", info
    # AT_PAGE text boxes have no body anchor (getAnchor() is None), so paragraph_index is None.
    assert info.get("paragraph_index") is None, info
    assert "AtPage box" in info.get("content", ""), info


@native_test
@with_native_doc("writer")
def test_clone_heading_block_hides_multi_para_tracked_deletion_uno(ctx, doc):
    from plugin.tests.testing_utils import strike_across_paragraphs

    text = doc.getText()
    strike_across_paragraphs(doc, text)
    first_para = text.createEnumeration().nextElement()
    cur = text.createTextCursorByRange(first_para)
    cur.setPropertyValue("ParaStyleName", "Heading 1")

    tctx = writer_services_tool_context(doc, ctx)
    res = CloneHeadingBlock().execute(tctx, paragraph_index=0)
    assert res["status"] == "ok", res
    body = text.getString()
    assert body.count("Keep") == 2, body
    assert body.count("gone") == 3, body  # only the original struck text, not cloned
