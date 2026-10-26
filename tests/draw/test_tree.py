# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Draw tree blank detection, label hints, and control snapshots."""

from __future__ import annotations

import types
from types import SimpleNamespace
from typing import Any, cast

import pytest

from plugin.draw.tree import (
    GetDrawTree,
    attach_label_hints,
    build_shape_tree,
    coerce_control_state,
    control_snapshot,
    find_shape_on_page,
    is_near_empty_text,
    is_text_capable_shape_type,
    nearest_label_hint,
)
from plugin.framework.errors import DocumentDisposedError


class _Pos:
    def __init__(self, x: int, y: int) -> None:
        self.X, self.Y = x, y


class _Size:
    def __init__(self, width: int, height: int) -> None:
        self.Width, self.Height = width, height


class _Shapes:
    def __init__(self, items: Any) -> None:
        self._items = list(items)

    def getCount(self) -> int:
        return len(self._items)

    def getByIndex(self, i: int) -> Any:
        return self._items[i]


class _Shape:
    def __init__(
        self,
        shape_type: str,
        *,
        name: str = "",
        text: str | None = None,
        position: Any = None,
        size: Any = None,
        props: dict[str, Any] | None = None,
        control: Any = None,
        children: list[Any] | None = None,
    ) -> None:
        self._type = shape_type
        self.Name = name
        self._text = text
        self._position = position if position is not None else _Pos(0, 0)
        self._size = size if size is not None else _Size(0, 0)
        self._props = props or {}
        self.Control = control
        self._children = children
        self._shapes = _Shapes(children) if children is not None else None

    def getShapeType(self) -> str:
        return self._type

    def getString(self) -> str | None:
        return self._text

    def getPosition(self) -> Any:
        return self._position

    def getSize(self) -> Any:
        return self._size

    def getPropertyValue(self, prop: str) -> Any:
        return self._props.get(prop)

    def getCount(self) -> int:
        return len(self._children or [])

    def getByIndex(self, i: int) -> Any:
        return (self._children or [])[i]


class _DisposedException(Exception):
    pass


class _SimpleNameValue:
    def __init__(self, name: str, value: Any) -> None:
        self.Name = name
        self.Value = value



def test_is_near_empty_text_treats_placeholders_as_blank():
    assert is_near_empty_text("")
    assert is_near_empty_text("   ")
    assert is_near_empty_text("____")
    assert is_near_empty_text("...")
    assert is_near_empty_text("☐")
    assert not is_near_empty_text("Name")
    assert not is_near_empty_text("A")


def test_is_text_capable_excludes_lines_graphics_controls():
    assert is_text_capable_shape_type("com.sun.star.drawing.TextShape")
    assert is_text_capable_shape_type("RectangleShape")
    assert is_text_capable_shape_type("CustomShape")
    assert not is_text_capable_shape_type("com.sun.star.drawing.LineShape")
    assert not is_text_capable_shape_type("ConnectorShape")
    assert not is_text_capable_shape_type("GraphicObjectShape")
    assert not is_text_capable_shape_type("ControlShape")
    assert not is_text_capable_shape_type("TableShape")


def test_nearest_label_hint_prefers_left_over_above():
    blank = {"geometry": {"x": 5000, "y": 2000, "width": 4000, "height": 800}}
    left = {"text": "Name:", "geometry": {"x": 500, "y": 1900, "width": 4000, "height": 800}}
    above = {"text": "Heading", "geometry": {"x": 5000, "y": 200, "width": 4000, "height": 800}}
    assert nearest_label_hint(blank, [left, above]) == "Name:"


def test_nearest_label_hint_left_priority_over_smaller_above_gap():
    blank = {"geometry": {"x": 5000, "y": 2000, "width": 4000, "height": 800}}
    left = {"text": "Name:", "geometry": {"x": 100, "y": 2000, "width": 3000, "height": 800}}  # gap = 1900
    above = {"text": "Heading", "geometry": {"x": 5000, "y": 1500, "width": 4000, "height": 400}}  # gap = 100
    assert nearest_label_hint(blank, [left, above]) == "Name:"



def test_attach_label_hints_marks_fillable_only():
    nodes = [
        {"text": "Product:", "geometry": {"x": 100, "y": 1000, "width": 2000, "height": 400}},
        {"fillable": True, "name": "", "geometry": {"x": 2500, "y": 1000, "width": 3000, "height": 400}},
        {"text": "Already filled", "geometry": {"x": 2500, "y": 2000, "width": 3000, "height": 400}},
    ]
    attach_label_hints(nodes)
    assert nodes[1]["label_hint"] == "Product:"
    assert "label_hint" not in nodes[2]


def test_coerce_control_state_accepts_yes_no_and_ints():
    assert coerce_control_state(True) == 1
    assert coerce_control_state(False) == 0
    assert coerce_control_state(1) == 1
    assert coerce_control_state("checked") == 1
    assert coerce_control_state("no") == 0
    assert coerce_control_state("indeterminate") == 2
    assert coerce_control_state("not-a-state") is None


def test_control_snapshot_reads_state_and_text():
    class _Model:
        Name = "Agree"
        Label = "I agree"
        State = 1

        def supportsService(self, svc):
            return svc.endswith("CheckBox")

    snap = control_snapshot(_Model())
    assert snap["type"] == "checkbox"
    assert snap["name"] == "Agree"
    assert snap["state"] == 1
    assert snap["label"] == "I agree"


def test_control_snapshot_listbox_and_shared_control_map():
    from plugin.draw.tree import CONTROL_TYPE_MAP
    import plugin.writer.specialized.forms as forms

    assert forms.CONTROL_TYPE_MAP is CONTROL_TYPE_MAP

    class _ListBoxModel:
        Name = "Options"
        Label = "Select one"

        def supportsService(self, svc):
            return "ListBox" in svc

    snap = control_snapshot(_ListBoxModel())
    assert snap["type"] == "listbox"
    assert snap["name"] == "Options"
    assert snap["label"] == "Select one"


def test_safe_attr_reraises_disposed():
    import pytest
    from plugin.draw.tree import _safe_attr

    class DisposedException(Exception):
        pass

    class DisposedObj:
        @property
        def Name(self):
            raise DisposedException("closed")

    with pytest.raises(DisposedException):
        _safe_attr(DisposedObj(), "Name")

    class ErrorObj:
        @property
        def Name(self):
            raise RuntimeError("attribute failed")

    assert _safe_attr(ErrorObj(), "Name") == ""


def test_control_snapshot_reraises_disposed():
    import pytest
    from plugin.framework.errors import DocumentDisposedError

    class DisposedException(Exception):
        pass

    class _DisposedModel:
        Name = "Agree"

        def supportsService(self, svc):
            return True

        @property
        def Text(self):
            raise DocumentDisposedError("disposed")

    with pytest.raises(DocumentDisposedError):
        control_snapshot(_DisposedModel())


def test_build_shape_tree_top_level_and_group_child():
    shape0 = _Shape(
        "com.sun.star.drawing.TextShape",
        name="lbl_product",
        text="Product:",
        position=_Pos(100, 100),
        size=_Size(2000, 400),
    )
    child_a = _Shape(
        "com.sun.star.drawing.TextShape",
        text="Lot",
        position=_Pos(100, 1000),
        size=_Size(2000, 400),
    )
    child_b = _Shape(
        "com.sun.star.drawing.TextShape",
        name="",
        text="",
        position=_Pos(2500, 1000),
        size=_Size(3000, 400),
    )
    shape1 = _Shape(
        "com.sun.star.drawing.GroupShape",
        children=[child_a, child_b],
    )
    tree = build_shape_tree(_Shapes([shape0, shape1]))

    assert tree[0]["index"] == 0
    assert "path_index" not in tree[0]
    assert tree[0]["text"] == "Product:"
    assert tree[0]["geometry"] == {"x": 100, "y": 100, "width": 2000, "height": 400}

    assert tree[1]["type"] == "GroupShape"
    assert tree[1]["index"] == 1
    children = tree[1]["children"]
    assert children[0]["text"] == "Lot"
    assert children[1]["path_index"] == "1.1"
    assert "index" not in children[1]
    assert children[1]["fillable"] is True
    assert children[1]["label_hint"] == "Lot"


def test_build_shape_tree_control_connector_style_and_custom_type():
    class _FakeModel:
        Name = "Agree"
        Label = "I agree"
        State = 0

        def supportsService(self, svc: str) -> bool:
            return svc.endswith("CheckBox")

    control_shape = _Shape("com.sun.star.drawing.ControlShape", control=_FakeModel())
    shape_a = _Shape("com.sun.star.drawing.TextShape", name="NodeA", text="Alpha")
    shape_b = _Shape("com.sun.star.drawing.TextShape", name="NodeB", text="Beta")
    connector_shape = _Shape(
        "com.sun.star.drawing.ConnectorShape",
        props={"StartShape": shape_a, "EndShape": shape_b},
    )
    custom_shape = _Shape(
        "com.sun.star.drawing.CustomShape",
        props={
            "FillColor": 0x00FF00,
            "ZOrder": 3,
            "CustomShapeGeometry": [_SimpleNameValue("Type", "octagon")],
        },
    )

    tree = build_shape_tree(_Shapes([control_shape, connector_shape, custom_shape]))

    assert tree[0]["control"]["type"] == "checkbox"
    assert tree[0]["control"]["state"] == 0
    assert tree[1]["connected_start"] == {"name": "NodeA", "text": "Alpha"}
    assert tree[1]["connected_end"] == {"name": "NodeB", "text": "Beta"}
    assert tree[2]["style"]["FillColor"] == "#00FF00"
    assert tree[2]["style"]["ZOrder"] == 3
    assert tree[2]["custom_shape_type"] == "octagon"


def test_build_shape_tree_geometry_missing_property_degrades():
    class _BrokenPosShape(_Shape):
        def getPosition(self) -> Any:
            raise RuntimeError("no geometry")

    tree = build_shape_tree(_Shapes([_BrokenPosShape("RectangleShape")]))
    assert len(tree) == 1
    assert "geometry" not in tree[0]


def test_build_shape_tree_reraises_disposal_from_text_and_type():
    class _DisposedTextShape(_Shape):
        def getString(self) -> str | None:
            raise _DisposedException("text disposed")

    with pytest.raises(DocumentDisposedError):
        build_shape_tree(_Shapes([_DisposedTextShape("TextShape")]))

    class _DisposedTypeShape(_Shape):
        def getShapeType(self) -> str:
            raise _DisposedException("type disposed")

    with pytest.raises(DocumentDisposedError):
        build_shape_tree(_Shapes([_DisposedTypeShape("TextShape")]))


def test_build_shape_tree_control_snapshot_runtime_error_degrades_to_unknown():
    class _CrashingModel:
        @property
        def Text(self) -> str:
            raise RuntimeError("snapshot failure")

    tree = build_shape_tree(_Shapes([_Shape("ControlShape", name="ctrl1", control=_CrashingModel())]))
    assert tree[0]["control"]["type"] == "unknown"
    assert tree[0]["control"]["name"] == "ctrl1"


def test_find_shape_on_page_by_index_name_and_control_name():
    shape0 = _Shape("RectangleShape", name="Rect")
    shape1 = _Shape("TextShape", name="Box")
    ctrl_model = SimpleNamespace(Name="Agree")
    ctrl_shape = _Shape("ControlShape", name="CtrlDifferent", control=ctrl_model)
    page = _Shapes([shape0, shape1, ctrl_shape])

    idx, shape, err = find_shape_on_page(page, index=0)
    assert idx == 0
    assert shape is shape0
    assert err is None

    idx, shape, err = find_shape_on_page(page, index=-1)
    assert idx is None and shape is None
    assert err == "Invalid shape index: -1"

    idx, shape, err = find_shape_on_page(page, index=9)
    assert idx is None and shape is None
    assert err == "Invalid shape index: 9"

    idx, shape, err = find_shape_on_page(page, name="Box")
    assert idx == 1
    assert shape is shape1
    assert err is None

    idx, shape, err = find_shape_on_page(page, name="Agree")
    assert err is None
    assert shape is ctrl_shape


def test_find_shape_on_page_nested_duplicate_and_missing():
    child_shape = _Shape("TextShape", name="Inner")
    group_shape = _Shape("GroupShape", children=[child_shape])
    page_nested = _Shapes([_Shape("RectangleShape"), group_shape])
    _idx, shape, err = find_shape_on_page(page_nested, name="Inner")
    assert err is None
    assert shape is child_shape

    page_dup = _Shapes([_Shape("TextShape", name="Dup"), _Shape("TextShape", name="Dup")])
    idx, shape, err = find_shape_on_page(page_dup, name="Dup")
    assert idx is None and shape is None
    assert err == "Several shapes named 'Dup'; pass index as well."

    idx, shape, err = find_shape_on_page(page_dup, name="Nope")
    assert idx is None and shape is None
    assert err == "No shape named 'Nope' on this page."

    idx, shape, err = find_shape_on_page(page_dup)
    assert idx is None and shape is None
    assert err == "Pass name or index to address the shape."


def test_get_draw_tree_uses_resolve_shape_page(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("plugin.draw.bridge.resolve_shape_page", lambda ctx, kwargs, bridge=None: (object(), 2, None))
    monkeypatch.setattr("plugin.draw.tree.build_shape_tree", lambda page: [])

    res = GetDrawTree().execute(cast("Any", types.SimpleNamespace(doc=object())), page=2)
    assert res == {"status": "ok", "page": 2, "tree": []}


def test_get_draw_tree_page_error_is_tool_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("plugin.draw.bridge.resolve_shape_page", lambda ctx, kwargs, bridge=None: (None, 9, "Invalid page index: 9"))

    out = GetDrawTree().execute(cast("Any", types.SimpleNamespace(doc=object())), page=9)
    assert out["status"] == "error"
    assert out["code"] == "TOOL_EXECUTION_ERROR"
    assert out["message"] == "Invalid page index: 9"

