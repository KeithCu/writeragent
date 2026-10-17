# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import Any

from plugin.ppt_master.adapter.uno_shape_postprocess import (
    _assign_graphic,
    _is_graphic_shape,
    _shape_graphic,
    clear_page_shapes,
    clone_shape_to_page,
)
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("impress")
def test_shape_graphic_fallback_and_clone(ctx: Any, doc: Any) -> None:
    pages = doc.getDrawPages()
    assert pages.getCount() >= 1
    page = pages.getByIndex(0)

    # 1. Non-graphic shape (RectangleShape) raises UnknownProperty on Graphic,
    # so _shape_graphic gracefully falls back to returning None.
    rect_shape = doc.createInstance("com.sun.star.drawing.RectangleShape")
    page.add(rect_shape)
    assert not _is_graphic_shape(rect_shape)
    assert _shape_graphic(rect_shape) is None

    # Assigning graphic to a non-graphic shape fails gracefully and returns False.
    assert _assign_graphic(rect_shape, object()) is False

    # 2. GraphicObjectShape: without uno_ctx or when reimport cannot run,
    # clone_shape_to_page uses the fallback path of leaving Graphic unset
    # rather than aliasing the source pool.
    graphic_shape = doc.createInstance("com.sun.star.drawing.GraphicObjectShape")
    page.add(graphic_shape)
    assert _is_graphic_shape(graphic_shape)

    cloned = clone_shape_to_page(graphic_shape, doc, page, uno_ctx=None)
    assert cloned is not None
    assert _shape_graphic(cloned) is None

    # 3. clear_page_shapes removes all shapes from the page
    assert page.getCount() >= 3
    clear_page_shapes(page)
    assert page.getCount() == 0
