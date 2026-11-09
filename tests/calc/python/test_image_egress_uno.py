# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests: Calc shape anchor reads on live document."""

from __future__ import annotations

from plugin.calc.python.image_egress import _cell_address_key, _shape_anchor_matches_cell
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_calc_page_anchored_shape_anchor_read_uno(ctx, doc):
    """Calc page-anchored shape anchor is CellRangeAddressable (REPORT37 E1)."""
    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    shape = doc.createInstance("com.sun.star.drawing.GraphicObjectShape")
    draw_page.add(shape)
    # The calc doc is pooled: a leftover shape grows sheet 0's used area and
    # breaks later used-range tests (calc.test_duckdb_tools_uno).
    try:
        anchor = shape.getPropertyValue("Anchor")
        assert anchor is not None
        # A page-anchored shape's Anchor in Calc is the sheet itself, which has getRangeAddress().
        key = _cell_address_key(anchor)
        assert key is not None
        assert key[0] == 0  # Sheet index 0
        # The sheet range starts at 0,0, but a page-anchored shape is not on A1.
        assert _shape_anchor_matches_cell(shape, sheet.getCellByPosition(0, 0)) is False
    finally:
        draw_page.remove(shape)


def _png_bytes(width: int, height: int) -> bytes:
    """A valid solid-red RGB PNG of the given pixel size."""
    import struct
    import zlib

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


@native_test
@with_native_doc("calc")
def test_reanchored_plot_slots_chain_reuse_and_sweep_uno(ctx, doc):
    """Live Calc re-anchors slots beyond the formula cell; the owner tag still finds them."""
    from plugin.calc.python.image_egress import (
        _anchored_plot_slots,
        _insert_image_result_on_sheet_impl,
        _read_plot_tag,
    )

    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    cell = sheet.getCellByPosition(0, 0)
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    # With no formula code, the selected cell is the plot owner.
    controller.select(cell)
    payloads = [
        {"__wa_payload__": "image", "format": "png", "data": _png_bytes(20 + i, 10)} for i in range(3)
    ]

    def run(count: int) -> list:
        for i in range(count):
            _insert_image_result_on_sheet_impl(payloads[i], doc=doc, image_index=i, image_count=count)
        return [shape for shape, _idx in sorted(_anchored_plot_slots(draw_page, cell), key=lambda s: s[1])]

    try:
        slots = run(3)
        assert len(slots) == 3
        # Calc moved slot 1's anchor off A1 to the cell under its new position.
        assert not _shape_anchor_matches_cell(slots[1], cell)
        assert _read_plot_tag(slots[1])[0] == (0, 0)
        for prev, nxt in zip(slots, slots[1:]):
            assert nxt.getPosition().X == prev.getPosition().X + prev.getSize().Width

        assert len(run(3)) == 3
        assert draw_page.getCount() == 3

        assert len(run(2)) == 2
        assert draw_page.getCount() == 2
    finally:
        while draw_page.getCount():
            draw_page.remove(draw_page.getByIndex(0))
