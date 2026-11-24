# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live UNO test for render_preview_page and scratch doc cleanup."""

from __future__ import annotations

from plugin.framework.uno_context import enum_has_more
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.vision.vision_preview import render_preview_page


def _desktop_component_count(ctx) -> int:
    desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    enum = desktop.getComponents().createEnumeration()
    count = 0
    while enum_has_more(enum):
        enum.nextElement()
        count += 1
    return count


@native_test
@with_native_doc("writer")
def test_render_preview_page_returns_1600px_graphic_and_closes_scratch_doc(ctx, doc):
    cursor = doc.getText().createTextCursor()
    style = doc.getStyleFamilies().getByName("PageStyles").getByName(cursor.getPropertyValue("PageStyleName"))
    expected_h = round(1600 * style.getPropertyValue("Height") / style.getPropertyValue("Width"))

    before = _desktop_component_count(ctx)
    img = render_preview_page(ctx, "<h1>T</h1><p>x</p>", "s")
    after = _desktop_component_count(ctx)

    assert img.width_px == 1600
    assert img.height_px == expected_h
    assert abs(img.graphic.SizePixel.Width - 1600) <= 1, img.graphic.SizePixel.Width
    assert abs(img.graphic.SizePixel.Height - img.height_px) <= 1, img.graphic.SizePixel.Height
    assert img.summary == "s"
    assert after == before, f"Scratch doc leaked: before={before}, after={after}"
