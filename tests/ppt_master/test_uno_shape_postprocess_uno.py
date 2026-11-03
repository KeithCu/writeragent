# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import Any

from plugin.draw.shape_clone import clone_shape
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("impress")
def test_shape_graphic_fallback_and_clone(ctx: Any, doc: Any) -> None:
    pages = doc.getDrawPages()
    assert pages.getCount() >= 1
    page = pages.getByIndex(0)

    # 1. Non-graphic shape (RectangleShape): clone_shape does not set Graphic.
    rect_shape = doc.createInstance("com.sun.star.drawing.RectangleShape")
    page.add(rect_shape)
    cloned_rect = clone_shape(doc, page, rect_shape, uno_ctx=ctx)
    assert cloned_rect.reimported is None

    # 2. GraphicObjectShape: with uno_ctx=None, leaves Graphic unset
    # rather than aliasing the source pool.
    graphic_shape = doc.createInstance("com.sun.star.drawing.GraphicObjectShape")
    page.add(graphic_shape)
    cloned_graphic = clone_shape(doc, page, graphic_shape, uno_ctx=None)
    assert cloned_graphic.reimported is False
