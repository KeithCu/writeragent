# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests: image_mark.bake crops and renders highlights via hidden Draw export."""

from __future__ import annotations

import os
import struct
import tempfile
import zlib
from typing import Any

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.writer.images import image_mark
from plugin.writer.images.image_tools import insert_image_at_locator


def _create_white_png(width: int = 40, height: int = 40) -> bytes:
    """Build an in-memory uncompressed 24-bit RGB PNG."""
    raw_scanlines = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    compressed = zlib.compress(raw_scanlines)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr_data) + _chunk(b"IDAT", compressed) + _chunk(b"IEND", b"")


def _read_bmp_pixel(bmp_path: str, x: int, y: int) -> tuple[int, int, int, int, int]:
    """Parse uncompressed BMP dimensions and pixel (R, G, B) at (x, y)."""
    with open(bmp_path, "rb") as f:
        data = f.read()

    assert data[:2] == b"BM", "Not a BMP file"
    offset = struct.unpack_from("<I", data, 10)[0]
    width = struct.unpack_from("<i", data, 18)[0]
    height_raw = struct.unpack_from("<i", data, 22)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]
    top_down = height_raw < 0
    height = abs(height_raw)

    bytes_per_pixel = bpp // 8
    row_bytes = ((width * bytes_per_pixel + 3) // 4) * 4

    row_index = y if top_down else (height - 1 - y)
    pixel_offset = offset + row_index * row_bytes + x * bytes_per_pixel

    b = data[pixel_offset]
    g = data[pixel_offset + 1]
    r = data[pixel_offset + 2]
    return width, height, r, g, b


@native_test
@with_native_doc("writer")
def test_image_mark_bake_crop_and_highlight(ctx: Any, doc: Any) -> None:
    png_bytes = _create_white_png(40, 40)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tf.write(png_bytes)
        tmp_png = tf.name

    tmp_bmp = ""
    try:
        graphic_shape = insert_image_at_locator(ctx, doc, tmp_png, width_mm=20, height_mm=20)
        assert graphic_shape is not None
        source_graphic = graphic_shape.getPropertyValue("Graphic")

        crop_region = (5, 5, 20, 20)
        marks: list[tuple[tuple[int, int, int, int], str, int]] = [((2, 2, 10, 10), "highlight", 0xFFEB3B)]
        baked_graphic = image_mark.bake(ctx, source_graphic, crop_region, marks, stroke=1)
        assert baked_graphic is not None

        fd, tmp_bmp = tempfile.mkstemp(suffix=".bmp")
        os.close(fd)
        smgr = ctx.ServiceManager
        provider = smgr.createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx)
        image_mark._store(provider, baked_graphic, tmp_bmp, "image/x-MS-bmp")

        width, height, r, g, b = _read_bmp_pixel(tmp_bmp, 5, 5)
        assert width == 20
        assert height == 20
        assert abs(r - 0xFF) <= 2, f"Red channel {r} not within 2 of 0xFF"
        assert abs(g - 0xEB) <= 2, f"Green channel {g} not within 2 of 0xEB"
        assert abs(b - 0x3B) <= 2, f"Blue channel {b} not within 2 of 0x3B"
    finally:
        for p in (tmp_png, tmp_bmp):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass
