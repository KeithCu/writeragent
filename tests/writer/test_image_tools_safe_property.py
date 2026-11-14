# WriterAgent - safe UNO property helpers for image_tools

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin.writer.images import image_tools


class _FakePropertySetInfo:
    def __init__(self, supported: set[str]) -> None:
        self._supported = supported

    def hasPropertyByName(self, name: str) -> bool:
        return name in self._supported


class _FakeUnoGraphic:
    """Fake UNO graphic where hasattr() disagrees with PropertySetInfo."""

    # hasattr(obj, "GraphicURL") is True even though GraphicURL is not a UNO property.
    GraphicURL = "trap"

    def __init__(self, supported: set[str]) -> None:
        self._psi = _FakePropertySetInfo(supported)
        self.set_calls: list[tuple[str, object]] = []

    def getPropertySetInfo(self) -> _FakePropertySetInfo:
        return self._psi

    def setPropertyValue(self, name: str, value: object) -> None:
        self.set_calls.append((name, value))


def test_has_uno_property_uses_property_set_info():
    # "Graphic" is in PropertySetInfo (hasattr False); "GraphicURL" is on the class (hasattr True).
    obj = _FakeUnoGraphic({"Graphic"})
    assert hasattr(obj, "Graphic") is False
    assert hasattr(obj, "GraphicURL") is True
    assert image_tools.visual_helpers.has_uno_property(obj, "Graphic") is True
    assert image_tools.visual_helpers.has_uno_property(obj, "GraphicURL") is False


def test_create_embedded_graphic_uses_safe_set_not_hasattr(monkeypatch):
    graphic = _FakeUnoGraphic({"Graphic"})
    xgraphic = SimpleNamespace(tag="loaded-xgraphic")

    doc = MagicMock()
    doc.createInstance.return_value = graphic

    monkeypatch.setattr(image_tools, "_graphic_from_provider", lambda ctx, url: xgraphic)

    result = image_tools._create_embedded_graphic(doc, "writer", "file:///tmp/x.png", ctx=MagicMock())
    assert result is graphic
    # Must fall back to Graphic via PropertySetInfo rather than writing the hasattr GraphicURL trap.
    assert graphic.GraphicURL == "trap"
    assert graphic.set_calls == [("Graphic", xgraphic)]


def test_apply_graphic_properties_skips_missing_title(monkeypatch):
    class FakeSize:
        def __init__(self, w, h):
            self.Width = w
            self.Height = h

    monkeypatch.setattr(image_tools, "Size", FakeSize)

    graphic = MagicMock()
    psi = MagicMock()

    def has_prop(name):
        return name in ("AnchorType", "Width", "Height")

    psi.hasPropertyByName.side_effect = has_prop
    graphic.getPropertySetInfo.return_value = psi

    image_tools._apply_graphic_properties(
        graphic,
        width=1000,
        height=800,
        title="Notebook output",
        description="image/png",
    )

    set_calls = [c[0][0] for c in graphic.setPropertyValue.call_args_list]
    assert "AnchorType" in set_calls
    assert "Width" in set_calls
    assert "Height" in set_calls
    assert "Title" not in set_calls
    assert "Description" not in set_calls
