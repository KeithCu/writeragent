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
        return [shape for shape, _idx in sorted(_anchored_plot_slots(sheet, cell), key=lambda s: s[1])]

    try:
        slots = run(3)
        assert len(slots) == 3
        # Calc moved slot 1's anchor off A1 to the cell under its new position.
        assert not _shape_anchor_matches_cell(slots[1], cell)
        assert _read_plot_tag(slots[1]).owner == (0, 0)
        for prev, nxt in zip(slots, slots[1:]):
            assert nxt.getPosition().X == prev.getPosition().X + prev.getSize().Width

        assert len(run(3)) == 3
        assert draw_page.getCount() == 3

        assert len(run(2)) == 2
        assert draw_page.getCount() == 2
    finally:
        while draw_page.getCount():
            draw_page.remove(draw_page.getByIndex(0))


@native_test
@with_native_doc("calc")
def test_plot_follows_formula_after_row_and_column_insert_uno(ctx, doc):
    """Inserting rows/columns above or left of the formula reuses the existing plot slots."""
    from plugin.calc.python.image_egress import _insert_image_result_on_sheet_impl

    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    payloads = [
        {"__wa_payload__": "image", "format": "png", "data": _png_bytes(20 + i, 10)} for i in range(2)
    ]

    inserted_row = False
    inserted_col = False
    try:
        b3 = sheet.getCellByPosition(1, 2)
        controller.select(b3)
        for i in range(2):
            _insert_image_result_on_sheet_impl(payloads[i], doc=doc, image_index=i, image_count=2)
        assert draw_page.getCount() == 2
        orig_shapes = [draw_page.getByIndex(0), draw_page.getByIndex(1)]
        orig_positions = [(s.getPosition().X, s.getPosition().Y) for s in orig_shapes]

        sheet.getRows().insertByIndex(0, 1)
        inserted_row = True
        sheet.getColumns().insertByIndex(0, 1)
        inserted_col = True
        moved_positions = [(s.getPosition().X, s.getPosition().Y) for s in orig_shapes]

        c4 = sheet.getCellByPosition(2, 3)
        controller.select(c4)
        for i in range(2):
            _insert_image_result_on_sheet_impl(payloads[i], doc=doc, image_index=i, image_count=2)

        assert draw_page.getCount() == 2
        assert draw_page.getByIndex(0) == orig_shapes[0]
        assert draw_page.getByIndex(1) == orig_shapes[1]
        assert [(draw_page.getByIndex(i).getPosition().X, draw_page.getByIndex(i).getPosition().Y) for i in range(2)] == moved_positions
        assert moved_positions != orig_positions
    finally:
        while draw_page.getCount():
            draw_page.remove(draw_page.getByIndex(0))
        if inserted_col:
            sheet.getColumns().removeByIndex(0, 1)
        if inserted_row:
            sheet.getRows().removeByIndex(0, 1)


@native_test
@with_native_doc("calc")
def test_dragged_plot_is_refreshed_in_place_uno(ctx, doc):
    """A plot dragged to another cell is refreshed in place at its new position."""
    from com.sun.star.awt import Point
    from plugin.calc.python.image_egress import _insert_image_result_on_sheet_impl

    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    b3 = sheet.getCellByPosition(1, 2)
    controller.select(b3)

    p1 = {"__wa_payload__": "image", "format": "png", "data": _png_bytes(20, 10)}
    p2 = {"__wa_payload__": "image", "format": "png", "data": _png_bytes(30, 15)}

    try:
        _insert_image_result_on_sheet_impl(p1, doc=doc, image_index=0, image_count=1)
        assert draw_page.getCount() == 1
        shape = draw_page.getByIndex(0)
        shape.setPosition(Point(30000, 20000))
        dragged_pos = (shape.getPosition().X, shape.getPosition().Y)

        controller.select(b3)
        _insert_image_result_on_sheet_impl(p2, doc=doc, image_index=0, image_count=1)
        assert draw_page.getCount() == 1
        assert draw_page.getByIndex(0) == shape
        assert (shape.getPosition().X, shape.getPosition().Y) == dragged_pos
    finally:
        while draw_page.getCount():
            draw_page.remove(draw_page.getByIndex(0))


@native_test
@with_native_doc("calc")
def test_pasted_plot_copy_survives_recalc_uno(ctx, doc):
    """A copy-pasted plot gets a non-slot name from Calc and survives recalc."""
    from plugin.calc.python.image_egress import (
        _insert_image_result_on_sheet_impl,
        _plot_slot_index,
        _read_plot_tag,
    )

    sheet = doc.getSheets().getByIndex(0)
    draw_page = sheet.getDrawPage()
    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    b3 = sheet.getCellByPosition(1, 2)
    controller.select(b3)

    p1 = {"__wa_payload__": "image", "format": "png", "data": _png_bytes(20, 10)}
    p2 = {"__wa_payload__": "image", "format": "png", "data": _png_bytes(24, 12)}

    try:
        _insert_image_result_on_sheet_impl(p1, doc=doc, image_index=0, image_count=1)
        assert draw_page.getCount() == 1
        orig = draw_page.getByIndex(0)
        orig_sha = _read_plot_tag(orig).sha

        controller.select(orig)
        transferable = controller.getTransferable()
        h20 = sheet.getCellByPosition(7, 19)
        controller.select(h20)
        controller.insertTransferable(transferable)
        assert draw_page.getCount() == 2

        controller.select(b3)
        _insert_image_result_on_sheet_impl(p2, doc=doc, image_index=0, image_count=1)
        assert draw_page.getCount() == 2

        shapes = [draw_page.getByIndex(i) for i in range(2)]
        copies = [s for s in shapes if _plot_slot_index(s.getPropertyValue("Name")) is None]
        assert len(copies) == 1
        assert _read_plot_tag(copies[0]).sha == orig_sha
    finally:
        while draw_page.getCount():
            draw_page.remove(draw_page.getByIndex(0))
