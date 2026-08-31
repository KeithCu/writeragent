# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Draw/Impress shape create (no live soffice)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.draw.shapes import UpsertShape, _ENHANCED_CUSTOM_SHAPE_ENGINE


class _Pos:
    def __init__(self, x, y):
        self.X = x
        self.Y = y


class _Size:
    def __init__(self, w, h):
        self.Width = w
        self.Height = h


class _RecordingShape:
    """Shape stand-in that records property sets for order assertions."""

    def __init__(self, events: list):
        self._events = events
        self._props: dict = {}

    def setPosition(self, position):
        self._events.append(("setPosition", position.X, position.Y))

    def setSize(self, size):
        self._events.append(("setSize", size.Width, size.Height))

    def setPropertyValue(self, name, value):
        self._events.append(("setPropertyValue", name, value))
        self._props[name] = value

    def getPropertyValue(self, name):
        return self._props.get(name)

    def getShapeType(self):
        return "com.sun.star.drawing.CustomShape"

    def getPosition(self):
        return _Pos(1000, 1000)

    def getSize(self):
        return _Size(5000, 5000)

    def getPropertySetInfo(self):
        info = MagicMock()
        info.getProperties.return_value = []
        return info


def _invoke_set_property(obj, method, args):
    """Stand-in for ``uno.invoke(shape, 'setPropertyValue', (name, value))``."""
    getattr(obj, method)(*args)


def test_shape_upsert_octagon_sets_geometry_before_page_add():
    """CustomShape Type/engine must be set before page.add; never again after.

    Post-add CustomShapeGeometry Type=octagon replaces the live SdrRectObj and
    can abort soffice (SfxItemPool::unregisterNameOrIndex).
    """
    events: list = []
    shape = _RecordingShape(events)

    page = MagicMock()
    page_shapes: list = []

    def page_add(added):
        events.append(("page.add",))
        page_shapes.append(added)

    page.add.side_effect = page_add
    page.getCount.side_effect = lambda: len(page_shapes)
    page.getByIndex.side_effect = lambda i: page_shapes[i]

    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page

    doc = MagicMock()
    doc.supportsService.return_value = False
    doc.createInstance.side_effect = lambda _type: shape

    ctx = MagicMock()
    ctx.doc = doc
    ctx.active_page_index = 0

    with (
        patch("plugin.draw.bridge.DrawBridge") as bridge_cls,
        patch("uno.invoke", side_effect=_invoke_set_property),
        patch("uno.Any", side_effect=lambda _type, value: value),
    ):
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value = pages
        bridge.get_active_page_index.return_value = 0

        result = UpsertShape().execute(
            ctx,
            action="create",
            shape_type="octagon",
            x=1000,
            y=1000,
            width=5000,
            height=5000,
            fill_color="none",
            line_color="black",
            line_width=100,
        )

    assert result["status"] == "ok"
    assert result["geometry_applied"] is True
    assert "warning" not in result
    doc.createInstance.assert_called_once_with("com.sun.star.drawing.CustomShape")
    page.add.assert_called_once_with(shape)

    add_idx = next(i for i, ev in enumerate(events) if ev[0] == "page.add")
    engine_idxs = [i for i, ev in enumerate(events) if ev[0] == "setPropertyValue" and ev[1] == "CustomShapeEngine"]
    geom_idxs = [i for i, ev in enumerate(events) if ev[0] == "setPropertyValue" and ev[1] == "CustomShapeGeometry"]

    assert engine_idxs, events
    assert geom_idxs, events
    assert engine_idxs[0] < add_idx, events
    assert geom_idxs[0] < add_idx, events
    assert events[engine_idxs[0]][2] == _ENHANCED_CUSTOM_SHAPE_ENGINE
    geom_value = events[geom_idxs[0]][2]
    geom_props = geom_value if isinstance(geom_value, tuple) else (geom_value,)
    assert any(getattr(p, "Name", None) == "Type" and getattr(p, "Value", None) == "octagon" for p in geom_props), events

    # Double-apply after add is the crashy swap; fill/line may still set after add.
    assert not any(i > add_idx for i in engine_idxs), events
    assert not any(i > add_idx for i in geom_idxs), events
