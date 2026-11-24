# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live UNO tests for plugin/writer/watermark.py."""

from __future__ import annotations

import os
import re
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

        rem = PageSetWatermark().execute(tool_ctx, text="")
        assert rem["status"] == "ok", rem
        assert rem["watermark"] is None
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


@native_test
@with_native_doc("writer", reuse=False)
def test_remove_watermark_clears_inactive_first_and_even_headers_uno(ctx: Any, doc: Any) -> None:
    import uno

    standard = doc.getStyleFamilies().getByName("PageStyles").getByName("Standard")
    standard.setPropertyValue("HeaderIsOn", True)
    standard.setPropertyValue("HeaderIsShared", False)
    standard.setPropertyValue("FirstIsShared", False)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx)
    res = PageSetWatermark().execute(tool_ctx, text="DRAFT")
    assert res["status"] == "ok", res

    desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    fd_src, src_docx = tempfile.mkstemp(suffix=".docx")
    os.close(fd_src)
    fd_mod, mod_docx = tempfile.mkstemp(suffix=".docx")
    os.close(fd_mod)
    fd_out, out_docx = tempfile.mkstemp(suffix=".docx")
    os.close(fd_out)

    reloaded = None
    try:
        doc.storeToURL(
            uno.systemPathToFileUrl(src_docx),
            (create_property_value("FilterName", "MS Word 2007 XML"),),
        )

        with zipfile.ZipFile(src_docx, "r") as zin, zipfile.ZipFile(mod_docx, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "word/document.xml":
                    data = re.sub(rb"<w:titlePg[^>]*/>", b"", data)
                elif item.filename == "word/settings.xml":
                    data = re.sub(rb"<w:evenAndOddHeaders[^>]*/>", b"", data)
                zout.writestr(item, data)

        reloaded = desktop.loadComponentFromURL(
            uno.systemPathToFileUrl(mod_docx),
            "_blank",
            0,
            (create_property_value("Hidden", True),),
        )
        reloaded_style = reloaded.getStyleFamilies().getByName("PageStyles").getByName("Standard")
        assert reloaded_style.getPropertyValue("FirstIsShared") is True
        assert reloaded_style.getPropertyValue("HeaderIsShared") is True
        initial_wm = [
            s.getName()
            for s in iter_draw_shapes(reloaded.getDrawPage())
            if s.getName().startswith("PowerPlusWaterMarkObject")
        ]
        assert len(initial_wm) > 1, initial_wm

        reloaded_ctx = TestingFactory.create_context(doc=reloaded, ctx=ctx)
        rem = PageSetWatermark().execute(reloaded_ctx, text="")
        assert rem["status"] == "ok", rem
        assert rem["watermark"] is None

        remaining_wm = [
            s.getName()
            for s in iter_draw_shapes(reloaded.getDrawPage())
            if s.getName().startswith("PowerPlusWaterMarkObject")
        ]
        assert remaining_wm == [], remaining_wm

        reloaded.storeToURL(
            uno.systemPathToFileUrl(out_docx),
            (create_property_value("FilterName", "MS Word 2007 XML"),),
        )
        with zipfile.ZipFile(out_docx, "r") as zf:
            header_names = [n for n in zf.namelist() if n.startswith("word/header") and n.endswith(".xml")]
            for name in header_names:
                assert b'id="PowerPlusWaterMarkObject' not in zf.read(name), name

        reloaded_style.setPropertyValue("FirstIsShared", False)
        reloaded_style.setPropertyValue("HeaderIsShared", False)
        unshared_wm = [
            s.getName()
            for s in iter_draw_shapes(reloaded.getDrawPage())
            if s.getName().startswith("PowerPlusWaterMarkObject")
        ]
        assert unshared_wm == [], unshared_wm
        got = PageGetWatermark().execute(reloaded_ctx)
        assert got["status"] == "ok", got
        assert got["watermark"] is None
    finally:
        if reloaded is not None:
            reloaded.close(True)
        for path in (src_docx, mod_docx, out_docx):
            if os.path.exists(path):
                os.unlink(path)


@native_test
@with_native_doc("writer", reuse=False)
def test_set_watermark_updates_inactive_first_and_even_headers_uno(ctx: Any, doc: Any) -> None:
    import uno
    from com.sun.star.style.BreakType import PAGE_BEFORE

    standard = doc.getStyleFamilies().getByName("PageStyles").getByName("Standard")
    standard.setPropertyValue("HeaderIsOn", True)
    standard.setPropertyValue("HeaderIsShared", False)
    standard.setPropertyValue("FirstIsShared", False)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx)
    first = PageSetWatermark().execute(tool_ctx, text="DRAFT")
    assert first["status"] == "ok", first

    standard.setPropertyValue("HeaderIsShared", True)
    standard.setPropertyValue("FirstIsShared", True)

    inactive = [
        s
        for s in iter_draw_shapes(doc.getDrawPage())
        if s.getName().startswith("PowerPlusWaterMarkObject")
        and s.getAnchor() is not None
        and s.getAnchor().getText() is None
    ]
    assert len(inactive) == 2

    text_body = doc.getText()
    for _idx in range(2):
        text_body.insertControlCharacter(text_body.getEnd(), 0, False)
        text_body.getEnd().setPropertyValue("BreakType", PAGE_BEFORE)

    doc.getUndoManager().clear()
    res = PageSetWatermark().execute(
        tool_ctx,
        text="FINAL",
        font="DejaVu Serif",
        angle=30,
        color="#ff0000",
        transparency=30,
    )
    assert res["status"] == "ok", res
    assert doc.getUndoManager().getAllUndoActionTitles() == ()

    fd_docx, docx_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd_docx)
    try:
        doc.storeToURL(
            uno.systemPathToFileUrl(docx_path),
            (create_property_value("FilterName", "MS Word 2007 XML"),),
        )
        with zipfile.ZipFile(docx_path, "r") as zf:
            header_names = sorted(
                n for n in zf.namelist() if n.startswith("word/header") and n.endswith(".xml")
            )
            assert len(header_names) == 3, header_names
            for name in header_names:
                xml = zf.read(name).decode("utf-8", errors="ignore")
                assert 'string="FINAL"' in xml, name
                assert "rotation:330" in xml, name
                assert 'fillcolor="red"' in xml, name
                assert "DejaVu Serif" in xml, name
                assert 'string="DRAFT"' not in xml, name

        standard.setPropertyValue("HeaderIsShared", False)
        standard.setPropertyValue("FirstIsShared", False)
        doc.getCurrentController().getViewCursor().gotoEnd(False)

        wm_shapes = [
            s
            for s in iter_draw_shapes(doc.getDrawPage())
            if s.getName().startswith("PowerPlusWaterMarkObject")
        ]
        by_header: dict[str, Any] = {}
        for prop_name in ("HeaderText", "HeaderTextFirst", "HeaderTextLeft"):
            hdr_text = standard.getPropertyValue(prop_name)
            matches = [
                s
                for s in wm_shapes
                if s.getAnchor() is not None and s.getAnchor().getText() == hdr_text
            ]
            assert len(matches) == 1, (prop_name, matches)
            shape = matches[0]
            assert shape.getString() == "FINAL", prop_name
            assert shape.getPropertyValue("RotateAngle") == 3000, prop_name
            assert shape.getPropertyValue("FillColor") == 0xFF0000, prop_name
            assert shape.getPropertyValue("FillTransparence") == 30, prop_name
            by_header[prop_name] = shape.getPropertyValue("BoundRect")

        base_rect = by_header["HeaderText"]
        assert base_rect.Width > 0 and base_rect.Height > 0
        for prop_name in ("HeaderTextFirst", "HeaderTextLeft"):
            rect = by_header[prop_name]
            assert abs(rect.Width - base_rect.Width) <= 5, (prop_name, rect.Width, base_rect.Width)
            assert abs(rect.Height - base_rect.Height) <= 5, (prop_name, rect.Height, base_rect.Height)
    finally:
        if os.path.exists(docx_path):
            os.unlink(docx_path)


def _make_inactive_watermarks(doc: Any, ctx: Any) -> tuple[Any, Any]:
    standard = doc.getStyleFamilies().getByName("PageStyles").getByName("Standard")
    standard.setPropertyValue("HeaderIsOn", True)
    standard.setPropertyValue("HeaderIsShared", False)
    standard.setPropertyValue("FirstIsShared", False)

    tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx)
    res = PageSetWatermark().execute(tool_ctx, text="DRAFT")
    assert res["status"] == "ok", res

    standard.setPropertyValue("HeaderIsShared", True)
    standard.setPropertyValue("FirstIsShared", True)
    assert any(
        s.getName().startswith("PowerPlusWaterMarkObject")
        and s.getAnchor() is not None
        and s.getAnchor().getText() is None
        for s in iter_draw_shapes(doc.getDrawPage())
    )
    return standard, tool_ctx


@native_test
@with_native_doc("writer", reuse=False)
def test_remove_watermark_undo_brings_back_no_copy_uno(ctx: Any, doc: Any) -> None:
    standard, tool_ctx = _make_inactive_watermarks(doc, ctx)
    text_body = doc.getText()
    text_body.insertString(text_body.getEnd(), "hello", False)

    rem = PageSetWatermark().execute(tool_ctx, text="")
    assert rem["status"] == "ok", rem

    undo_mgr = doc.getUndoManager()
    assert undo_mgr.isUndoPossible() is False
    assert undo_mgr.isRedoPossible() is False

    standard.setPropertyValue("HeaderIsShared", False)
    standard.setPropertyValue("FirstIsShared", False)
    wm_shapes = [
        s.getName()
        for s in iter_draw_shapes(doc.getDrawPage())
        if s.getName().startswith("PowerPlusWaterMarkObject")
    ]
    assert wm_shapes == [], wm_shapes
    got = PageGetWatermark().execute(tool_ctx)
    assert got["status"] == "ok", got
    assert got["watermark"] is None


@native_test
@with_native_doc("writer", reuse=False)
def test_remove_watermark_with_all_headers_off_clears_undo_uno(ctx: Any, doc: Any) -> None:
    import uno

    _, tool_ctx = _make_inactive_watermarks(doc, ctx)
    page_styles = doc.getStyleFamilies().getByName("PageStyles")
    for style_name in page_styles.getElementNames():
        page_styles.getByName(style_name).setPropertyValue("HeaderIsOn", False)

    shape = doc.createInstance("com.sun.star.drawing.RectangleShape")
    size = uno.createUnoStruct("com.sun.star.awt.Size")
    size.Width = 1000
    size.Height = 1000
    shape.setSize(size)
    doc.getDrawPage().add(shape)
    shape.setName("PowerPlusWaterMarkObjectBody")

    undo_mgr = doc.getUndoManager()
    assert undo_mgr.isUndoPossible() is True

    rem = PageSetWatermark().execute(tool_ctx, text="")
    assert rem["status"] == "ok", rem
    assert undo_mgr.getAllUndoActionTitles() == ()
    assert not any(
        s.getName().startswith("PowerPlusWaterMarkObject") for s in iter_draw_shapes(doc.getDrawPage())
    )

    text_body = doc.getText()
    text_body.insertString(text_body.getEnd(), "after", False)
    assert text_body.getString().endswith("after")
    undo_mgr.undo()
    assert not text_body.getString().endswith("after")


@native_test
@with_native_doc("writer", reuse=False)
def test_remove_watermark_inside_open_undo_context_uno(ctx: Any, doc: Any) -> None:
    _, tool_ctx = _make_inactive_watermarks(doc, ctx)
    undo_mgr = doc.getUndoManager()
    undo_mgr.enterUndoContext("outer")
    try:
        text_body = doc.getText()
        text_body.insertString(text_body.getEnd(), "inside", False)
        rem = PageSetWatermark().execute(tool_ctx, text="")
        assert rem["status"] == "ok", rem
    finally:
        undo_mgr.leaveUndoContext()

    assert undo_mgr.isUndoPossible() is False
    assert not any(
        s.getName().startswith("PowerPlusWaterMarkObject") for s in iter_draw_shapes(doc.getDrawPage())
    )


@native_test
@with_native_doc("writer", reuse=False)
def test_remove_watermark_keeps_picture_removal_undoable_uno(ctx: Any, doc: Any) -> None:
    standard, tool_ctx = _make_inactive_watermarks(doc, ctx)
    header_text = standard.getPropertyValue("HeaderText")
    pic = doc.createInstance("com.sun.star.drawing.GraphicObjectShape")
    header_text.insertTextContent(header_text.getStart(), pic, False)
    pic.setName("WordPictureWatermark1")

    rem = PageSetWatermark().execute(tool_ctx, text="")
    assert rem["status"] == "ok", rem
    assert rem["picture_watermarks_removed"] == 1
    assert "WordPictureWatermark1" not in [s.getName() for s in iter_draw_shapes(doc.getDrawPage())]

    undo_mgr = doc.getUndoManager()
    while undo_mgr.isUndoPossible():
        undo_mgr.undo()

    names = [s.getName() for s in iter_draw_shapes(doc.getDrawPage())]
    assert names == ["WordPictureWatermark1"]
