# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live UNO test for render_preview_page and scratch doc cleanup."""

from __future__ import annotations

from plugin.framework.uno_context import enum_has_more
from plugin.testing_runner import native_test
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
def test_render_preview_page_returns_1600px_graphic_and_closes_scratch_doc(ctx):
    before = _desktop_component_count(ctx)
    img = render_preview_page(ctx, "<h1>T</h1><p>x</p>", "s")
    after = _desktop_component_count(ctx)

    assert img.width_px == 1600
    assert abs(img.graphic.SizePixel.Width - 1600) <= 1, img.graphic.SizePixel.Width
    assert img.height_px > 0
    assert img.summary == "s"
    assert after == before, f"Scratch doc leaked: before={before}, after={after}"
