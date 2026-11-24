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
from plugin.writer.images.images import ImageDelete, ImageGetInfo, ImageList
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
