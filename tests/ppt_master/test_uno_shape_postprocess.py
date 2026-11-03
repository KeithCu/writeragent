from __future__ import annotations
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later


import os
import tempfile
from typing import Any
from unittest.mock import MagicMock

import pytest

from plugin.draw.shape_clone import (
    SHAPE_CLONE_PROPS,
    ClonedShape,
    clone_shape,
)
from plugin.ppt_master.adapter import uno_shape_postprocess as postprocess
from plugin.ppt_master.adapter.uno_shape_postprocess import (
    clear_page_shapes,
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

    def remove(self, shape: Any) -> None:
        self.shapes.remove(shape)


def test_copy_props_omit_hidden_pool_items():
    for name in _POOL_ITEM_PROPS:
        assert name not in SHAPE_CLONE_PROPS
    assert "FillGradient" not in SHAPE_CLONE_PROPS
    assert "FillHatch" not in SHAPE_CLONE_PROPS


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

    result = clone_shape(doc, page, source, uno_ctx=uno_ctx)

    assert result.shape is dest
    assert result.reimported is True
    assert result.graphic_shapes == 1
    assert result.graphic_reimports == 1
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

    result = clone_shape(doc, FakePage(), source, uno_ctx=uno_ctx)

    assert result.shape is dest
    assert result.reimported is None
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

    result = clone_shape(doc, FakePage(), source, uno_ctx=None)

    assert result.shape is dest
    assert result.reimported is False
    assert "Graphic" not in dest.props
    assert "GraphicURL" not in dest.props
    assert all(name != "Graphic" or value is not live for name, value in dest.sets)


def test_copy_shapes_to_page_forwards_uno_ctx(monkeypatch):
    seen: dict[str, Any] = {}
    source = FakeShape("com.sun.star.drawing.RectangleShape", {"FillColor": 1})
    dest_page = FakePage()
    doc = MagicMock()

    def fake_clone(
        target_doc: Any,
        target_page: Any,
        source_shape: Any,
        *,
        uno_ctx: Any | None = None,
        **kwargs: Any,
    ) -> ClonedShape:
        seen["ctx"] = uno_ctx
        seen["source"] = source_shape
        seen["doc"] = target_doc
        seen["page"] = target_page
        return ClonedShape(
            shape=object(),
            shape_type="com.sun.star.drawing.RectangleShape",
            reimported=None,
            graphic_shapes=0,
            graphic_reimports=0,
            empty_group_message=None,
        )

    monkeypatch.setattr(postprocess, "clone_shape", fake_clone)
    marker = object()
    copied = copy_shapes_to_page(FakePage([source]), doc, dest_page, uno_ctx=marker)

    assert copied == 1
    assert seen["ctx"] is marker
    assert seen["source"] is source
    assert seen["doc"] is doc
    assert seen["page"] is dest_page


def test_clone_shape_copies_fill_gradient_and_hatch():
    doc = MagicMock()
    page = FakePage()

    # 1. Gradient with value="GRADIENT"
    dest1 = FakeShape("com.sun.star.drawing.RectangleShape")
    doc.createInstance.return_value = dest1
    grad_val = MagicMock()
    grad_val.value = "GRADIENT"
    grad_struct = MagicMock()
    source1 = FakeShape(
        "com.sun.star.drawing.RectangleShape",
        {"FillStyle": grad_val, "FillGradient": grad_struct},
    )
    clone_shape(doc, page, source1)
    assert dest1.props.get("FillGradient") is grad_struct

    # 2. Gradient with int 2
    dest2 = FakeShape("com.sun.star.drawing.RectangleShape")
    doc.createInstance.return_value = dest2
    source2 = FakeShape(
        "com.sun.star.drawing.RectangleShape",
        {"FillStyle": 2, "FillGradient": grad_struct},
    )
    clone_shape(doc, page, source2)
    assert dest2.props.get("FillGradient") is grad_struct

    # 3. Hatch
    dest3 = FakeShape("com.sun.star.drawing.RectangleShape")
    doc.createInstance.return_value = dest3
    hatch_struct = MagicMock()
    source3 = FakeShape(
        "com.sun.star.drawing.RectangleShape",
        {"FillStyle": 3, "FillHatch": hatch_struct},
    )
    clone_shape(doc, page, source3)
    assert dest3.props.get("FillHatch") is hatch_struct


def test_copy_shapes_to_page_disposal_raises_while_runtime_exception_skips():
    class DisposedException(Exception):
        pass

    class RuntimeException(Exception):
        pass

    doc = MagicMock()
    page = FakePage()

    # Case A: shape 1 raises DisposedException -> copy_shapes_to_page raises, shape 2 not cloned
    shape1_disp = FakeShape("com.sun.star.drawing.RectangleShape")
    shape1_disp.getPropertyValue = MagicMock(side_effect=DisposedException("doc disposed"))
    shape2_ok = FakeShape("com.sun.star.drawing.RectangleShape")
    shape2_ok.getPropertyValue = MagicMock(return_value=None)
    src_page_disp = FakePage([shape1_disp, shape2_ok])

    dest_shape = FakeShape("com.sun.star.drawing.RectangleShape")
    doc.createInstance.return_value = dest_shape

    with pytest.raises(DisposedException):
        copy_shapes_to_page(src_page_disp, doc, page)
    # Shape 1 was added before disposal raised; shape 2 was never cloned
    assert page.getCount() == 1
    shape2_ok.getPropertyValue.assert_not_called()

    # Case B: shape 1 raises RuntimeException -> skipped, shape 2 is cloned, return count is 1
    shape1_err = FakeShape("com.sun.star.drawing.RectangleShape")
    shape1_err.getShapeType = MagicMock(side_effect=RuntimeException("shape corrupted"))
    shape2_ok2 = FakeShape("com.sun.star.drawing.RectangleShape")
    src_page_err = FakePage([shape1_err, shape2_ok2])

    target_page = FakePage()
    copied = copy_shapes_to_page(src_page_err, doc, target_page)
    assert copied == 1
    assert target_page.getCount() == 1


def test_clear_page_shapes():
    page = FakePage([FakeShape("s1"), FakeShape("s2")])
    assert page.getCount() == 2
    clear_page_shapes(page)
    assert page.getCount() == 0
