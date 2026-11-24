# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native LibreOffice tests for text frame insert/delete tools."""

import os

from plugin.draw.shapes import UpsertShape
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.specialized.textframes import (
    FrameDelete,
    FrameGetInfo,
    FrameInsert,
    FrameList,
    FrameSetProperties,
)
from plugin.writer.structural import GetPageObjects
from tests.writer.docx_objects_fixture import build_docx_objects_doc


@native_test
@with_native_doc("writer")
def test_frame_insert_list_delete_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    text.insertString(cur, "Body text for frame anchor.", False)

    ins = FrameInsert().execute(
        tool_ctx,
        target="beginning",
        name="AgentFrame",
        text="Inside the frame",
        width_mm=80.0,
        height_mm=30.0,
        anchor_type=0,
    )
    assert ins.get("status") == "ok", ins
    assert ins.get("frame_name") == "AgentFrame"

    listed = FrameList().execute(tool_ctx)
    assert listed.get("status") == "ok", listed
    assert listed.get("count") == 1
    assert listed["frames"][0]["name"] == "AgentFrame"
    assert "Inside" in listed["frames"][0]["content_preview"]

    deleted = FrameDelete().execute(tool_ctx, name="AgentFrame")
    assert deleted.get("status") == "ok", deleted
    assert FrameList().execute(tool_ctx).get("count") == 0


@native_test
@with_native_doc("writer", reuse=False)
def test_docx_text_box_listed_and_deleted_uno(ctx, doc):
    reloaded, docx_path = build_docx_objects_doc(ctx, doc)
    try:
        tctx = TestingFactory.create_context(doc=reloaded, ctx=ctx, env="native")
        listed = FrameList().execute(tctx)
        assert listed.get("status") == "ok", listed
        boxes = [f for f in listed["frames"] if f["name"] == "Frame1"]
        assert len(boxes) == 1, listed
        assert boxes[0].get("shape_name") == "Text Box 2", boxes[0]
        assert "Inside the text box" in boxes[0].get("content_preview", ""), boxes[0]

        info = FrameGetInfo().execute(tctx, name="Frame1")
        assert info.get("status") == "ok", info
        assert isinstance(info.get("shape_index"), int), info

        miss = FrameGetInfo().execute(tctx, name="Frame9")
        assert miss.get("status") == "error", miss
        assert miss["details"]["available"] == ["Frame1"], miss

        page0 = GetPageObjects().execute(tctx, page=0)
        assert page0.get("status") == "ok", page0
        page_boxes = [f for f in page0["frames"] if f.get("name") == "Frame1"]
        assert len(page_boxes) == 1, page0
        assert page_boxes[0].get("shape_name") == "Text Box 2", page_boxes[0]

        deleted = FrameDelete().execute(tctx, name="Frame1")
        assert deleted.get("status") == "ok", deleted
        page0_after = GetPageObjects().execute(tctx, page=0)
        assert page0_after.get("status") == "ok", page0_after
        assert not any(f.get("name") == "Frame1" for f in page0_after["frames"]), page0_after
        assert not reloaded.getTextFrames().hasByName("Frame1")
        dp = reloaded.getDrawPage()
        assert not any(dp.getByIndex(i).getName() == "Text Box 2" for i in range(dp.getCount()))
        assert "Body paragraph two." in reloaded.getText().getString()
    finally:
        try:
            reloaded.close(True)
        except Exception:
            pass
        if os.path.exists(docx_path):
            os.unlink(docx_path)


@native_test
@with_native_doc("writer")
def test_frame_delete_page_anchored_uno(ctx, doc):
    from com.sun.star.awt import Size
    from com.sun.star.text.TextContentAnchorType import AT_PAGE

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Body stays intact.", False)

    frame = doc.createInstance("com.sun.star.text.TextFrame")
    frame.setPropertyValue("Size", Size(5000, 2000))
    frame.setPropertyValue("AnchorType", AT_PAGE)
    frame.setPropertyValue("AnchorPageNo", 1)
    text.insertTextContent(cur, frame, False)
    frame.setName("PageFrame")
    frame.getText().setString("Page frame text")

    assert frame.getAnchor() is None

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    deleted = FrameDelete().execute(tool_ctx, name="PageFrame")
    assert deleted.get("status") == "ok", deleted
    assert not doc.getTextFrames().hasByName("PageFrame")

    doc.getUndoManager().undo()
    assert doc.getTextFrames().hasByName("PageFrame")
    restored = doc.getTextFrames().getByName("PageFrame")
    assert restored.getText().getString() == "Page frame text"
    assert restored.getPropertyValue("AnchorType") == AT_PAGE
    assert text.getString() == "Body stays intact."


@native_test
@with_native_doc("writer")
def test_frame_delete_warns_when_recording_and_not_when_off_uno(ctx, doc):
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    FrameInsert().execute(
        tool_ctx, target="beginning", name="RecFrame", text="A", width_mm=40.0, height_mm=20.0, anchor_type=0
    )
    del_off = FrameDelete().execute(tool_ctx, name="RecFrame")
    assert del_off["status"] == "ok"
    assert "tracked" not in del_off and "warning" not in del_off

    FrameInsert().execute(
        tool_ctx, target="beginning", name="RecFrame2", text="B", width_mm=40.0, height_mm=20.0, anchor_type=0
    )
    doc.setPropertyValue("RecordChanges", True)
    try:
        del_on = FrameDelete().execute(tool_ctx, name="RecFrame2")
        assert del_on["status"] == "ok"
        assert del_on["tracked"] is False
        assert "warning" in del_on
        assert doc.getRedlines().getCount() == 0
        assert not doc.getTextFrames().hasByName("RecFrame2")
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_frame_insert_as_char_and_paragraph_while_recording_uno(ctx, doc):
    from plugin.framework.uno_context import execute_dispatch

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    doc.setPropertyValue("RecordChanges", True)
    try:
        res_inline = FrameInsert().execute(
            tool_ctx, target="beginning", name="InlineFrame", text="InlineText", width_mm=40.0, height_mm=20.0, anchor_type=1
        )
        assert res_inline["status"] == "ok"
        assert res_inline["tracked"] is True and res_inline["pending_review"] is True
        assert doc.getTextFrames().hasByName("InlineFrame")
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert not doc.getTextFrames().hasByName("InlineFrame")

        res_para = FrameInsert().execute(
            tool_ctx, target="beginning", name="ParaFrame", text="KeepMe", width_mm=40.0, height_mm=20.0, anchor_type=0
        )
        assert res_para["status"] == "ok"
        assert res_para["tracked"] is False and "warning" in res_para
        assert "pending_review" not in res_para
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert doc.getTextFrames().hasByName("ParaFrame")
        assert doc.getTextFrames().getByName("ParaFrame").getText().getString() == "KeepMe"
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_frame_insert_page_anchor_uses_target_page_uno(ctx, doc):
    from com.sun.star.style.BreakType import PAGE_BEFORE
    from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK
    from com.sun.star.text.TextContentAnchorType import AT_PAGE

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Page one text", False)
    text.insertControlCharacter(cur, PARAGRAPH_BREAK, False)
    cur.setPropertyValue("BreakType", PAGE_BEFORE)
    text.insertString(cur, "PAGE_TWO_MARKER", False)
    text.insertControlCharacter(cur, PARAGRAPH_BREAK, False)
    cur.setPropertyValue("BreakType", PAGE_BEFORE)
    text.insertString(cur, "Page three text", False)

    vc = doc.getCurrentController().getViewCursor()
    vc.gotoRange(text.getStart(), False)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = FrameInsert().execute(
        tool_ctx,
        target="search",
        old_content="PAGE_TWO_MARKER",
        name="Page2Frame",
        text="Frame on page 2",
        width_mm=80.0,
        height_mm=30.0,
        anchor_type=2,
    )
    assert res.get("status") == "ok", res
    assert res.get("anchor_type") == 2

    frame = doc.getTextFrames().getByName("Page2Frame")
    assert frame.getPropertyValue("AnchorType") == AT_PAGE
    assert frame.getPropertyValue("AnchorPageNo") == 2
    vc.gotoRange(frame.getText().getStart(), False)
    assert vc.getPage() == 2

    doc.getUndoManager().undo()
    assert not doc.getTextFrames().hasByName("Page2Frame")
    assert "PAGE_TWO_MARKER" in text.getString()


@native_test
@with_native_doc("writer")
def test_frame_insert_reports_real_anchor_uno(ctx, doc):
    from com.sun.star.text.TextContentAnchorType import AT_PARAGRAPH

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = FrameInsert().execute(
        tool_ctx,
        target="end",
        name="FallbackFrame",
        text="Body",
        width_mm=80.0,
        height_mm=30.0,
        anchor_type=3,
    )
    assert res.get("status") == "ok", res
    assert res.get("anchor_type") == 0
    frame = doc.getTextFrames().getByName("FallbackFrame")
    assert frame.getPropertyValue("AnchorType") == AT_PARAGRAPH


@native_test
@with_native_doc("writer")
def test_frame_insert_record_mode_inline_and_anchored_search_uno(ctx, doc):
    from plugin.framework.config import get_config, save
    from plugin.framework.uno_context import execute_dispatch

    text = doc.getText()
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    prev = get_config("doc.agent_edit_review_mode")
    save({"doc.agent_edit_review_mode": "record"})
    try:
        text.setString("hello INLINE world")
        res_inline = FrameInsert().execute(
            tool_ctx,
            target="search",
            old_content="INLINE",
            name="RecInlineFrame",
            text="Inside",
            width_mm=40.0,
            height_mm=20.0,
            anchor_type=1,
        )
        assert res_inline["status"] == "ok", res_inline
        assert res_inline["tracked"] is True and res_inline["pending_review"] is True
        assert doc.getTextFrames().hasByName("RecInlineFrame")
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert not doc.getTextFrames().hasByName("RecInlineFrame")
        assert text.getString() == "hello INLINE world"

        text.setString("hello PARA world")
        res_para = FrameInsert().execute(
            tool_ctx,
            target="search",
            old_content="PARA",
            name="RecParaFrame",
            text="Inside",
            width_mm=40.0,
            height_mm=20.0,
            anchor_type=0,
        )
        assert res_para["status"] == "ok", res_para
        assert res_para["tracked"] is False and "warning" in res_para
        assert res_para["pending_review"] is True
        redlines = doc.getRedlines()
        assert redlines.getCount() == 1
        assert redlines.getByIndex(0).getPropertyValue("RedlineType") == "Delete"
        assert "PARA" in text.getString()
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert text.getString() == "hello PARA world"
    finally:
        save({"doc.agent_edit_review_mode": prev})


@native_test
@with_native_doc("writer")
def test_frame_insert_replacing_own_insertion_has_nothing_pending_uno(ctx, doc):
    text = doc.getText()
    text.setString("hello  world")
    doc.setPropertyValue("RecordChanges", True)
    try:
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.goRight(6, False)
        text.insertString(cur, "PARA", False)

        tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
        res = FrameInsert().execute(
            tool_ctx,
            target="search",
            old_content="PARA",
            name="OwnInsertFrame",
            text="Inside",
            width_mm=40.0,
            height_mm=20.0,
            anchor_type=0,
        )
        assert res["status"] == "ok", res
        assert res["tracked"] is False
        assert "pending_review" not in res
        assert doc.getRedlines().getCount() == 0
        assert text.getString() == "hello  world"
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_docx_text_box_set_properties_refused_uno(ctx, doc):
    reloaded, docx_path = build_docx_objects_doc(ctx, doc)
    try:
        tctx = TestingFactory.create_context(doc=reloaded, ctx=ctx, env="native")
        frame = reloaded.getTextFrames().getByName("Frame1")
        orig_hori_orient = frame.getPropertyValue("HoriOrient")
        orig_hori_pos = frame.getPropertyValue("HoriOrientPosition")

        dp = reloaded.getDrawPage()
        shape = next(
            dp.getByIndex(i)
            for i in range(dp.getCount())
            if dp.getByIndex(i).getName() == "Text Box 2"
        )
        orig_size = shape.getSize()

        res1 = FrameSetProperties().execute(tctx, name="Frame1", hori_orient=1)
        assert res1.get("status") == "error", res1
        assert res1.get("code") == "TEXT_BOX_SHAPE", res1
        assert frame.getPropertyValue("HoriOrient") == orig_hori_orient
        assert frame.getPropertyValue("HoriOrientPosition") == orig_hori_pos

        res2 = FrameSetProperties().execute(tctx, name="Frame1", width_mm=90.0, height_mm=50.0)
        assert res2.get("status") == "error", res2
        assert res2.get("code") == "TEXT_BOX_SHAPE", res2
        assert shape.getSize().Width == orig_size.Width
        assert shape.getSize().Height == orig_size.Height

        up = UpsertShape().execute(
            tctx,
            action="edit",
            index=res2["details"]["shape_index"],
            width=9000,
            height=5000,
        )
        assert up.get("status") == "ok", up
        new_shape_size = shape.getSize()
        assert abs(new_shape_size.Width - 9000) <= 20, new_shape_size.Width
        assert abs(new_shape_size.Height - 5000) <= 20, new_shape_size.Height
        new_frame_size = reloaded.getTextFrames().getByName("Frame1").getPropertyValue("Size")
        assert abs(new_frame_size.Width - new_shape_size.Width) <= 20
        assert abs(new_frame_size.Height - new_shape_size.Height) <= 20
    finally:
        try:
            reloaded.close(True)
        except Exception:
            pass
        if os.path.exists(docx_path):
            os.unlink(docx_path)
