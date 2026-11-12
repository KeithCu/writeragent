# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO tests for visual helpers."""

from __future__ import annotations

from typing import Any

from com.sun.star.awt.FontSlant import ITALIC, NONE, OBLIQUE

from plugin.doc.visual_helpers import (
    DRAW_GRAPHIC_SERVICE,
    apply_character_properties,
    get_active_draw_page,
    list_graphic_objects,
    remove_graphic_from_draw_pages,
    selected_graphic_object,
)
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


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

