# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live UNO tests for plugin/writer/watermark.py."""

from __future__ import annotations

import os
import tempfile
import zipfile
from typing import Any

from plugin.framework.uno_context import create_property_value
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.search import iter_draw_shapes
from plugin.writer.watermark import PageGetWatermark, PageSetWatermark


def _shape_custom_type(shape: Any) -> str:
    for pv in shape.getPropertyValue("CustomShapeGeometry"):
        if pv.Name == "Type":
            return str(pv.Value)
    return ""


@native_test
@with_native_doc("writer", reuse=False)
def test_set_watermark_stores_dialog_shape_uno(ctx: Any, doc: Any) -> None:
    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx)
    res = PageSetWatermark().execute(tool_ctx, text="DRAFT")
    assert res["status"] == "ok", res

    wm_shapes = [
        s
        for s in iter_draw_shapes(doc.getDrawPage())
        if s.getName().startswith("PowerPlusWaterMarkObject")
    ]
    assert wm_shapes
    for shape in wm_shapes:
        assert _shape_custom_type(shape) == "fontwork-plain-text"
        assert str(shape.getPropertyValue("TextWrap").value) == "THROUGH"
        assert shape.getPropertyValue("Opaque") is False
        assert shape.getPropertyValue("FillColor") == 0xC0C0C0
        assert shape.getPropertyValue("FillTransparence") == 50
        assert shape.getPropertyValue("RotateAngle") == 4500
        assert shape.getPropertyValue("CharFontName") == "Liberation Sans"
        assert shape.getString() == "DRAFT"
        assert "SwXHeadFootText" in shape.getAnchor().getText().getImplementationName()

    standard = doc.getStyleFamilies().getByName("PageStyles").getByName("Standard")
    assert standard.getPropertyValue("HeaderIsOn") is True

    got = PageGetWatermark().execute(tool_ctx)
    assert got["status"] == "ok", got
    assert got["watermark"] == {
        "text": "DRAFT",
        "font": "Liberation Sans",
        "angle": 45,
        "transparency": 50,
        "color": "#c0c0c0",
    }


@native_test
@with_native_doc("writer", reuse=False)
def test_watermark_docx_and_odt_round_trip_uno(ctx: Any, doc: Any) -> None:
    import uno

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx)
    res = PageSetWatermark().execute(
        tool_ctx,
        text="CONFIDENTIAL",
        angle=0,
        color="#ff0000",
        transparency=50,
        font="DejaVu Sans",
    )
    assert res["status"] == "ok", res

    desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    fd_docx, docx_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd_docx)
    fd_odt, odt_path = tempfile.mkstemp(suffix=".odt")
    os.close(fd_odt)

    reloaded_docx = None
    reloaded_odt = None
    try:
        docx_url = uno.systemPathToFileUrl(docx_path)
        doc.storeToURL(docx_url, (create_property_value("FilterName", "MS Word 2007 XML"),))

        with zipfile.ZipFile(docx_path, "r") as zf:
            header_names = [n for n in zf.namelist() if n.startswith("word/header") and n.endswith(".xml")]
            assert any('id="PowerPlusWaterMarkObject' in zf.read(n).decode("utf-8", errors="ignore") for n in header_names)

        reloaded_docx = desktop.loadComponentFromURL(
            docx_url, "_blank", 0, (create_property_value("Hidden", True),)
        )
        got_docx = PageGetWatermark().execute(TestingFactory.create_context(doc=reloaded_docx, ctx=ctx))
        assert got_docx["status"] == "ok", got_docx
        assert got_docx["watermark"] == {
            "text": "CONFIDENTIAL",
            "font": "DejaVu Sans",
            "angle": 0,
            "transparency": 50,
            "color": "#ff0000",
        }

        odt_url = uno.systemPathToFileUrl(odt_path)
        doc.storeToURL(odt_url, ())
        reloaded_odt = desktop.loadComponentFromURL(
            odt_url, "_blank", 0, (create_property_value("Hidden", True),)
        )
        got_odt = PageGetWatermark().execute(TestingFactory.create_context(doc=reloaded_odt, ctx=ctx))
        assert got_odt["status"] == "ok", got_odt
        assert got_odt["watermark"] == {
            "text": "CONFIDENTIAL",
            "font": "DejaVu Sans",
            "angle": 0,
            "transparency": 50,
            "color": "#ff0000",
        }
    finally:
        for reloaded in (reloaded_docx, reloaded_odt):
            if reloaded is not None:
                reloaded.close(True)
        for path in (docx_path, odt_path):
            if os.path.exists(path):
                os.unlink(path)


@native_test
@with_native_doc("writer", reuse=False)
def test_picture_watermark_kept_by_get_removed_by_set_uno(ctx: Any, doc: Any) -> None:
    standard = doc.getStyleFamilies().getByName("PageStyles").getByName("Standard")
    standard.setPropertyValue("HeaderIsOn", True)
    header_text = standard.getPropertyValue("HeaderText")

    pic = doc.createInstance("com.sun.star.drawing.GraphicObjectShape")
    header_text.insertTextContent(header_text.getStart(), pic, False)
    pic.setName("WordPictureWatermark123")

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx)
    got = PageGetWatermark().execute(tool_ctx)
    assert got["status"] == "ok", got
    assert got["picture_watermark"] is True
    assert any(
        s.getName() == "WordPictureWatermark123" for s in iter_draw_shapes(doc.getDrawPage())
    )

    res = PageSetWatermark().execute(tool_ctx, text="DRAFT")
    assert res["status"] == "ok", res
    assert res["picture_watermarks_removed"] == 1
    assert res["picture_watermark"] is False
    assert not any(
        s.getName().startswith("WordPictureWatermark") for s in iter_draw_shapes(doc.getDrawPage())
    )

    rem = PageSetWatermark().execute(tool_ctx, text="")
    assert rem["status"] == "ok", rem
    assert rem["watermark"] is None
    assert not any(
        s.getName().startswith("PowerPlusWaterMarkObject") for s in iter_draw_shapes(doc.getDrawPage())
    )
