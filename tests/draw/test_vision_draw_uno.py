# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests: Draw/Impress vision on unnamed selected graphics."""

from __future__ import annotations

import os
import struct
import tempfile
import zlib
from typing import Any
from unittest.mock import patch

from plugin.doc.visual_helpers import graphic_objects_in_selection
from plugin.framework.errors import DocumentDisposedError
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import (
    _windows_should_skip_draw_family_close,
    close_draw_family_doc,
    settle_after_draw_family_close,
    with_native_doc,
)
from plugin.vision.vision_runner import run_and_insert_vision_for_selection
from plugin.writer.images.image_tools import insert_image_at_locator


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


def _write_temp_png(r: int, g: int, b: int) -> str:
    fd, path = tempfile.mkstemp(suffix=".png", prefix="wa_draw_vision_")
    os.close(fd)
    with open(path, "wb") as handle:
        handle.write(_make_unique_png_bytes(r, g, b))
    return path


def _select_shape(doc: Any, shape: Any) -> None:
    controller = doc.getCurrentController()
    controller.select(shape)


@native_test
@with_native_doc("draw", reuse=False)
def test_draw_vision_unnamed_selected_graphic(ctx, doc):
    """Draw pasted pictures often have empty Name; vision must still OCR the selection."""
    path = _write_temp_png(10, 20, 30)
    try:
        graphic = insert_image_at_locator(ctx, doc, path, page_index=0, x_mm=20, y_mm=20, width_mm=30, height_mm=30)
        assert graphic is not None
        _select_shape(doc, graphic)

        pairs = graphic_objects_in_selection(doc)
        assert len(pairs) == 1
        assert pairs[0][1] is not None

        captured: list[tuple[str, bytes]] = []

        def fake_run_vision(_ctx: Any, spec: dict[str, Any], png_bytes: bytes, context: dict[str, Any] | None = None, cancel: Any = None):
            del cancel, context, spec
            captured.append(("", png_bytes))
            return {"status": "ok", "helper": "extract_text", "full_text": "draw ocr", "html": "<p>draw ocr</p>"}

        with patch("plugin.vision.vision_runner.run_vision", side_effect=fake_run_vision):
            result = run_and_insert_vision_for_selection(
                ctx,
                doc,
                helper="extract_text",
                insert_into_document=False,
            )
        assert result.get("status") == "ok", result
        assert captured, "run_vision was not called"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@native_test
@with_native_doc("draw", reuse=False)
def test_draw_vision_unnamed_keeps_discovered_shape_across_selection_change_uno(ctx, doc):
    """Draw exports the discovered unnamed shape even when selection changes before export."""
    import plugin.vision.vision_runner as vr

    path_a = _write_temp_png(10, 20, 30)
    path_b = _write_temp_png(200, 100, 50)
    try:
        shape_a = insert_image_at_locator(ctx, doc, path_a, page_index=0, x_mm=10, y_mm=10, width_mm=20, height_mm=20)
        shape_b = insert_image_at_locator(ctx, doc, path_b, page_index=0, x_mm=50, y_mm=10, width_mm=20, height_mm=20)
        assert shape_a is not None and shape_b is not None
        shape_a.setName("")
        shape_b.setName("")
        _select_shape(doc, shape_a)

        real_export = vr.export_graphic_object_to_bytes
        exported_objs: list[Any] = []

        def wrapped_export(c: Any, obj: Any) -> bytes:
            exported_objs.append(obj)
            return real_export(c, obj)

        def fake_run_vision(_ctx: Any, _spec: dict[str, Any], _png: bytes, context: dict[str, Any] | None = None, cancel: Any = None):
            del context, cancel
            return {"status": "ok", "helper": "extract_text", "full_text": "draw ocr", "html": "<p>draw ocr</p>"}

        with (
            patch("plugin.vision.vision_runner.export_graphic_object_to_bytes", side_effect=wrapped_export),
            patch("plugin.vision.vision_runner.run_vision", side_effect=fake_run_vision),
        ):
            result = run_and_insert_vision_for_selection(
                ctx,
                doc,
                helper="extract_text",
                insert_into_document=False,
                on_discovered=lambda _discovered: _select_shape(doc, shape_b),
            )

        assert result.get("status") == "ok", result
        assert exported_objs == [shape_a]
    finally:
        for path in (path_a, path_b):
            try:
                os.remove(path)
            except OSError:
                pass


@native_test
@with_native_doc("draw", reuse=False)
def test_draw_vision_named_kept_shape_closed_doc_raises_disposed_uno(ctx, doc):
    """Closing a Draw document after discovering a named kept shape raises DocumentDisposedError on export."""
    if _windows_should_skip_draw_family_close():
        return
    path = _write_temp_png(10, 20, 30)
    closed = False
    try:
        shape = insert_image_at_locator(ctx, doc, path, page_index=0, x_mm=10, y_mm=10, width_mm=20, height_mm=20)
        assert shape is not None
        shape.setName("Pic1")
        _select_shape(doc, shape)
        del shape

        def close_on_discovered(_discovered: Any) -> None:
            nonlocal closed
            closed = True
            close_draw_family_doc(doc)

        with patch("plugin.vision.vision_runner.run_vision") as mock_run_vision:
            try:
                run_and_insert_vision_for_selection(
                    ctx,
                    doc,
                    helper="extract_text",
                    insert_into_document=False,
                    on_discovered=close_on_discovered,
                )
                raise AssertionError("Expected DocumentDisposedError")
            except DocumentDisposedError:
                pass
        mock_run_vision.assert_not_called()
    finally:
        if closed:
            settle_after_draw_family_close()
        try:
            os.remove(path)
        except OSError:
            pass
