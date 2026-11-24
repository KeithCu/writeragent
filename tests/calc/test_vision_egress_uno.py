# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests: Calc vision egress keeps an unnamed image across selection changes."""

from __future__ import annotations

import struct
import zlib
from typing import Any
from unittest.mock import patch

from com.sun.star.awt import Size

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.vision.vision_runner import run_and_insert_vision_for_selection
from plugin.writer.images.image_tools import export_graphic_object_to_bytes, graphic_from_bytes


def _make_unique_png_bytes(r: int, g: int, b: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = b"\x00" + bytes([r, g, b])
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


def _add_unnamed_calc_image(ctx: Any, doc: Any, sheet: Any, col: int, row: int, rgb: tuple[int, int, int]) -> Any:
    shape = doc.createInstance("com.sun.star.drawing.GraphicObjectShape")
    shape.setPropertyValue("Graphic", graphic_from_bytes(ctx, _make_unique_png_bytes(*rgb)))
    shape.setSize(Size(2000, 2000))
    sheet.getDrawPage().add(shape)
    shape.setPropertyValue("Anchor", sheet.getCellByPosition(col, row))
    shape.setName("")
    return shape


@native_test
@with_native_doc("calc", reuse=False)
def test_calc_unnamed_image_keeps_discovered_shape_across_selection_change_uno(ctx, doc):
    """Switching selection during OCR must still export and insert under the first unnamed image."""
    sheet = doc.getSheets().getByIndex(0)
    shape_a = _add_unnamed_calc_image(ctx, doc, sheet, 0, 0, (10, 20, 30))
    shape_b = _add_unnamed_calc_image(ctx, doc, sheet, 5, 20, (200, 100, 50))
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
