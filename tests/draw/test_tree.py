# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Draw tree blank detection, label hints, and control snapshots."""

from __future__ import annotations

from plugin.draw.tree import (
    attach_label_hints,
    coerce_control_state,
    control_snapshot,
    is_near_empty_text,
    is_text_capable_shape_type,
    nearest_label_hint,
)


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


