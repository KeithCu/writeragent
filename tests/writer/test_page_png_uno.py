# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live UNO test for FilterData pixel sizing in render_writer_page_png."""

from __future__ import annotations

import struct

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.writer.page_png import PNG_MAGIC, render_writer_page_png


@native_test
@with_native_doc("writer")
def test_render_writer_page_png_pixel_size_uno(ctx, doc):
    del ctx
    png, reason = render_writer_page_png(doc, 0, pixel_size=(800, 1035))
    assert reason is None, reason
    assert png is not None and png[:8] == PNG_MAGIC
    # PNG IHDR starts at byte 16: width (4 bytes BE), height (4 bytes BE).
    ihdr_w, _ihdr_h = struct.unpack(">II", png[16:24])
    assert abs(ihdr_w - 800) <= 1, f"Expected IHDR width ~800, got {ihdr_w}"
