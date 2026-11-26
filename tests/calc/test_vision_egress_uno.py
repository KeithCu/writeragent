# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests: Calc vision egress keeps an unnamed image across selection changes."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from com.sun.star.awt import Size

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import unique_png_bytes, with_native_doc
from plugin.vision.vision_runner import run_and_insert_vision_for_selection
from plugin.writer.images.image_tools import export_graphic_object_to_bytes, graphic_from_bytes


def _add_calc_image(ctx: Any, doc: Any, sheet: Any, col: int, row: int, rgb: tuple[int, int, int], *, name: str = "") -> Any:
    shape = doc.createInstance("com.sun.star.drawing.GraphicObjectShape")
    shape.setPropertyValue("Graphic", graphic_from_bytes(ctx, unique_png_bytes(*rgb)))
    shape.setSize(Size(2000, 2000))
    sheet.getDrawPage().add(shape)
    shape.setPropertyValue("Anchor", sheet.getCellByPosition(col, row))
    shape.setName(name)
    return shape


@native_test
@with_native_doc("calc", reuse=False)
def test_calc_unnamed_image_keeps_discovered_shape_across_selection_change_uno(ctx, doc):
    """Switching selection during OCR must still export and insert under the first unnamed image."""
    sheet = doc.getSheets().getByIndex(0)
    shape_a = _add_calc_image(ctx, doc, sheet, 0, 0, (10, 20, 30))
    shape_b = _add_calc_image(ctx, doc, sheet, 5, 20, (200, 100, 50))
    assert shape_a.getName() == ""
    assert shape_b.getName() == ""

    expected_bytes_a = export_graphic_object_to_bytes(ctx, shape_a)
    expected_bytes_b = export_graphic_object_to_bytes(ctx, shape_b)
    assert expected_bytes_a != expected_bytes_b

    controller = doc.getCurrentController()
    controller.select(shape_a)

    captured_bytes: list[bytes] = []

    def fake_run_vision(_ctx: Any, _spec: dict[str, Any], png_bytes: bytes, context: dict[str, Any] | None = None, cancel: Any = None):
        del context, cancel
        captured_bytes.append(png_bytes)
        controller.select(shape_b)
        return {
            "status": "ok",
            "helper": "extract_structure",
            "full_text": "hello",
            "blocks": [{"type": "text", "text": "hello", "box": [0, 0, 10, 10]}],
            "tables": [],
            "html": "<p>hello</p>",
        }

    with patch("plugin.vision.vision_runner.run_vision", side_effect=fake_run_vision):
        result = run_and_insert_vision_for_selection(
            ctx,
            doc,
            helper="extract_structure",
            params={"insert_mode": "structured", "delete_image_after_insert": False},
            insert_into_document=True,
        )

    assert result.get("status") == "ok", result
    assert captured_bytes == [expected_bytes_a]
    assert sheet.getCellByPosition(0, 1).getString() == "extract_structure"
    assert sheet.getCellByPosition(0, 2).getString() == "hello"
    assert sheet.getCellByPosition(5, 21).getString() == ""


@native_test
@with_native_doc("calc", reuse=False)
def test_calc_duplicate_name_across_sheets_inserts_and_deletes_selected_with_one_undo_uno(ctx, doc):
    """A duplicate-named Calc image on sheet 2 inserts below itself, deletes itself, and undoes in one step."""
    sheets = doc.getSheets()
    sheets.insertNewByName("Sheet2", 1)
    sheet0 = sheets.getByIndex(0)
    sheet1 = sheets.getByIndex(1)

    _add_calc_image(ctx, doc, sheet0, 0, 0, (10, 20, 30), name="Pic")
    shape1 = _add_calc_image(ctx, doc, sheet1, 0, 0, (200, 100, 50), name="Pic")
    expected_bytes_1 = export_graphic_object_to_bytes(ctx, shape1)

    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet1)
    controller.select(shape1)

    captured_bytes: list[bytes] = []

    def fake_run_vision(_ctx: Any, _spec: dict[str, Any], png_bytes: bytes, context: dict[str, Any] | None = None, cancel: Any = None):
        del context, cancel
        captured_bytes.append(png_bytes)
        return {
            "status": "ok",
            "helper": "extract_structure",
            "full_text": "hello",
            "blocks": [{"type": "text", "text": "hello", "box": [0, 0, 10, 10]}],
            "tables": [],
            "html": "<p>hello</p>",
        }

    with patch("plugin.vision.vision_runner.run_vision", side_effect=fake_run_vision):
        result = run_and_insert_vision_for_selection(
            ctx,
            doc,
            helper="extract_structure",
            params={"insert_mode": "structured", "delete_image_after_insert": True},
            insert_into_document=True,
        )

    assert result.get("status") == "ok", result
    assert result.get("deleted_images") == ["Pic"]
    assert captured_bytes == [expected_bytes_1]
    assert sheet1.getCellByPosition(0, 1).getString() == "extract_structure"
    assert sheet1.getCellByPosition(0, 2).getString() == "hello"
    assert sheet1.getDrawPage().getCount() == 0
    assert sheet0.getDrawPage().getCount() == 1
    assert sheet0.getCellByPosition(0, 1).getString() == ""

    doc.getUndoManager().undo()
    assert sheet1.getDrawPage().getCount() == 1
    assert export_graphic_object_to_bytes(ctx, sheet1.getDrawPage().getByIndex(0)) == expected_bytes_1
    assert sheet1.getCellByPosition(0, 1).getString() == ""
    assert sheet1.getCellByPosition(0, 2).getString() == ""


@native_test
@with_native_doc("calc", reuse=False)
def test_calc_unnamed_image_deletes_discovered_shape_after_selection_change_uno(ctx, doc):
    """Delete image after OCR removes the discovered unnamed shape even when selection moves during OCR."""
    sheet = doc.getSheets().getByIndex(0)
    shape_a = _add_calc_image(ctx, doc, sheet, 0, 0, (10, 20, 30))
    shape_b = _add_calc_image(ctx, doc, sheet, 5, 20, (200, 100, 50))

    controller = doc.getCurrentController()
    controller.select(shape_a)

    def fake_run_vision(_ctx: Any, _spec: dict[str, Any], _png_bytes: bytes, context: dict[str, Any] | None = None, cancel: Any = None):
        del context, cancel
        controller.select(shape_b)
        return {
            "status": "ok",
            "helper": "extract_structure",
            "full_text": "hello",
            "blocks": [{"type": "text", "text": "hello", "box": [0, 0, 10, 10]}],
            "tables": [],
            "html": "<p>hello</p>",
        }

    with patch("plugin.vision.vision_runner.run_vision", side_effect=fake_run_vision):
        result = run_and_insert_vision_for_selection(
            ctx,
            doc,
            helper="extract_structure",
            params={"insert_mode": "structured", "delete_image_after_insert": True},
            insert_into_document=True,
        )

    assert result.get("status") == "ok", result
    assert result.get("deleted_images") == [""]
    assert sheet.getDrawPage().getCount() == 1
    assert sheet.getDrawPage().getByIndex(0) == shape_b
    assert sheet.getCellByPosition(0, 1).getString() == "extract_structure"
    assert sheet.getCellByPosition(0, 2).getString() == "hello"
    assert sheet.getCellByPosition(5, 21).getString() == ""
