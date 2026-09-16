# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live get_image page=N on Draw and Impress (GraphicExportFilter on XDrawPage)."""
import base64

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _add_rect(doc, page, color, x=1000, y=1000):
    from com.sun.star.awt import Point, Size

    shape = doc.createInstance("com.sun.star.drawing.RectangleShape")
    shape.setPosition(Point(x, y))
    shape.setSize(Size(8000, 5000))
    shape.setPropertyValue("FillColor", color)
    shape.setPropertyValue("FillStyle", 1)  # SOLID
    page.add(shape)


def _two_pages(doc):
    pages = doc.getDrawPages()
    if pages.getCount() < 2:
        pages.insertNewByIndex(1)
    _add_rect(doc, pages.getByIndex(0), 0xFF0000)
    _add_rect(doc, pages.getByIndex(1), 0x0000FF, x=12000, y=8000)
    return pages


def _exec(doc, ctx, doc_type, **kwargs):
    from plugin.framework.tool import ToolContext
    from plugin.writer.get_image import GetImage

    tctx = ToolContext(doc, ctx, doc_type, {}, "test")
    return GetImage().execute(tctx, **kwargs)


def _assert_png_result(res, source):
    assert res.get("status") == "ok", res
    assert res.get("source") == source
    blob = res.get("_mcp_image") or {}
    raw = base64.b64decode(blob.get("data") or "")
    assert raw[:8] == _PNG_MAGIC, "get_image must return a real PNG, not an empty placeholder"
    assert blob.get("mimeType") == "image/png"
    return raw


@native_test
@with_native_doc("draw")
def test_get_image_draw_page_png(ctx, doc):
    _two_pages(doc)
    page1 = _assert_png_result(_exec(doc, ctx, "draw", page=0), "page 0")
    page2 = _assert_png_result(_exec(doc, ctx, "draw", page=1), "page 1")
    assert page1 != page2, "page=0 and page=1 must render different Draw pages"

    missing = _exec(doc, ctx, "draw", page=9)
    assert missing.get("status") == "error"
    assert "page not found" in missing.get("message", "")
    assert "2 page(s)" in missing.get("message", "")

    unnamed = _exec(doc, ctx, "draw", image="NoSuchGraphic")
    assert unnamed.get("status") == "error"
    assert "NoSuchGraphic" in unnamed.get("message", "")

    no_sel = _exec(doc, ctx, "draw", selection=True)
    assert no_sel.get("status") == "error"
    assert "No image selected" in no_sel.get("message", "")


@native_test
@with_native_doc("impress")
def test_get_image_impress_page_png(ctx, doc):
    _two_pages(doc)
    page1 = _assert_png_result(_exec(doc, ctx, "impress", page=0), "page 0")
    page2 = _assert_png_result(_exec(doc, ctx, "impress", page=1), "page 1")
    assert page1 != page2, "page=0 and page=1 must render different Impress slides"
