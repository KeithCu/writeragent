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
@with_native_doc("calc")
def test_calc_list_active_page_and_remove_uno(ctx: Any, doc: Any) -> None:
    controller = doc.getCurrentController()
    sheet = controller.getActiveSheet()
    page = sheet.getDrawPage()
    shape = doc.createInstance(DRAW_GRAPHIC_SERVICE)
    page.add(shape)
    shape.setName("Logo")

    items = list_graphic_objects(doc)
    assert len(items) == 1
    assert items[0][0] == "Logo"
    assert getattr(items[0], "sheet_index", None) == 0
    assert getattr(items[0], "sheet_name", None) == sheet.getName()
    assert get_active_draw_page(doc) == page

    assert remove_graphic_from_draw_pages(doc, page.getByIndex(0)) is True
    assert page.getCount() == 0


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

