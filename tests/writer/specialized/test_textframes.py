# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for text frame tools (no LibreOffice required)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugin.framework.errors import DocumentDisposedError
from plugin.writer.specialized.textframes import FrameGetInfo


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
    frame.getAnchor.side_effect = Exception("no anchor")
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
    with pytest.raises(DocumentDisposedError):
        FrameGetInfo().execute(ctx, name="Box")
