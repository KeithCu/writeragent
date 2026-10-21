# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for fill_draw_fields resolution and value apply (no soffice)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.draw.field_fill import FillDrawFields, apply_fill_value, resolve_field_node


def _tree():
    return [
        {"type": "TextShape", "index": 0, "name": "lbl_product", "text": "Product:"},
        {
            "type": "TextShape",
            "index": 1,
            "name": "fld_product",
            "fillable": True,
            "label_hint": "Product:",
        },
        {
            "type": "ControlShape",
            "index": 3,
            "name": "AgreeBox",
            "control": {"type": "checkbox", "name": "AgreeBox", "state": 0},
        },
    ]


def test_resolve_field_by_name_index_and_label_hint():
    tree = _tree()
    node, err = resolve_field_node(tree, name="fld_product")
    assert err is None
    assert node is not None
    assert node["index"] == 1

    node, err = resolve_field_node(tree, index=3)
    assert err is None
    assert node is not None
    assert node["name"] == "AgreeBox"

    node, err = resolve_field_node(tree, label_hint="Product:")
    assert err is None
    assert node is not None
    assert node["name"] == "fld_product"


def test_resolve_field_miss_and_ambiguous_hint():
    tree = _tree()
    node, err = resolve_field_node(tree, name="missing")
    assert node is None
    assert err is not None
    assert "No shape named" in err

    tree[0]["fillable"] = True
    tree[0]["label_hint"] = "Product:"
    node, err = resolve_field_node(tree, label_hint="Product:")
    assert node is None
    assert err is not None
    assert "Several" in err


def test_apply_fill_value_setstring_and_checkbox_state():
    shape = MagicMock()
    shape.getShapeType.return_value = "com.sun.star.drawing.TextShape"
    ok, detail = apply_fill_value(shape, "Widget-7")
    assert ok
    shape.setString.assert_called_once_with("Widget-7")
    assert "text" in detail

    class _CheckModel:
        State = 0

    class _CheckShape:
        Control = _CheckModel()

        def getShapeType(self):
            return "com.sun.star.drawing.ControlShape"

    box = _CheckShape()
    ok, detail = apply_fill_value(box, "yes")
    assert ok
    assert box.Control.State == 1
    assert "state" in detail

    # Bad state input expects checkbox/radio in error message
    bad_box = _CheckShape()
    ok_bad, detail_bad = apply_fill_value(bad_box, "invalid_val")
    assert not ok_bad
    assert "checkbox/radio" in detail_bad


def test_apply_fill_value_prefers_text_over_state():
    class _TextAndStateModel:
        Text = "original"
        State = 0

    class _TextAndStateShape:
        Control = _TextAndStateModel()

        def getShapeType(self):
            return "com.sun.star.drawing.ControlShape"

    shape = _TextAndStateShape()
    ok, detail = apply_fill_value(shape, "new text")
    assert ok
    assert shape.Control.Text == "new text"
    assert shape.Control.State == 0
    assert "text" in detail


def test_fill_draw_fields_happy_and_miss_on_mock_page():
    page = MagicMock()
    blank = MagicMock()
    blank.getShapeType.return_value = "com.sun.star.drawing.TextShape"
    blank.Name = "fld_product"
    blank.getString.return_value = ""
    page.getCount.return_value = 2
    page.getByIndex.side_effect = lambda i: blank if i == 1 else MagicMock(Name="other", getShapeType=lambda: "com.sun.star.drawing.TextShape")

    tree = _tree()
    ctx = MagicMock()
    ctx.active_page_index = 0
    ctx.doc = MagicMock()

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls, patch("plugin.draw.field_fill.build_shape_tree", return_value=tree):
        bridge_cls.return_value.get_pages.return_value.getByIndex.return_value = page
        out = FillDrawFields().execute(
            ctx,
            fields=[
                {"name": "fld_product", "value": "Widget-7"},
                {"name": "no_such_field", "value": "x"},
            ],
        )
    assert out["status"] == "partial"
    assert out["ok_count"] == 1
    assert out["fail_count"] == 1
    assert out["results"][0]["ok"] is True
    assert out["results"][1]["ok"] is False
    blank.setString.assert_called_once_with("Widget-7")
