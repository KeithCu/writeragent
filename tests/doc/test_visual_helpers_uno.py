# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO tests for visual helpers."""

from __future__ import annotations

from typing import Any

from com.sun.star.awt.FontSlant import ITALIC, NONE, OBLIQUE
from com.sun.star.text.TextContentAnchorType import (
    AS_CHARACTER,
    AT_CHARACTER,
    AT_PARAGRAPH,
)

from plugin.doc.visual_helpers import (
    DRAW_GRAPHIC_SERVICE,
    apply_character_properties,
    delete_graphic,
    get_active_draw_page,
    list_graphic_objects,
    remove_graphic_from_draw_pages,
    selected_graphic_object,
)
from plugin.framework.errors import ToolExecutionError
from plugin.framework.uno_context import enum_has_more
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.writer.images.image_tools import insert_image_at_locator


@native_test
@with_native_doc("writer")
def test_apply_character_properties_italic_uses_font_slant_italic(ctx, doc):
    text = doc.getText()
    cursor = text.createTextCursor()
    text.insertString(cursor, "Hello", False)
    cursor.gotoStart(False)
    cursor.gotoEnd(True)

    results = apply_character_properties(cursor, italic=True)
    assert results.get("CharPosture") is True
    posture = cursor.getPropertyValue("CharPosture")
    assert posture == ITALIC
    assert posture != OBLIQUE

    results = apply_character_properties(cursor, italic=False)
    assert results.get("CharPosture") is True
    assert cursor.getPropertyValue("CharPosture") == NONE


@native_test
@with_native_doc("calc", reuse=False)
def test_calc_list_active_page_and_remove_uno(ctx: Any, doc: Any) -> None:
    controller = doc.getCurrentController()
    sheet = controller.getActiveSheet()
    page = sheet.getDrawPage()
    count_before = page.getCount()
    shape = doc.createInstance(DRAW_GRAPHIC_SERVICE)
    page.add(shape)
    try:
        shape.setName("Logo")

        items = list_graphic_objects(doc)
        assert any(
            item[0] == "Logo"
            and getattr(item, "sheet_index", None) == 0
            and getattr(item, "sheet_name", None) == sheet.getName()
            for item in items
        )
        assert get_active_draw_page(doc) == page

        # page.getByIndex returns a fresh wrapper distinct in Python identity from shape;
        # remove_graphic_from_draw_pages matches it via UNO identity (PyUNO_cmp).
        assert remove_graphic_from_draw_pages(doc, page.getByIndex(page.getCount() - 1)) is True
        assert page.getCount() == count_before
    finally:
        if page.getCount() > count_before:
            page.remove(shape)


@native_test
@with_native_doc("draw", reuse=False)
def test_draw_current_page_list_select_remove_uno(ctx: Any, doc: Any) -> None:
    controller = doc.getCurrentController()
    page = controller.getCurrentPage()
    shape = doc.createInstance(DRAW_GRAPHIC_SERVICE)
    page.add(shape)
    shape.setName("Logo")

    controller.select(shape)
    assert selected_graphic_object(doc) == shape
    assert [name for name, _shape in list_graphic_objects(doc)] == ["Logo"]
    assert get_active_draw_page(doc) == page

    count_before = page.getCount()
    assert remove_graphic_from_draw_pages(doc, page.getByIndex(count_before - 1)) is True
    assert page.getCount() == count_before - 1


@native_test
@with_native_doc("writer")
def test_writer_active_draw_page_is_document_draw_page_uno(ctx: Any, doc: Any) -> None:
    assert doc.getCurrentController() is not None
    assert get_active_draw_page(doc) == doc.getDrawPage()


def _logo_path() -> str:
    import os

    here = os.path.abspath(__file__)
    directory = os.path.dirname(here)
    for _unused in range(6):
        candidate = os.path.join(directory, "extension", "assets", "logo_32.png")
        if os.path.isfile(candidate):
            return candidate
        directory = os.path.dirname(directory)
    raise FileNotFoundError("extension/assets/logo_32.png not found")


def _dispatch(ctx: Any, doc: Any, cmd: str) -> None:
    helper = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    helper.executeDispatch(doc.getCurrentController().getFrame(), cmd, "", 0, ())


def _redline_types(doc: Any) -> list[str]:
    types: list[str] = []
    enum = doc.getRedlines().createEnumeration()
    while enum_has_more(enum):
        types.append(str(enum.nextElement().getPropertyValue("RedlineType")))
    return types


@native_test
@with_native_doc("writer", reuse=False)
def test_delete_graphic_tracked_anchor_types_reject_and_accept_uno(ctx: Any, doc: Any) -> None:
    logo = _logo_path()
    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Anchor text", False)

    for anchor_type in (AS_CHARACTER, AT_CHARACTER, AT_PARAGRAPH):
        graphic = insert_image_at_locator(ctx, doc, logo, width_mm=10, height_mm=10)
        graphic.setName(f"Pic_{anchor_type.value}")
        graphic.setPropertyValue("AnchorType", anchor_type)
        name = graphic.getName()

        doc.setPropertyValue("RecordChanges", True)
        try:
            assert delete_graphic(ctx, doc, graphic) is True
            assert _redline_types(doc) == ["Delete"]
            assert doc.getGraphicObjects().hasByName(name) is True

            _dispatch(ctx, doc, ".uno:RejectAllTrackedChanges")
            assert doc.getGraphicObjects().hasByName(name) is True

            target = doc.getGraphicObjects().getByName(name)
            assert delete_graphic(ctx, doc, target) is True
            _dispatch(ctx, doc, ".uno:AcceptAllTrackedChanges")
            assert doc.getGraphicObjects().hasByName(name) is False
        finally:
            doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_delete_graphic_adjacent_as_char_pictures_both_succeed_uno(ctx: Any, doc: Any) -> None:
    logo = _logo_path()
    g1 = insert_image_at_locator(ctx, doc, logo, width_mm=10, height_mm=10)
    g1.setName("Adj1")
    g2 = insert_image_at_locator(ctx, doc, logo, width_mm=10, height_mm=10)
    g2.setName("Adj2")
    g1.setPropertyValue("AnchorType", AS_CHARACTER)
    g2.setPropertyValue("AnchorType", AS_CHARACTER)

    doc.setPropertyValue("RecordChanges", True)
    try:
        assert delete_graphic(ctx, doc, g1) is True
        assert delete_graphic(ctx, doc, g2) is True
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_delete_graphic_preserves_view_selection_uno(ctx: Any, doc: Any) -> None:
    logo = _logo_path()
    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Keep this selected\n", False)
    graphic = insert_image_at_locator(ctx, doc, logo, width_mm=10, height_mm=10)
    graphic.setName("SelPic")

    sel_cur = text.createTextCursor()
    sel_cur.gotoStart(False)
    sel_cur.goRight(4, True)
    controller = doc.getCurrentController()
    controller.select(sel_cur)

    doc.setPropertyValue("RecordChanges", True)
    try:
        assert delete_graphic(ctx, doc, graphic) is True
        assert controller.getSelection().getByIndex(0).getString() == "Keep"
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_delete_graphic_untrackable_raises_and_keeps_image_uno(ctx: Any, doc: Any) -> None:
    logo = _logo_path()
    graphic = insert_image_at_locator(ctx, doc, logo, width_mm=10, height_mm=10)
    graphic.setName("ProtectedPic")
    graphic.setPropertyValue("ContentProtected", True)

    shape = doc.createInstance(DRAW_GRAPHIC_SERVICE)
    doc.getDrawPage().add(shape)
    shape.setName("DrawPic")

    doc.setPropertyValue("RecordChanges", True)
    try:
        try:
            delete_graphic(ctx, doc, graphic)
            raise AssertionError("Expected ToolExecutionError for ContentProtected picture")
        except ToolExecutionError as exc:
            assert exc.code == "IMAGE_DELETE_NOT_TRACKABLE"
        assert doc.getGraphicObjects().hasByName("ProtectedPic") is True

        try:
            delete_graphic(ctx, doc, shape)
            raise AssertionError("Expected ToolExecutionError for GraphicObjectShape")
        except ToolExecutionError as exc:
            assert exc.code == "IMAGE_DELETE_NOT_TRACKABLE"
        assert any(name == "DrawPic" for name, _obj in list_graphic_objects(doc))
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_delete_graphic_recording_off_removes_immediately_uno(ctx: Any, doc: Any) -> None:
    logo = _logo_path()
    graphic = insert_image_at_locator(ctx, doc, logo, width_mm=10, height_mm=10)
    graphic.setName("LivePic")
    name = graphic.getName()

    doc.setPropertyValue("RecordChanges", False)
    assert delete_graphic(ctx, doc, graphic) is False
    assert doc.getGraphicObjects().hasByName(name) is False

