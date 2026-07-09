# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests: vision OCR HTML insert preserves the selected embedded graphic."""

from __future__ import annotations

import os

from plugin.doc.visual_helpers import list_graphic_objects
from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import native_test, setup, teardown
from plugin.vision.vision_egress import prepare_vision_writer_insert
from plugin.writer.format import insert_html_at_cursor
from plugin.writer.images.image_tools import insert_image_at_locator

_VISION_HTML_FIXTURE = "<p>Vision OCR line after image.</p>"

_test_doc = None
_test_ctx = None


def _vision_test_logo_path() -> str:
    """Return logo_32.png in dev tree or release bundle (assets path is remapped)."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for rel in ("extension/assets/logo_32.png", "assets/logo_32.png"):
        path = os.path.join(root, *rel.split("/"))
        if os.path.isfile(path):
            return path
    return os.path.join(root, "extension", "assets", "logo_32.png")


_logo_path = _vision_test_logo_path()


@setup
def setup_vision_graphic_insert_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    desktop = get_desktop(ctx)
    import uno

    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    _test_doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))
    assert _test_doc is not None, "Could not create Writer document"
    assert os.path.isfile(_logo_path), f"fixture image missing: {_logo_path}"


@teardown
def teardown_vision_graphic_insert_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        try:
            _test_doc.close(True)
        except Exception:
            pass
    _test_doc = None
    _test_ctx = None


@native_test
def test_vision_html_insert_keeps_selected_graphic():
    graphic = insert_image_at_locator(_test_ctx, _test_doc, _logo_path, width_mm=20, height_mm=20)
    assert graphic is not None, "failed to insert fixture image"

    controller = _test_doc.getCurrentController()
    controller.select(graphic)

    before = list_graphic_objects(_test_doc)
    assert len(before) == 1, f"expected one graphic before insert, got {before!r}"

    cursor = prepare_vision_writer_insert(_test_doc, _test_ctx)
    insert_html_at_cursor(_test_doc, _test_ctx, cursor, _VISION_HTML_FIXTURE, apply_styles=False)

    after = list_graphic_objects(_test_doc)
    assert len(after) == 1, f"graphic was removed during HTML insert: before={before!r} after={after!r}"
    assert "Vision OCR line after image." in _test_doc.getText().getString()
