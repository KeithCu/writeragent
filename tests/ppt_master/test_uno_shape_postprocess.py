# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import tempfile
from typing import Any
from unittest.mock import MagicMock

from plugin.ppt_master.adapter.uno_shape_postprocess import (
    _COPY_PROPS,
    clone_shape_to_page,
    copy_shapes_to_page,
)

# Hidden-document pool items. Assigning these and then closing the source
# aborts soffice. See plugin/draw/designs.py.
_POOL_ITEM_PROPS = ("Graphic", "GraphicURL", "FillBitmap", "Background")


class FakeShape:
    def __init__(self, shape_type: str, props: dict[str, Any] | None = None, text: str = "") -> None:
        self.shape_type = shape_type
        self.props = dict(props or {})
        self.sets: list[tuple[str, Any]] = []
        self.position = (1, 2)
        self.size = (10, 20)
        self.string = text

    def getShapeType(self) -> str:
        return self.shape_type

    def getPropertyValue(self, name: str) -> Any:
        if name not in self.props:
            raise AttributeError(name)
        return self.props[name]

    def setPropertyValue(self, name: str, value: Any) -> None:
        self.sets.append((name, value))
        self.props[name] = value

    def getPosition(self) -> tuple[int, int]:
        return self.position

    def setPosition(self, value: tuple[int, int]) -> None:
        self.position = value

    def getSize(self) -> tuple[int, int]:
        return self.size

    def setSize(self, value: tuple[int, int]) -> None:
        self.size = value

    def getString(self) -> str:
        return self.string

    def setString(self, value: str) -> None:
        self.string = value


class FakePage:
    def __init__(self, shapes: list[Any] | None = None) -> None:
        self.shapes = list(shapes or [])

    def getCount(self) -> int:
        return len(self.shapes)

    def getByIndex(self, index: int) -> Any:
        return self.shapes[index]

    def add(self, shape: Any) -> None:
        self.shapes.append(shape)


def test_copy_props_omit_hidden_pool_items():
    for name in _POOL_ITEM_PROPS:
        assert name not in _COPY_PROPS


def test_graphic_clone_reimports_pixels_instead_of_aliasing_source_pool(monkeypatch):
    live = object()
    owned = object()
    source = FakeShape(
        "com.sun.star.drawing.GraphicObjectShape",
        {"FillColor": 5, "Graphic": live, "GraphicURL": "vnd.sun.star.GraphicObject:hidden-pool"},
        text="cap",
    )
    dest = FakeShape("com.sun.star.drawing.GraphicObjectShape")
    doc = MagicMock()
    doc.createInstance.return_value = dest
    page = FakePage()
    provider = MagicMock()
    provider.queryGraphic.side_effect = [None, owned]
    uno_ctx = MagicMock()
    uno_ctx.ServiceManager.createInstanceWithContext.return_value = provider
    created: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def tracking_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    monkeypatch.setattr(tempfile, "mkstemp", tracking_mkstemp)

    cloned = clone_shape_to_page(source, doc, page, uno_ctx=uno_ctx)

    assert cloned is dest
    graphic_values = [value for name, value in dest.sets if name == "Graphic"]
    assert graphic_values == [owned]
    assert live not in graphic_values
    assert all(name != "GraphicURL" for name, value in dest.sets)
    assert dest.props["FillColor"] == 5
    uno_ctx.ServiceManager.createInstanceWithContext.assert_called_once_with(
        "com.sun.star.graphic.GraphicProvider",
        uno_ctx,
    )
    assert provider.storeGraphic.call_count == 2
    assert provider.storeGraphic.call_args_list[0].args[0] is live
    assert provider.storeGraphic.call_args_list[1].args[0] is live
    assert created
    assert all(not os.path.exists(path) for path in created)


def test_rectangle_clone_does_not_touch_graphic_provider():
    source = FakeShape("com.sun.star.drawing.RectangleShape", {"FillColor": 9}, text="hi")
    dest = FakeShape("com.sun.star.drawing.RectangleShape")
    doc = MagicMock()
    doc.createInstance.return_value = dest
    uno_ctx = MagicMock()

    cloned = clone_shape_to_page(source, doc, FakePage(), uno_ctx=uno_ctx)

    assert cloned is dest
    assert dest.props["FillColor"] == 9
    assert "Graphic" not in dest.props
    assert "GraphicURL" not in dest.props
    uno_ctx.ServiceManager.createInstanceWithContext.assert_not_called()


def test_graphic_clone_without_context_does_not_alias_live_graphic():
    live = object()
    source = FakeShape(
        "com.sun.star.drawing.GraphicObjectShape",
        {"Graphic": live, "GraphicURL": "vnd.sun.star.GraphicObject:hidden-pool"},
    )
    dest = FakeShape("com.sun.star.drawing.GraphicObjectShape")
    doc = MagicMock()
    doc.createInstance.return_value = dest

    cloned = clone_shape_to_page(source, doc, FakePage(), uno_ctx=None)

    assert cloned is dest
    assert "Graphic" not in dest.props
    assert "GraphicURL" not in dest.props
    assert all(name != "Graphic" or value is not live for name, value in dest.sets)


def test_copy_shapes_to_page_forwards_uno_ctx(monkeypatch):
    seen: dict[str, Any] = {}
    source = FakeShape("com.sun.star.drawing.RectangleShape", {"FillColor": 1})
    dest_page = FakePage()
    doc = MagicMock()

    def fake_clone(source_shape: Any, target_doc: Any, target_page: Any, uno_ctx: Any | None = None) -> Any:
        seen["ctx"] = uno_ctx
        seen["source"] = source_shape
        seen["doc"] = target_doc
        seen["page"] = target_page
        return object()

    from plugin.ppt_master.adapter import uno_shape_postprocess as postprocess

    monkeypatch.setattr(postprocess, "clone_shape_to_page", fake_clone)
    marker = object()
    copied = copy_shapes_to_page(FakePage([source]), doc, dest_page, uno_ctx=marker)

    assert copied == 1
    assert seen["ctx"] is marker
    assert seen["source"] is source
    assert seen["doc"] is doc
    assert seen["page"] is dest_page
