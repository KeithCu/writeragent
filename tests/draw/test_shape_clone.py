# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.draw.shape_clone import (
    SHAPE_CLONE_PROPS,
    clone_shape,
    copy_prop,
    reimport_graphic,
)


def test_clone_shape_sets_geometry_after_add():
    calls: list[str] = []
    dest_doc = MagicMock()
    clone = MagicMock()
    dest_doc.createInstance.return_value = clone
    dest_master = MagicMock()
    dest_master.add.side_effect = lambda _unused: calls.append("add")
    src = MagicMock()
    src.ShapeType = "com.sun.star.drawing.GraphicObjectShape"
    with patch(
        "plugin.draw.shape_clone.copy_prop",
        side_effect=lambda _dest, _src, name: calls.append(name) or True,
    ):
        out = clone_shape(dest_doc, dest_master, src)
    assert out.shape_type == "com.sun.star.drawing.GraphicObjectShape"
    assert calls[0] == "add"
    assert calls[-2:] == ["Position", "Size"]
    dest_doc.createInstance.assert_called_once_with("com.sun.star.drawing.GraphicObjectShape")


def test_clone_shape_rejects_non_uno_type():
    src = MagicMock()
    src.ShapeType = "not.a.sun.star.shape"
    with pytest.raises(RuntimeError, match="Unsupported shape type"):
        clone_shape(MagicMock(), MagicMock(), src)


def test_copy_prop_falls_back_to_attribute_access():
    class ShapeStub:
        def __init__(self, pos=None):
            self.Position = pos

        def getPropertyValue(self, name):
            raise RuntimeError("UnknownPropertyException")

        def setPropertyValue(self, name, val):
            raise RuntimeError("UnknownPropertyException")

    src = ShapeStub((100, 200))
    dest = ShapeStub((0, 0))
    assert copy_prop(dest, src, "Position") == "copied"
    assert dest.Position == (100, 200)

    # Missing attribute returns "missing"
    assert copy_prop(dest, src, "NoSuchProp") == "missing"

    # Destination rejecting a property returns "rejected"
    class RejectDest:
        def getPropertyValue(self, name):
            raise RuntimeError("UnknownPropertyException")

        def setPropertyValue(self, name, val):
            raise RuntimeError("CannotSet")

        def __setattr__(self, name, val):
            if name == "ZOrder":
                raise RuntimeError("CannotSetAttr")
            super().__setattr__(name, val)

    reject_dest = RejectDest()
    src_prop = MagicMock()
    src_prop.getPropertyValue.return_value = 5
    assert copy_prop(reject_dest, src_prop, "ZOrder") == "rejected"


def test_copy_prop_disposal_propagates():
    class DisposedException(Exception):
        pass

    src = MagicMock()
    src.getPropertyValue.side_effect = DisposedException("disposed")
    dest = MagicMock()

    with pytest.raises(DisposedException):
        copy_prop(dest, src, "Position")

    src_missing = MagicMock(spec=["getPropertyValue"])
    src_missing.getPropertyValue.side_effect = AttributeError("missing")
    assert copy_prop(dest, src_missing, "NoSuchProp") == "missing"


def test_copy_prop_geometry_failure_raises():
    class BrokenDest:
        def setPropertyValue(self, name, val):
            raise RuntimeError("cannot set property")

        def __setattr__(self, name, val):
            if name in ("Position", "Size"):
                raise RuntimeError("cannot set attr")
            super().__setattr__(name, val)

    src = MagicMock()
    src.getPropertyValue.return_value = (10, 20)
    dest = BrokenDest()

    with pytest.raises(RuntimeError, match="Failed to set Position"):
        copy_prop(dest, src, "Position")


def test_clone_shape_rejected_prop_records_warning():
    dest_doc = MagicMock()

    class RejectingShape:
        def getPropertyValue(self, name):
            raise RuntimeError("UnknownPropertyException")

        def setPropertyValue(self, name, val):
            if name in ("ZOrder", "Position", "Size"):
                raise RuntimeError("CannotSet")

        def __setattr__(self, name, val):
            if name in ("ZOrder", "Position", "Size"):
                raise RuntimeError("CannotSetAttr")
            super().__setattr__(name, val)

    clone = RejectingShape()
    dest_doc.createInstance.return_value = clone
    dest_master = MagicMock()

    src_shape = MagicMock()
    src_shape.ShapeType = "com.sun.star.drawing.RectangleShape"
    src_shape.getPropertyValue.side_effect = lambda n: 1 if n == "ZOrder" else (0, 0)

    warnings: list[str] = []
    # Position / Size still raise
    with pytest.raises(RuntimeError, match="Failed to set Position"):
        clone_shape(dest_doc, dest_master, src_shape, shape_index=2, warnings=warnings)
    # ZOrder was attempted before Position / Size and recorded its warning
    assert "rejected ZOrder on shape 2" in warnings


def test_clone_fill_gradient_success_copies_gradient_no_warning():
    dest_doc = MagicMock()
    clone = MagicMock()
    dest_doc.createInstance.return_value = clone
    dest_master = MagicMock()

    src_shape = MagicMock()
    src_shape.ShapeType = "com.sun.star.drawing.CustomShape"
    grad_enum = MagicMock()
    grad_enum.value = "GRADIENT"
    gradient_struct = MagicMock()

    def get_prop(n):
        if n == "FillStyle":
            return grad_enum
        if n == "FillGradient":
            return gradient_struct
        return None

    src_shape.getPropertyValue.side_effect = get_prop

    warnings: list[str] = []
    clone_shape(dest_doc, dest_master, src_shape, shape_index=0, warnings=warnings)

    clone.setPropertyValue.assert_any_call("FillGradient", gradient_struct)
    assert not any("fill gradient not copied" in w for w in warnings)


def test_clone_group_shape_with_children():
    dest_doc = MagicMock()
    group_clone = MagicMock()
    group_children: list[Any] = []
    group_clone.add.side_effect = group_children.append
    group_clone.getCount.side_effect = lambda: len(group_children)

    child1_clone = MagicMock()
    child2_clone = MagicMock()
    created_shapes = [group_clone, child1_clone, child2_clone]
    dest_doc.createInstance.side_effect = lambda t: created_shapes.pop(0)

    dest_master = MagicMock()

    src_child1 = MagicMock()
    src_child1.ShapeType = "com.sun.star.drawing.CustomShape"
    src_child1.CustomShapeGeometry = {"geom": 1}
    src_child1.PolyPolygon = ((0, 0), (1, 1))
    src_child1.RotateAngle = 4500
    src_child1.getPropertyValue.side_effect = lambda n: getattr(src_child1, n, None)

    src_child2 = MagicMock()
    src_child2.ShapeType = "com.sun.star.drawing.RectangleShape"
    src_child2.getPropertyValue.side_effect = lambda n: getattr(src_child2, n, None)

    src_group = MagicMock()
    src_group.ShapeType = "com.sun.star.drawing.GroupShape"
    src_group.getCount.return_value = 2
    src_group.getByIndex.side_effect = lambda i: src_child1 if i == 0 else src_child2

    warnings: list[str] = []
    clone_shape(dest_doc, dest_master, src_group, shape_index=0, warnings=warnings)

    dest_master.add.assert_called_once_with(group_clone)
    assert group_children == [child1_clone, child2_clone]

    props_set = [call.args[0] for call in child1_clone.setPropertyValue.call_args_list]
    assert "CustomShapeGeometry" in props_set
    assert "PolyPolygon" in props_set
    assert "RotateAngle" in props_set
    assert "FillBitmap" not in props_set


def test_reimport_graphic_disposed_propagates():
    class DisposedException(Exception):
        pass

    ctx = MagicMock()
    src_shape = MagicMock()
    src_shape.getPropertyValue.side_effect = DisposedException("disposed")
    dest_shape = MagicMock()

    with pytest.raises(DisposedException):
        reimport_graphic(ctx, src_shape, dest_shape)


def test_reimport_graphic_rejected_file_url_fallback(tmp_path):
    pic = tmp_path / "img.png"
    pic.write_bytes(b"\x89PNG\r\n\x1a\n")

    ctx = MagicMock()
    src_shape = MagicMock()
    src_shape.Graphic = MagicMock()
    dest_shape = MagicMock()

    first_spv = True
    first_attr = True

    def spv(name, val):
        nonlocal first_spv
        if name == "Graphic" and first_spv:
            first_spv = False
            raise RuntimeError("cannot setProperty Graphic from file")
        dest_shape._g = val

    dest_shape.setPropertyValue.side_effect = spv

    def assign_graphic(self, val):
        nonlocal first_attr
        if first_attr:
            first_attr = False
            raise RuntimeError("cannot setattr Graphic from file")
        dest_shape._g = val

    type(dest_shape).Graphic = property(lambda self: getattr(self, "_g", None), assign_graphic)

    provider = MagicMock()
    provider.queryGraphic.return_value = MagicMock()
    ctx.ServiceManager.createInstanceWithContext.return_value = provider

    ok = reimport_graphic(ctx, src_shape, dest_shape, image_path=str(pic))
    assert ok is True
    provider.storeGraphic.assert_called_once()

    # Both assigns failing returns False and source graphic object is absent from dest
    fail_dest = MagicMock()
    fail_dest.setPropertyValue.side_effect = RuntimeError("never setProperty")
    type(fail_dest).Graphic = property(
        lambda self: getattr(self, "_g", None),
        lambda self, val: (_ for _ in ()).throw(RuntimeError("never setattr")),
    )
    src_obj = MagicMock()
    src_shape2 = MagicMock()
    src_shape2.Graphic = src_obj
    ok_fail = reimport_graphic(ctx, src_shape2, fail_dest)
    assert ok_fail is False
    assert getattr(fail_dest, "_g", None) is not src_obj


# hidden-document pool items; assigning one and closing the source aborts soffice
_POOL_ITEM_PROPS = ("Graphic", "GraphicURL", "FillBitmap", "Background")


class _ScFakeShape:
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


class _ScFakePage:
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
    source = _ScFakeShape(
        "com.sun.star.drawing.GraphicObjectShape",
        {"FillColor": 5, "Graphic": live, "GraphicURL": "vnd.sun.star.GraphicObject:hidden-pool"},
        text="cap",
    )
    dest = _ScFakeShape("com.sun.star.drawing.GraphicObjectShape")
    doc = MagicMock()
    doc.createInstance.return_value = dest
    page = _ScFakePage()
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
    assert created
    assert all(not os.path.exists(path) for path in created)


def test_rectangle_clone_does_not_touch_graphic_provider():
    source = _ScFakeShape("com.sun.star.drawing.RectangleShape", {"FillColor": 9}, text="hi")
    dest = _ScFakeShape("com.sun.star.drawing.RectangleShape")
    doc = MagicMock()
    doc.createInstance.return_value = dest
    uno_ctx = MagicMock()

    result = clone_shape(doc, _ScFakePage(), source, uno_ctx=uno_ctx)

    assert result.shape is dest
    assert result.reimported is None
    assert dest.props["FillColor"] == 9
    assert "Graphic" not in dest.props
    assert "GraphicURL" not in dest.props
    uno_ctx.ServiceManager.createInstanceWithContext.assert_not_called()


def test_graphic_clone_without_context_does_not_alias_live_graphic():
    live = object()
    source = _ScFakeShape(
        "com.sun.star.drawing.GraphicObjectShape",
        {"Graphic": live, "GraphicURL": "vnd.sun.star.GraphicObject:hidden-pool"},
    )
    dest = _ScFakeShape("com.sun.star.drawing.GraphicObjectShape")
    doc = MagicMock()
    doc.createInstance.return_value = dest

    result = clone_shape(doc, _ScFakePage(), source, uno_ctx=None)

    assert result.shape is dest
    assert result.reimported is False
    assert "Graphic" not in dest.props
    assert "GraphicURL" not in dest.props
    assert all(name != "Graphic" or value is not live for name, value in dest.sets)


@pytest.mark.parametrize(
    ("fill_style_kind", "prop_name"),
    [
        ("enum_gradient", "FillGradient"),
        ("int_gradient", "FillGradient"),
        ("int_hatch", "FillHatch"),
    ],
)
def test_clone_shape_copies_fill_gradient_and_hatch(fill_style_kind: str, prop_name: str):
    doc = MagicMock()
    page = _ScFakePage()
    dest = _ScFakeShape("com.sun.star.drawing.RectangleShape")
    doc.createInstance.return_value = dest

    if fill_style_kind == "enum_gradient":
        fill_style: Any = MagicMock()
        fill_style.value = "GRADIENT"
    elif fill_style_kind == "int_gradient":
        fill_style = 2
    else:
        fill_style = 3

    fill_struct = MagicMock()
    source = _ScFakeShape(
        "com.sun.star.drawing.RectangleShape",
        {"FillStyle": fill_style, prop_name: fill_struct},
    )
    clone_shape(doc, page, source)
    assert dest.props.get(prop_name) is fill_struct

