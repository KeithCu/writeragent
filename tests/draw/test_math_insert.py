# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
from types import SimpleNamespace

from plugin.draw.math_insert import _ole_visual_object, _uno_ctx_from_tool_ctx


def test_uno_ctx_from_tool_ctx_uses_inner_ctx() -> None:
    inner = object()
    assert _uno_ctx_from_tool_ctx(SimpleNamespace(ctx=inner)) is inner


def test_uno_ctx_from_tool_ctx_passes_bare_object() -> None:
    bare = object()
    assert _uno_ctx_from_tool_ctx(bare) is bare


class _Model:
    def getVisualAreaSize(self, aspect: int) -> tuple[int, int]:
        return (aspect, 0)

    def getMapUnit(self, aspect: int) -> int:
        return aspect


class _Shape:
    def __init__(self, model: object | None) -> None:
        self.Model = model


def test_ole_visual_object_is_the_model() -> None:
    # Draw OLE2Shape has no getEmbeddedObject. The formula document is Model.
    model = _Model()
    assert _ole_visual_object(_Shape(model)) is model


def test_ole_visual_object_missing_model() -> None:
    assert _ole_visual_object(_Shape(None)) is None
    assert _ole_visual_object(object()) is None
