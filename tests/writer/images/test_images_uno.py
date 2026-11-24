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
        assert len([i for i in range(dp.getCount()) if dp.getByIndex(i).getName() == "Image 1"]) == 2
        assert reloaded.getGraphicObjects().hasByName("Image 1")

        refused = ImageDelete().execute(tctx, name="Image 11")
        assert refused.get("status") == "error", refused
        assert refused.get("code") == "IMAGE_DELETE_NOT_TRACKABLE", refused
        assert "Image 11" in [dp.getByIndex(i).getName() for i in range(dp.getCount())]

        reloaded.setPropertyValue("RecordChanges", False)
        del11 = ImageDelete().execute(tctx, name="Image 11")
        assert del11.get("status") == "ok", del11
        assert "Image 11" not in [dp.getByIndex(i).getName() for i in range(dp.getCount())]

        # Find the draw-page index of the GraphicObjectShape "Image 1" (not the FrameShape fly).
        draw_img1_idx = next(
            i
            for i in range(dp.getCount())
            if dp.getByIndex(i).getName() == "Image 1"
            and dp.getByIndex(i).supportsService("com.sun.star.drawing.GraphicObjectShape")
        )
        del_shape = DeleteShape().execute(tctx, index=draw_img1_idx)
        assert del_shape.get("status") == "ok", del_shape
        assert reloaded.getGraphicObjects().hasByName("Image 1")
        assert not any(
            dp.getByIndex(i).getName() == "Image 1"
            and dp.getByIndex(i).supportsService("com.sun.star.drawing.GraphicObjectShape")
            for i in range(dp.getCount())
        )
    finally:
        try:
            reloaded.close(True)
        except Exception:
            pass
        if os.path.exists(docx_path):
            os.unlink(docx_path)


@native_test
@with_native_doc("writer", reuse=False)
def test_image_insert_and_delete_tracked_uno(ctx, doc):
    """Embedded image_insert records an Insert redline (Reject removes, Undo/Redo exact); Record mode tags comments."""
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
        text.setString("BeforeAfter\n\n")
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.goRight(6, False)
        doc.setPropertyValue("RecordChanges", True)
        try:
            g_mid = insert_image_at_locator(ctx, doc, tmp_logo, width_mm=10, height_mm=10, text_cursor=cur)
            assert g_mid is not None
            assert doc.getRedlines().getCount() == 1
            assert "x" not in text.getString()

            um = doc.getUndoManager()
            um.undo()
            assert doc.getRedlines().getCount() == 0
            assert text.getString() == "BeforeAfter\n\n"
            um.redo()
            assert doc.getRedlines().getCount() == 1
            execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
            assert text.getString() == "BeforeAfter\n\n"
            assert doc.getGraphicObjects().getCount() == 0

            # Empty paragraph insert
            cur_empty = text.createTextCursor()
            cur_empty.gotoEnd(False)
            g_emp = insert_image_at_locator(ctx, doc, tmp_logo, width_mm=10, height_mm=10, text_cursor=cur_empty)
            assert g_emp is not None
            assert doc.getRedlines().getCount() == 1
            execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
            assert doc.getGraphicObjects().getCount() == 0

            # Header insert
            res_hdr = ImageInsert().execute(
                tctx, path=tmp_logo, target="header", style="Standard", auto_height=True, width_mm=10, height_mm=10
            )
            assert res_hdr["status"] == "ok", res_hdr
            assert res_hdr.get("style_unreviewed") is True
            assert doc.getRedlines().getCount() >= 1
            execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")

            # Cell insert (via view cursor, matching ImageInsert at cursor)
            tbl = doc.createInstance("com.sun.star.text.TextTable")
            tbl.initialize(1, 1)
            text.insertTextContent(text.getEnd(), tbl, False)
            cell = tbl.getCellByName("A1")
            doc.getCurrentController().getViewCursor().gotoRange(cell.getEnd(), False)
            g_cell = insert_image_at_locator(ctx, doc, tmp_logo, width_mm=10, height_mm=10)
            assert g_cell is not None
            execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
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
