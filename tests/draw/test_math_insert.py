# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
from types import SimpleNamespace
from typing import Any

import pytest

from plugin.draw.math_insert import (
    _DISPLAY_BLOCK_RE,
    _MAP_ONE_CM,
    _MAP_ONE_MM,
    InsertMathDraw,
    _content_size_hmm,
    _try_ole_shape_content_size_hmm,
)
from plugin.framework.errors import DocumentDisposedError
from plugin.writer.math.math_mml_convert import MathConversionResult


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


def test_positive_awt_size() -> None:
    assert _content_size_hmm(None) is None
    assert _content_size_hmm(_AwtSize(0, 10)) is None
    assert _content_size_hmm(_AwtSize(10, 0)) is None
    assert _content_size_hmm(_AwtSize(-5, 10)) is None
    assert _content_size_hmm(_AwtSize(100, 200)) == (100, 200)


@pytest.mark.parametrize(
    ("width", "height", "map_unit", "expected"),
    [
        (1, 2, _MAP_ONE_MM, (100, 200)),
        (1, 2, _MAP_ONE_CM, (1000, 2000)),
        (1, 2, 9999, None),
    ],
)
def test_content_size_hmm_map_units(width: int, height: int, map_unit: int, expected: tuple[int, int] | None) -> None:
    assert _content_size_hmm(_AwtSize(width, height), map_unit) == expected


@pytest.mark.parametrize(
    ("latex", "expected"),
    [
        (r"\intercal", False),
        (r"x^2", False),
        (r"\frac{a}{b}", False),
        (r"\int_0^1", True),
        (r"\iint", True),
        (r"\oint", True),
        (r"\displaystyle x", True),
    ],
)
def test_latex_is_display_block(latex: str, expected: bool) -> None:
    assert bool(_DISPLAY_BLOCK_RE.search(latex)) is expected


class _FakePages:
    def __init__(self, pages: list[Any]) -> None:
        self._pages = pages

    def getCount(self) -> int:
        return len(self._pages)

    def getByIndex(self, idx: int) -> Any:
        return self._pages[idx]


class _FakeDoc:
    def __init__(self, pages: list[Any]) -> None:
        self._draw_pages = _FakePages(pages)

    def getDrawPages(self) -> Any:
        return self._draw_pages


def test_insert_math_invalid_page_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "plugin.draw.math_insert.convert_latex_to_starmath",
        lambda *args, **kwargs: MathConversionResult(True, "x", None),
    )
    ctx = SimpleNamespace(doc=_FakeDoc([]), active_page_index=0)
    res = InsertMathDraw().execute(ctx, formula_type="latex", formula="x^2", page=5, x=0, y=0)
    assert res["status"] == "error"
    assert "Invalid page index: 5" in res["message"]


def test_insert_math_disposal_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "plugin.draw.math_insert.convert_latex_to_starmath",
        lambda *args, **kwargs: MathConversionResult(True, "x", None),
    )

    class _DisposedPages:
        def getCount(self) -> int:
            return 1

        def getByIndex(self, idx: int) -> Any:
            raise DocumentDisposedError("disposed")

    class _DocWithDisposedPages:
        def getDrawPages(self) -> Any:
            return _DisposedPages()

    ctx = SimpleNamespace(doc=_DocWithDisposedPages(), active_page_index=0)
    with pytest.raises(DocumentDisposedError):
        InsertMathDraw().execute(ctx, formula_type="latex", formula="x^2", page=0, x=0, y=0)
