# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
from types import SimpleNamespace

from plugin.draw.math_insert import _try_ole_shape_content_size_hmm, _uno_ctx_from_tool_ctx


def test_uno_ctx_from_tool_ctx_uses_inner_ctx() -> None:
    inner = object()
    assert _uno_ctx_from_tool_ctx(SimpleNamespace(ctx=inner)) is inner


def test_uno_ctx_from_tool_ctx_passes_bare_object() -> None:
    bare = object()
    assert _uno_ctx_from_tool_ctx(bare) is bare


class _AwtSize:
    def __init__(self, width: int, height: int) -> None:
        self.Width = width
        self.Height = height


class _ContentEmbed:
    """Stand-in for OLE2Shape.EmbeddedObject (XVisualObject)."""

    def getVisualAreaSize(self, aspect: int) -> _AwtSize:
        assert aspect == 1
        # 1in x 0.5in in twips. 1440 twip = 2540 hundredths of a mm.
        return _AwtSize(1440, 720)

    def getMapUnit(self, aspect: int) -> int:
        assert aspect == 1
        return 9  # EmbedMapUnits.TWIP


def test_content_size_uses_embedded_object_property_not_get_embedded_object() -> None:
    # Draw OLE2Shape has no getEmbeddedObject(); that call used to return
    # None and insert_math always fell through to the length heuristic.
    calls = {"getEmbeddedObject": 0}

    class Shape:
        EmbeddedObject = _ContentEmbed()

        def getEmbeddedObject(self) -> None:
            calls["getEmbeddedObject"] += 1
            return None

    assert _try_ole_shape_content_size_hmm(Shape()) == (2540, 1270)
    assert calls["getEmbeddedObject"] == 0


def test_content_size_reads_visible_area_when_embed_is_missing() -> None:
    class Shape:
        VisibleArea = _AwtSize(8000, 2400)

        def getEmbeddedObject(self) -> None:
            raise AssertionError("getEmbeddedObject is not a Draw OLE2Shape API")

    assert _try_ole_shape_content_size_hmm(Shape()) == (8000, 2400)


def test_content_size_is_none_without_draw_size_properties() -> None:
    class Shape:
        def getEmbeddedObject(self) -> None:
            return None

    assert _try_ole_shape_content_size_hmm(Shape()) is None
