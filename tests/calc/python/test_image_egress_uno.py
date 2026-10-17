# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests: Calc shape anchor reads on live document."""

from __future__ import annotations

from plugin.calc.python.image_egress import _cell_address_key
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
    finally:
        draw_page.remove(shape)
