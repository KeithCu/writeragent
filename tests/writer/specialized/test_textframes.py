# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for text frame tools (no LibreOffice required)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from plugin.framework.errors import DocumentDisposedError
from plugin.writer.specialized import textframes
from plugin.writer.specialized.textframes import FrameGetInfo, FrameSetProperties


def test_frame_get_info_anchor_type_uses_enum_name():
    frame = MagicMock()
    size = MagicMock()
    size.Width = 1000
    size.Height = 500
    props = {
        "Size": size,
        "AnchorType": SimpleNamespace(value="AT_CHARACTER"),
        "HoriOrient": 3,
        "VertOrient": 1,
    }

    def _get(name: str):
        if name not in props:
            raise Exception(name)
        return props[name]

    frame.getPropertyValue.side_effect = _get
    frames = MagicMock()
    frames.hasByName.return_value = True
    frames.getByName.return_value = frame
    doc = MagicMock()
    doc.getTextFrames.return_value = frames
    ctx = MagicMock()
    ctx.doc = doc
    res = FrameGetInfo().execute(ctx, name="Box")
    assert res["status"] == "ok"
    assert res["anchor_type"] == 4

    props["AnchorType"] = SimpleNamespace(value="AT_PARAGRAPH")
    assert FrameGetInfo().execute(ctx, name="Box")["anchor_type"] == 0

    def _disposed(name: str):
        if name == "AnchorType":
            raise DocumentDisposedError("closed")
        return props[name]

    frame.getPropertyValue.side_effect = _disposed
    res_disposed = FrameGetInfo().execute_safe(ctx, name="Box")
    assert res_disposed["status"] == "error"
    assert res_disposed["code"] == "DOCUMENT_DISPOSED"


class FakeFrame:
    def __init__(self, props: dict[str, Any] | None = None, fail_on: str | None = None):
        size = SimpleNamespace(Width=1000, Height=500)
        self.props: dict[str, Any] = props if props is not None else {
            "Size": size,
            "AnchorType": SimpleNamespace(value="AT_PARAGRAPH"),
            "HoriOrient": 0,
            "VertOrient": 0,
        }
        self.writes: list[tuple[str, Any]] = []
        self.fail_on = fail_on

    def getPropertyValue(self, name: str) -> Any:
        if name not in self.props:
            raise KeyError(name)
        return self.props[name]

    def setPropertyValue(self, name: str, value: Any) -> None:
        if self.fail_on == name:
            raise RuntimeError(f"boom on {name}")
        self.writes.append((name, value))
        self.props[name] = value


def _ctx_for_frame(frame: Any) -> MagicMock:
    frames = MagicMock()
    frames.hasByName.return_value = True
    frames.getByName.return_value = frame
    doc = MagicMock()
    doc.getTextFrames.return_value = frames
    um = MagicMock()
    um.isLocked.return_value = False
    doc.getUndoManager.return_value = um
    ctx = MagicMock()
    ctx.doc = doc
    return ctx


def test_frame_set_properties_validation():
    bad_calls = [
        {"name": "Box", "width_mm": 0},
        {"name": "Box", "width_mm": -1},
        {"name": "Box", "width_mm": float("inf")},
        {"name": "Box", "anchor_type": 5},
        {"name": "Box", "anchor_type": True},
        {"name": "Box", "hori_orient": True},
        {"name": "Box"},
    ]
    tool = FrameSetProperties()
    for bad in bad_calls:
        frame = FakeFrame()
        ctx = _ctx_for_frame(frame)
        res = tool.execute(ctx, **bad)
        assert res["status"] == "error"
        assert len(frame.writes) == 0


def test_frame_set_properties_units():
    frame = FakeFrame()
    ctx = _ctx_for_frame(frame)
    tool = FrameSetProperties()
    res = tool.execute(
        ctx,
        name="Box",
        width_mm=0.29,
        height_mm=None,
        anchor_type=0,
        hori_orient=3,
        vert_orient=1,
    )
    assert res["status"] == "ok"
    assert res["updated"] == ["size", "anchor_type", "hori_orient", "vert_orient"]
    assert len(frame.writes) == 4
    assert frame.writes[0][0] == "Size"
    assert frame.writes[0][1].Width == 29
    assert frame.writes[0][1].Height == 500


def test_frame_set_properties_rollback():
    frame = FakeFrame(fail_on="AnchorType")
    ctx = _ctx_for_frame(frame)
    um = ctx.doc.getUndoManager.return_value
    um.getAllUndoActionTitles.side_effect = [
        (),
        ("WriterAgent: Set text frame properties",),
    ]
    tool = FrameSetProperties()
    res = tool.execute(ctx, name="Box", width_mm=10.0, anchor_type=0)
    assert res["status"] == "error"
    assert res["code"] == "SET_PROPERTIES_FAILED"
    um.undo.assert_called_once()


def test_frame_text_uses_visible_text(monkeypatch):
    seen = []
    monkeypatch.setattr(
        textframes,
        "get_string_without_tracked_deletions",
        lambda text: seen.append(text) or "visible",
    )
    frame = MagicMock()
    assert textframes._frame_text(frame) == "visible"
    assert seen == [frame.getText()]


def test_frame_get_info_truncation_and_non_body_anchor(monkeypatch):
    monkeypatch.setattr(textframes, "_frame_text", lambda frame: "x" * 5000)
    frame = MagicMock()
    size = MagicMock()
    size.Width = 1000
    size.Height = 500
    props = {
        "Size": size,
        "AnchorType": SimpleNamespace(value="AT_CHARACTER"),
        "HoriOrient": 3,
        "VertOrient": 1,
    }
    frame.getPropertyValue.side_effect = lambda name: props[name]
    anchor = MagicMock()
    body_text = MagicMock()
    anchor.getText.return_value = MagicMock()  # distinct from body_text
    frame.getAnchor.return_value = anchor

    frames = MagicMock()
    frames.hasByName.return_value = True
    frames.getByName.return_value = frame
    doc = MagicMock()
    doc.getTextFrames.return_value = frames
    doc.getText.return_value = body_text

    ctx = MagicMock()
    ctx.doc = doc
    res = FrameGetInfo().execute(ctx, name="Box")
    assert res["status"] == "ok"
    assert len(res["content"]) == 4000
    assert res["content_length"] == 5000
    assert res["truncated"] is True
    assert res["paragraph_index"] is None
    ctx.services.document.get_paragraph_ranges.assert_not_called()


def test_frame_get_info_body_anchor(monkeypatch):
    monkeypatch.setattr(textframes, "_frame_text", lambda frame: "visible")
    frame = MagicMock()
    size = MagicMock()
    size.Width = 1000
    size.Height = 500
    props = {
        "Size": size,
        "AnchorType": SimpleNamespace(value="AT_PARAGRAPH"),
        "HoriOrient": 0,
        "VertOrient": 0,
    }
    frame.getPropertyValue.side_effect = lambda name: props[name]
    same_text = MagicMock()
    frame.getAnchor.return_value.getText.return_value = same_text

    frames = MagicMock()
    frames.hasByName.return_value = True
    frames.getByName.return_value = frame
    doc = MagicMock()
    doc.getTextFrames.return_value = frames
    doc.getText.return_value = same_text

    ctx = MagicMock()
    ctx.doc = doc
    ctx.services.document.get_paragraph_ranges.return_value = []
    ctx.services.document.find_paragraph_for_range.return_value = 2

    res = FrameGetInfo().execute(ctx, name="Box")
    assert res["status"] == "ok"
    assert res["paragraph_index"] == 2
    ctx.services.document.get_paragraph_ranges.assert_called_once()

    ctx.services.document.find_paragraph_for_range.return_value = None
    res2 = FrameGetInfo().execute(ctx, name="Box")
    assert res2["paragraph_index"] is None
