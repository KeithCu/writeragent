# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests: ImageList preserves the view cursor position."""

from __future__ import annotations

import os

from plugin.doc.text_helpers import clone_text_range
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.images.image_tools import insert_image_at_locator
from plugin.writer.images.images import ImageDelete, ImageGetInfo, ImageInsert, ImageList
from plugin.draw.shapes import DeleteShape
from tests.writer.docx_objects_fixture import build_docx_objects_doc


def _logo_path() -> str:
    here = os.path.abspath(__file__)
    directory = os.path.dirname(here)
    for _unused in range(6):
        for rel in ("extension/assets/logo_32.png", "assets/logo_32.png"):
            path = os.path.join(directory, *rel.split("/"))
            if os.path.isfile(path):
                return path
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return os.path.join(directory, "extension", "assets", "logo_32.png")


@native_test
@with_native_doc("writer")
def test_image_list_preserves_view_cursor(ctx, doc):
    logo = _logo_path()
    assert os.path.isfile(logo), f"fixture image missing: {logo}"

    text = doc.getText()
    text.setString("")
    cursor = text.createTextCursor()
    text.insertString(cursor, "First paragraph\n", False)
    text.insertString(cursor, "Second paragraph to park on\n", False)
    text.insertString(cursor, "Third paragraph\n", False)

    # Insert two graphics
    g1 = insert_image_at_locator(ctx, doc, logo, width_mm=15, height_mm=15)
    g2 = insert_image_at_locator(ctx, doc, logo, width_mm=15, height_mm=15)
    assert g1 is not None and g2 is not None

    # Park view cursor on the second paragraph
    search = doc.createSearchDescriptor()
    search.setSearchString("Second paragraph to park on")
    found = doc.findFirst(search)
    assert found is not None

    vc = doc.getCurrentController().getViewCursor()
    vc.gotoRange(found.getStart(), False)
    saved_pos = clone_text_range(vc)

    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
    res = ImageList().execute(tctx)
    assert res.get("status") == "ok"
    assert len(res.get("images", [])) >= 2

    # View cursor must remain on the parked paragraph
    assert text.compareRegionStarts(vc.getStart(), saved_pos.getStart()) == 0
    doc_svc = tctx.services.document
    para_ranges = doc_svc.get_paragraph_ranges(doc)
    p_idx_vc = doc_svc.find_paragraph_for_range(vc, para_ranges, text)
    p_idx_saved = doc_svc.find_paragraph_for_range(saved_pos, para_ranges, text)
    assert p_idx_vc == p_idx_saved


@native_test
@with_native_doc("writer", reuse=False)
def test_docx_rotated_pictures_listed_and_deleted_uno(ctx, doc):
    reloaded, docx_path = build_docx_objects_doc(ctx, doc)
    try:
        tctx = TestingFactory.create_context(doc=reloaded, ctx=ctx, env="native")
        listed = ImageList().execute(tctx)
        assert listed.get("status") == "ok", listed
        imgs = listed["images"]

        img11 = [i for i in imgs if i["name"] == "Image 11"]
        assert len(img11) == 1, imgs
        assert isinstance(img11[0].get("shape_index"), int), img11[0]

        img1s = [i for i in imgs if i["name"] == "Image 1"]
        assert len(img1s) == 2, imgs
        assert all(isinstance(i.get("shape_index"), int) for i in img1s), img1s

        info = ImageGetInfo().execute(tctx, name="Image 11")
        assert info.get("status") == "ok", info

        ambig = ImageDelete().execute(tctx, name="Image 1")
        assert ambig.get("status") == "error", ambig
        assert ambig.get("code") == "AMBIGUOUS_NAME", ambig
        assert "shape_delete" in ambig.get("message", ""), ambig

        dp = reloaded.getDrawPage()
        # Bind each shape once (docs/framework/uno-test-lifecycle.md): a late release of a dropped
        # proxy can detach the next one, and a detached shape reports no GraphicObjectShape service.
        shapes = [dp.getByIndex(i) for i in range(dp.getCount())]
        assert [s.getName() for s in shapes].count("Image 1") == 2
        assert reloaded.getGraphicObjects().hasByName("Image 1")

        refused = ImageDelete().execute(tctx, name="Image 11")
        assert refused.get("status") == "error", refused
        assert refused.get("code") == "IMAGE_DELETE_NOT_TRACKABLE", refused
        shapes = [dp.getByIndex(i) for i in range(dp.getCount())]
        assert "Image 11" in [s.getName() for s in shapes]

        reloaded.setPropertyValue("RecordChanges", False)
        del11 = ImageDelete().execute(tctx, name="Image 11")
        assert del11.get("status") == "ok", del11
        shapes = [dp.getByIndex(i) for i in range(dp.getCount())]
        assert "Image 11" not in [s.getName() for s in shapes]

        # Draw-page index of the GraphicObjectShape "Image 1" (not the FrameShape fly).
        draw_img1_idx = next(
            i
            for i, s in enumerate(shapes)
            if s.getName() == "Image 1" and s.supportsService("com.sun.star.drawing.GraphicObjectShape")
        )
        del_shape = DeleteShape().execute(tctx, index=draw_img1_idx)
        assert del_shape.get("status") == "ok", del_shape
        assert reloaded.getGraphicObjects().hasByName("Image 1")
        shapes = [dp.getByIndex(i) for i in range(dp.getCount())]
        assert not any(
            s.getName() == "Image 1" and s.supportsService("com.sun.star.drawing.GraphicObjectShape") for s in shapes
        )
    finally:
        try:
            reloaded.close(True)
        except Exception:
            pass
        if os.path.exists(docx_path):
            os.unlink(docx_path)


def _verify_recorded_insert(doc, ctx, insert_fn, text_obj, *, undo_redo=False):
    """Run *insert_fn* while recording, verify 1 Insert redline, Reject, Accept, and optional Undo/Redo."""
    from plugin.framework.uno_context import execute_dispatch

    expected_text = text_obj.getString()
    before_graphics = doc.getGraphicObjects().getCount()
    assert insert_fn() is not None
    assert doc.getRedlines().getCount() == 1
    assert doc.getRedlines().getByIndex(0).getPropertyValue("RedlineType") == "Insert"
    assert "x" not in text_obj.getString()

    if undo_redo:
        um = doc.getUndoManager()
        um.undo()
        assert doc.getRedlines().getCount() == 0
        assert text_obj.getString() == expected_text
        um.redo()
        assert doc.getRedlines().getCount() == 1
        assert doc.getRedlines().getByIndex(0).getPropertyValue("RedlineType") == "Insert"

    execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
    assert doc.getRedlines().getCount() == 0
    assert text_obj.getString() == expected_text
    assert doc.getGraphicObjects().getCount() == before_graphics

    assert insert_fn() is not None
    assert doc.getRedlines().getCount() == 1
    assert doc.getRedlines().getByIndex(0).getPropertyValue("RedlineType") == "Insert"
    execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
    assert doc.getRedlines().getCount() == 0
    assert doc.getGraphicObjects().getCount() == before_graphics + 1
    assert "x" not in text_obj.getString()


@native_test
@with_native_doc("writer", reuse=False)
def test_image_insert_and_delete_tracked_uno(ctx, doc):
    """Embedded image_insert records an Insert redline (Reject removes, Accept keeps, Undo/Redo exact); Record mode tags comments."""
    import shutil
    import tempfile
    from plugin.framework.config import get_config, save
    from plugin.framework.uno_context import execute_dispatch

    logo = _logo_path()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_logo = os.path.join(tmpdir, "logo.png")
        shutil.copyfile(logo, tmp_logo)
        tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
        text = doc.getText()

        # Body middle, empty paragraph, header, and table cell while recording
        from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK

        text.setString("")
        setup_cur = text.createTextCursor()
        text.insertString(setup_cur, "BeforeAfter", False)
        text.insertControlCharacter(setup_cur, PARAGRAPH_BREAK, False)
        tbl = doc.createInstance("com.sun.star.text.TextTable")
        tbl.initialize(1, 1)
        text.insertTextContent(text.getEnd(), tbl, False)
        tbl.setName("CellImgTbl")
        cell = tbl.getCellByName("A1")
        doc.setPropertyValue("RecordChanges", True)
        try:
            def _ins_mid():
                cur = text.createTextCursor()
                cur.gotoStart(False)
                cur.goRight(6, False)
                return insert_image_at_locator(ctx, doc, tmp_logo, width_mm=10, height_mm=10, text_cursor=cur)

            _verify_recorded_insert(doc, ctx, _ins_mid, text, undo_redo=True)

            def _ins_empty():
                cur_empty = text.createTextCursor()
                cur_empty.gotoStart(False)
                cur_empty.goRight(len("BeforeAfter") + 1, False)
                return insert_image_at_locator(ctx, doc, tmp_logo, width_mm=10, height_mm=10, text_cursor=cur_empty)

            _verify_recorded_insert(doc, ctx, _ins_empty, text, undo_redo=True)

            # Header insert
            hdr_text = doc.getStyleFamilies().getByName("PageStyles").getByName("Standard")

            def _ins_hdr():
                res_hdr = ImageInsert().execute(
                    tctx, path=tmp_logo, target="header", style="Standard", auto_height=True, width_mm=10, height_mm=10
                )
                assert res_hdr["status"] == "ok", res_hdr
                return res_hdr["image_name"]

            res_hdr0 = ImageInsert().execute(
                tctx, path=tmp_logo, target="header", style="Standard", auto_height=True, width_mm=10, height_mm=10
            )
            assert res_hdr0["status"] == "ok", res_hdr0
            assert res_hdr0.get("style_unreviewed") is True
            assert doc.getRedlines().getCount() == 1
            assert doc.getRedlines().getByIndex(0).getPropertyValue("RedlineType") == "Insert"
            header_obj = hdr_text.getPropertyValue("HeaderText")
            assert "x" not in header_obj.getString()
            execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
            assert header_obj.getString() == ""
            assert doc.getGraphicObjects().getCount() == 2

            _verify_recorded_insert(doc, ctx, _ins_hdr, header_obj)

            # Cell insert (via view cursor, matching ImageInsert at cursor)
            def _ins_cell():
                doc.getCurrentController().getViewCursor().gotoRange(cell.getEnd(), False)
                return insert_image_at_locator(ctx, doc, tmp_logo, width_mm=10, height_mm=10)

            _verify_recorded_insert(doc, ctx, _ins_cell, cell)
        finally:
            doc.setPropertyValue("RecordChanges", False)

        # Record mode: image_insert and image_delete carry wa-review: comments
        prev = get_config("doc.agent_edit_review_mode")
        save({"doc.agent_edit_review_mode": "record"})
        try:
            res_ins = ImageInsert().execute(
                tctx, path=tmp_logo, target="body", style="Standard", auto_height=True, width_mm=10, height_mm=10
            )
            assert res_ins["status"] == "ok" and res_ins.get("pending_review") is True
            comments = [
                doc.getRedlines().getByIndex(i).getPropertyValue("RedlineComment")
                for i in range(doc.getRedlines().getCount())
            ]
            assert any("wa-review:" in c for c in comments), comments
            execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")

            res_del = ImageDelete().execute(tctx, name=res_ins["image_name"])
            assert res_del["status"] == "ok" and res_del.get("tracked") is True
            comments_del = [
                doc.getRedlines().getByIndex(i).getPropertyValue("RedlineComment")
                for i in range(doc.getRedlines().getCount())
            ]
            assert any("wa-review:" in c for c in comments_del), comments_del
            execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        finally:
            save({"doc.agent_edit_review_mode": prev})


@native_test
@with_native_doc("writer")
def test_insert_frame_while_recording_accept_and_reject_uno(ctx, doc):
    from plugin.framework.uno_context import execute_dispatch
    from plugin.writer.images.image_tools import insert_image

    logo = _logo_path()
    doc.setPropertyValue("RecordChanges", True)
    try:
        insert_image(ctx, doc, logo, width_px=64, height_px=64, title="Cap", description="", add_frame=True)
        assert doc.getTextFrames().getCount() == 1
        assert doc.getGraphicObjects().getCount() == 1
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert doc.getTextFrames().getCount() == 0
        assert doc.getGraphicObjects().getCount() == 0

        insert_image(ctx, doc, logo, width_px=64, height_px=64, title="Cap", description="", add_frame=True)
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        assert doc.getTextFrames().getCount() == 1
        assert doc.getGraphicObjects().getCount() == 1
    finally:
        doc.setPropertyValue("RecordChanges", False)
