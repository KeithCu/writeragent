# WriterAgent - AI Writing Assistant for LibreOffice
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for shared visual UNO helper functions."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from plugin.doc import visual_helpers
from plugin.doc.doc_type import (
    CALC_DOCUMENT_SERVICE,
    DRAW_DOCUMENT_SERVICE,
    IMPRESS_DOCUMENT_SERVICE,
    WEB_DOCUMENT_SERVICE,
    WRITER_DOCUMENT_SERVICE,
)


class FakePropertyInfo:
    def __init__(self, names: set[str]):
        self._names = names

    def hasPropertyByName(self, name: str) -> bool:
        return name in self._names


class FakePropertyObject:
    def __init__(self, properties: dict[str, object], services: set[str] | None = None):
        self._properties = dict(properties)
        self._services = services or set()
        self.set_calls: list[tuple[str, object]] = []

    def getPropertySetInfo(self):
        return FakePropertyInfo(set(self._properties))

    def getPropertyValue(self, name: str):
        if name not in self._properties:
            raise KeyError(name)
        return self._properties[name]

    def setPropertyValue(self, name: str, value: object) -> None:
        self.set_calls.append((name, value))
        self._properties[name] = value

    def supportsService(self, service: str) -> bool:
        return service in self._services


class FakeGraphicShape(FakePropertyObject):
    def __init__(self, name: str):
        super().__init__({"Graphic": object()}, {visual_helpers.DRAW_GRAPHIC_SERVICE})
        self._name = name

    def getName(self) -> str:
        return self._name


class FakeDrawPage:
    def __init__(self, shapes: list[object]):
        self._shapes = shapes

    def getCount(self) -> int:
        return len(self._shapes)

    def getByIndex(self, index: int):
        return self._shapes[index]

    def remove(self, shape: object):
        self._shapes.remove(shape)


class FakeSelection:
    def __init__(self, items: list[object]):
        self._items = items

    def getCount(self) -> int:
        return len(self._items)

    def getByIndex(self, index: int):
        return self._items[index]


class FakeController:
    def __init__(self, *, selection: object | None = None, active_sheet: object | None = None, current_page: object | None = None):
        self.Selection = selection
        self.ActiveSheet = active_sheet
        self.CurrentPage = current_page

    def getSelection(self):
        return self.Selection

    def getActiveSheet(self):
        return self.ActiveSheet

    def getCurrentPage(self):
        return self.CurrentPage


class FakeGraphicCollection:
    def __init__(self, graphics: dict[str, object]):
        self._graphics = graphics

    def getElementNames(self):
        return tuple(self._graphics)

    def getByName(self, name: str):
        return self._graphics[name]


class FakeWriterDoc:
    CurrentController = None

    def __init__(self, graphics: dict[str, object], *, draw_page: FakeDrawPage | None = None):
        self._graphics = graphics
        self._draw_page = draw_page

    def supportsService(self, service: str) -> bool:
        return service == WRITER_DOCUMENT_SERVICE

    def getGraphicObjects(self):
        return FakeGraphicCollection(self._graphics)

    def getDrawPage(self):
        if self._draw_page is None:
            raise AttributeError("no draw page")
        return self._draw_page


class FakeSheet:
    def __init__(self, draw_page: FakeDrawPage, name: str | None = None):
        self.DrawPage = draw_page
        self.Name = name

    def getDrawPage(self):
        return self.DrawPage


class FakeSheets:
    def __init__(self, sheets: list[FakeSheet]):
        self._sheets = sheets

    def getCount(self) -> int:
        return len(self._sheets)

    def getByIndex(self, index: int) -> FakeSheet:
        return self._sheets[index]


class FakeCalcDoc:
    def __init__(self, draw_page: FakeDrawPage):
        self.CurrentController = FakeController(active_sheet=FakeSheet(draw_page))

    def supportsService(self, service: str) -> bool:
        return service == CALC_DOCUMENT_SERVICE


class FakeDrawDoc:
    def __init__(self, draw_page: FakeDrawPage):
        self.CurrentController = FakeController(current_page=draw_page)

    def supportsService(self, service: str) -> bool:
        return service == DRAW_DOCUMENT_SERVICE


class FakeImpressDoc:
    """PresentationDocument only — not DrawingDocument (isolates impress label)."""

    def supportsService(self, service: str) -> bool:
        return service == IMPRESS_DOCUMENT_SERVICE


class FakeWebDoc(FakeWriterDoc):
    def __init__(self, graphics: dict[str, object] | None = None, *, draw_page: FakeDrawPage | None = None):
        super().__init__(graphics or {}, draw_page=draw_page)

    def supportsService(self, service: str) -> bool:
        return service in (WEB_DOCUMENT_SERVICE, WRITER_DOCUMENT_SERVICE)


def test_safe_uno_property_helpers_use_property_set_info():
    obj = FakePropertyObject({"GraphicURL": "file:///tmp/a.png"})

    assert visual_helpers.has_uno_property(obj, "GraphicURL") is True
    assert visual_helpers.has_uno_property(obj, "Title") is False
    assert visual_helpers.safe_set_property(obj, "Title", "ignored") is False
    assert visual_helpers.safe_set_property(obj, "GraphicURL", "file:///tmp/b.png") is True
    assert obj.getPropertyValue("GraphicURL") == "file:///tmp/b.png"


def test_selected_graphic_object_handles_selection_containers():
    graphic = FakeGraphicShape("Image 1")
    controller = FakeController(selection=FakeSelection([graphic]))
    model = type("FakeModel", (), {"CurrentController": controller})()

    assert visual_helpers.selected_graphic_object(model) is graphic


def test_selected_graphic_object_rejects_multi_selection():
    graphic = FakeGraphicShape("Image 1")
    controller = FakeController(selection=FakeSelection([graphic, graphic]))
    model = type("FakeModel", (), {"CurrentController": controller})()

    assert visual_helpers.selected_graphic_object(model) is None


class FakeTextRange:
    """Minimal XTextRange stand-in with compareRegionStarts-based containment."""

    def __init__(self, text: "FakeText", start: int, end: int):
        self._text = text
        self._start = start
        self._end = end

    def getText(self):
        return self._text

    def getStart(self):
        return FakeTextRange(self._text, self._start, self._start)

    def getEnd(self):
        return FakeTextRange(self._text, self._end, self._end)


class FakeText:
    def compareRegionStarts(self, a: FakeTextRange, b: FakeTextRange) -> int:
        if a._start < b._start:
            return 1
        if a._start > b._start:
            return -1
        return 0


class FakeAnchoredGraphic(FakePropertyObject):
    def __init__(self, name: str, text: FakeText, pos: int):
        super().__init__({"Graphic": object()}, {visual_helpers.WRITER_GRAPHIC_SERVICE})
        self._name = name
        self._anchor = FakeTextRange(text, pos, pos)

    def getName(self) -> str:
        return self._name

    def getAnchor(self):
        return self._anchor


def test_graphic_objects_in_selection_multi_graphics():
    text = FakeText()
    g1 = FakeAnchoredGraphic("Image1", text, 10)
    g2 = FakeAnchoredGraphic("Image2", text, 30)
    controller = FakeController(selection=FakeSelection([g2, g1]))
    doc = FakeWriterDoc({"Image1": g1, "Image2": g2})
    doc.CurrentController = controller

    pairs = visual_helpers.graphic_objects_in_selection(doc)
    assert [n for n, _ in pairs] == ["Image1", "Image2"]


def test_graphic_objects_in_selection_text_range_contains_images():
    text = FakeText()
    g1 = FakeAnchoredGraphic("Image1", text, 10)
    g2 = FakeAnchoredGraphic("Image2", text, 40)
    outside = FakeAnchoredGraphic("Outside", text, 100)
    sel = FakeTextRange(text, 0, 50)
    controller = FakeController(selection=sel)
    doc = FakeWriterDoc({"Image1": g1, "Image2": g2, "Outside": outside})
    doc.CurrentController = controller

    pairs = visual_helpers.graphic_objects_in_selection(doc)
    assert [n for n, _ in pairs] == ["Image1", "Image2"]


def test_graphic_objects_in_selection_single_graphic():
    graphic = FakeGraphicShape("Only")
    controller = FakeController(selection=FakeSelection([graphic]))
    doc = FakeWriterDoc({"Only": graphic})
    doc.CurrentController = controller

    pairs = visual_helpers.graphic_objects_in_selection(doc)
    assert pairs == [("Only", graphic)]


def test_graphic_objects_in_selection_empty():
    controller = FakeController(selection=None)
    doc = FakeWriterDoc({})
    doc.CurrentController = controller
    assert visual_helpers.graphic_objects_in_selection(doc) == []


def test_graphic_objects_in_selection_calc_single_only():
    g1 = FakeGraphicShape("A")
    g2 = FakeGraphicShape("B")
    doc = FakeCalcDoc(FakeDrawPage([g1, g2]))
    doc.CurrentController.Selection = FakeSelection([g1, g2])
    # Multi-select rejected for Calc — selected_graphic_object returns None.
    assert visual_helpers.graphic_objects_in_selection(doc) == []

    doc.CurrentController.Selection = FakeSelection([g1])
    assert visual_helpers.graphic_objects_in_selection(doc) == [("A", g1)]


def test_graphic_objects_in_selection_web_doc():
    graphic = FakeGraphicShape("WebImage")
    controller = FakeController(selection=FakeSelection([graphic]))
    doc = FakeWebDoc({"WebImage": graphic})
    doc.CurrentController = controller

    assert visual_helpers.get_visual_doc_type(doc) == "web"
    pairs = visual_helpers.graphic_objects_in_selection(doc)
    assert pairs == [("WebImage", graphic)]


def test_active_draw_page_resolves_calc_sheet_and_draw_current_page():
    calc_page = FakeDrawPage([])
    draw_page = FakeDrawPage([])

    assert visual_helpers.get_active_draw_page(FakeCalcDoc(calc_page), "calc") is calc_page
    assert visual_helpers.get_active_draw_page(FakeDrawDoc(draw_page), "draw") is draw_page


def test_active_draw_page_resolves_writer_get_draw_page():
    page = FakeDrawPage([])
    doc = FakeWriterDoc({}, draw_page=page)
    assert visual_helpers.get_active_draw_page(doc) is page
    assert visual_helpers.get_active_draw_page(FakeWriterDoc({})) is None


def test_list_graphic_objects_reads_writer_graphic_collection():
    graphic = FakePropertyObject({"Graphic": object()}, {visual_helpers.WRITER_GRAPHIC_SERVICE})
    doc = FakeWriterDoc({"Image 1": graphic})

    assert visual_helpers.get_visual_doc_type(doc) == "writer"
    assert visual_helpers.list_graphic_objects(doc) == [("Image 1", graphic)]
    assert visual_helpers.get_graphic_object_by_name(doc, "Image 1") is graphic


def test_list_graphic_objects_reads_all_draw_pages():
    a = FakeGraphicShape("A")
    b = FakeGraphicShape("B")

    class Pages:
        def __init__(self, pages):
            self._pages = pages

        def getCount(self):
            return len(self._pages)

        def getByIndex(self, i):
            return self._pages[i]

    doc = FakeDrawDoc(FakeDrawPage([a]))
    doc.getDrawPages = lambda: Pages([FakeDrawPage([a]), FakeDrawPage([b])])  # type: ignore[method-assign]
    names = [n for n, _g in visual_helpers.list_graphic_objects(doc)]
    assert names == ["A", "B"]


def test_list_graphic_objects_reads_calc_draw_page_graphic_shapes():
    graphic = FakeGraphicShape("Calc Image")
    other = FakePropertyObject({}, set())
    doc = FakeCalcDoc(FakeDrawPage([other, graphic]))

    assert visual_helpers.get_visual_doc_type(doc) == "calc"
    assert visual_helpers.list_graphic_objects(doc) == [("Calc Image", graphic)]
    assert visual_helpers.get_graphic_object_by_name(doc, "Calc Image") is graphic


def test_calc_multi_sheet_images():
    sheet0_dp = FakeDrawPage([])
    sheet0 = FakeSheet(sheet0_dp, name="Sheet1")
    graphic1 = FakeGraphicShape("Logo")
    sheet1_dp = FakeDrawPage([graphic1])
    sheet1 = FakeSheet(sheet1_dp, name="Sheet2")

    doc = FakeCalcDoc(FakeDrawPage([]))
    active_sheet_before = doc.CurrentController.getActiveSheet()
    doc.getSheets = lambda: FakeSheets([sheet0, sheet1])  # type: ignore[attr-defined]

    # Two sheets, graphic only on sheet 1, list returns it, active sheet unchanged
    items = visual_helpers.list_graphic_objects(doc)
    assert len(items) == 1
    assert items[0][0] == "Logo"
    assert items[0][1] is graphic1
    assert getattr(items[0], "sheet_index", None) == 1
    assert getattr(items[0], "sheet_name", None) == "Sheet2"
    assert doc.CurrentController.getActiveSheet() is active_sheet_before

    # Graphic by name returns it when unique
    assert visual_helpers.get_graphic_object_by_name(doc, "Logo") is graphic1
    assert visual_helpers.get_graphic_object_by_name(doc, "Logo", sheet_index=1) is graphic1
    assert visual_helpers.get_graphic_object_by_name(doc, "Logo", sheet_index=0) is None

    # Duplicate names without sheet_index are ambiguous
    graphic0 = FakeGraphicShape("Logo")
    sheet0_dp._shapes.append(graphic0)
    with pytest.raises(visual_helpers.AmbiguousGraphicNameError) as exc_info:
        visual_helpers.get_graphic_object_by_name(doc, "Logo")
    assert exc_info.value.sheet_indices == [0, 1]

    # With sheet_index, the named sheet wins
    assert visual_helpers.get_graphic_object_by_name(doc, "Logo", sheet_index=0) is graphic0
    assert visual_helpers.get_graphic_object_by_name(doc, "Logo", sheet_index=1) is graphic1

    # remove_graphic_from_draw_pages removes a shape that is not on the active sheet
    assert graphic1 in sheet1_dp._shapes
    assert visual_helpers.remove_graphic_from_draw_pages(doc, graphic1) is True
    assert graphic1 not in sheet1_dp._shapes


def test_get_visual_doc_type_web_before_writer():
    assert visual_helpers.get_visual_doc_type(FakeWebDoc()) == "web"


def test_get_visual_doc_type_draw_and_impress():
    assert visual_helpers.get_visual_doc_type(FakeDrawDoc(FakeDrawPage([]))) == "draw"
    assert visual_helpers.get_visual_doc_type(FakeImpressDoc()) == "impress"


def test_unit_conversions_match_existing_image_tool_assumptions():
    assert visual_helpers.mm_to_units(12.9, 3.1) == (1290, 310)
    assert visual_helpers.mm_to_unit(12.7) == 1270
    assert visual_helpers.mm_to_unit(12.75) == 1275
    assert visual_helpers.mm_to_units(-1.2, 0) == (-120, 0)
    assert visual_helpers.px_to_units(10, 20) == (264, 529)
    assert visual_helpers.units_to_px(2540, 1270) == (96, 48)


def test_px_to_display_units_caps_longer_edge_at_135mm():
    """Generate pixels must not become page mm: 1024px @ 96 DPI is ~10.7\"."""
    raw_w, raw_h = visual_helpers.px_to_units(1024, 1024)
    assert max(raw_w, raw_h) > 13500
    disp_w, disp_h = visual_helpers.px_to_display_units(1024, 1024)
    assert max(disp_w, disp_h) == visual_helpers.GENERATED_IMAGE_MAX_DISPLAY_MM * 100
    assert disp_w == disp_h


def test_px_to_display_units_preserves_aspect_and_does_not_upscale():
    wide_w, wide_h = visual_helpers.px_to_display_units(1536, 768)
    assert wide_w == visual_helpers.GENERATED_IMAGE_MAX_DISPLAY_MM * 100
    assert wide_h == 6750  # 13500 * (768/1536)
    small_w, small_h = visual_helpers.px_to_display_units(256, 256)
    assert (small_w, small_h) == visual_helpers.px_to_units(256, 256)
    assert max(small_w, small_h) < 13500


def test_parse_color_to_uno_int_hex_and_int() -> None:
    assert visual_helpers.parse_color_to_uno_int("#FF0000") == 0xFF0000
    assert visual_helpers.parse_color_to_uno_int("00FF00") == 0x00FF00
    assert visual_helpers.parse_color_to_uno_int("#0f0") == 0x00FF00
    assert visual_helpers.parse_color_to_uno_int("0x0000FF") == 0x0000FF
    assert visual_helpers.parse_color_to_uno_int(0x112233) == 0x112233
    assert visual_helpers.parse_color_to_uno_int(0x1AABBCC) == 0xAABBCC


def test_parse_color_to_uno_int_names_rgb_tuple() -> None:
    assert visual_helpers.parse_color_to_uno_int("red") == 0xFF0000
    assert visual_helpers.parse_color_to_uno_int("Dark Green") == 0x006400
    assert visual_helpers.parse_color_to_uno_int("rgb(1, 2, 3)") == 0x010203
    assert visual_helpers.parse_color_to_uno_int("rgba(10, 20, 30, 0.5)") == 0x0A141E
    assert visual_helpers.parse_color_to_uno_int((1, 2, 3)) == 0x010203
    assert visual_helpers.parse_color_to_uno_int([255, 0, 0]) == 0xFF0000


def test_parse_color_to_uno_int_invalid() -> None:
    assert visual_helpers.parse_color_to_uno_int(None) is None
    assert visual_helpers.parse_color_to_uno_int("") is None
    assert visual_helpers.parse_color_to_uno_int("not-a-color") is None
    assert visual_helpers.parse_color_to_uno_int(True) is None
    assert visual_helpers.parse_color_to_uno_int((1, 2)) is None
    assert visual_helpers.parse_color_to_uno_int("rgb(300, 0, 0)") is None


def test_apply_character_properties() -> None:
    target = FakePropertyObject(
        {
            "CharFontName": "",
            "CharHeight": 10.0,
            "CharWeight": 100.0,
            "CharPosture": 0,
            "CharColor": 0,
            "CharUnderline": 0,
        }
    )
    results = visual_helpers.apply_character_properties(
        target,
        font_name="Arial",
        font_size_pt=12,
        bold=True,
        italic=False,
        color="#00FF00",
        underline=1,
    )
    assert results == {
        "CharFontName": True,
        "CharHeight": True,
        "CharWeight": True,
        "CharPosture": True,
        "CharColor": True,
        "CharUnderline": True,
    }
    assert target._properties["CharFontName"] == "Arial"
    assert target._properties["CharHeight"] == 12.0
    assert target._properties["CharWeight"] == 150.0
    assert target._properties["CharPosture"] == 0
    assert target._properties["CharColor"] == 0x00FF00
    assert target._properties["CharUnderline"] == 1


def test_apply_character_properties_italic_is_not_oblique() -> None:
    """Without pyuno, italic falls back to FontSlant.ITALIC (2), not OBLIQUE (1)."""
    target = FakePropertyObject({"CharPosture": 0})
    results = visual_helpers.apply_character_properties(target, italic=True)
    assert results == {"CharPosture": True}
    assert target._properties["CharPosture"] == 2


def test_apply_character_properties_italic_uses_named_font_slant(monkeypatch) -> None:
    """italic=True must pass FontSlant.ITALIC, the same named constant calc uses."""
    import sys
    import types

    slant = types.ModuleType("com.sun.star.awt.FontSlant")
    slant.NONE = "none-sentinel"
    slant.OBLIQUE = "oblique-sentinel"
    slant.ITALIC = "italic-sentinel"
    monkeypatch.setitem(sys.modules, "com.sun.star.awt.FontSlant", slant)

    target = FakePropertyObject({"CharPosture": 0})
    visual_helpers.apply_character_properties(target, italic=True)
    assert target._properties["CharPosture"] == "italic-sentinel"
    visual_helpers.apply_character_properties(target, italic=False)
    assert target._properties["CharPosture"] == "none-sentinel"


def test_apply_character_properties_invalid_color() -> None:
    target = FakePropertyObject({"CharColor": 0})
    results = visual_helpers.apply_character_properties(target, color="nope")
    assert results == {"CharColor": False}
    assert target._properties["CharColor"] == 0


class _RemovablePage:
    def __init__(self, shapes: list[object]):
        self._shapes = list(shapes)
        self.removed: list[object] = []

    def getCount(self) -> int:
        return len(self._shapes)

    def getByIndex(self, index: int):
        return self._shapes[index]

    def remove(self, shape: object) -> None:
        self.removed.append(shape)
        if shape in self._shapes:
            self._shapes.remove(shape)


class _NamedShape:
    def __init__(self, name: str):
        self._name = name

    def getName(self) -> str:
        return self._name


class _DocWithPages:
    def __init__(self, pages: list[object]):
        self._pages = pages

    def getDrawPages(self):
        return FakeDrawPage(self._pages)


def test_remove_graphic_identity_ignores_same_name():
    s1 = _NamedShape("Pic")
    s2 = _NamedShape("Pic")
    page = _RemovablePage([s1, s2])
    doc = _DocWithPages([page])

    res = visual_helpers.remove_graphic_from_draw_pages(doc, s2)
    assert res is True
    assert page.removed == [s2]


def test_remove_graphic_unique_name_fallback_deletes_that_shape():
    target = _NamedShape("Pic")
    on_page = _NamedShape("Pic")
    page = _RemovablePage([on_page])
    doc = _DocWithPages([page])

    res = visual_helpers.remove_graphic_from_draw_pages(doc, target)
    assert res is True
    assert page.removed == [on_page]


def test_remove_graphic_ambiguous_name_deletes_nothing():
    target = _NamedShape("Pic")
    s1 = _NamedShape("Pic")
    s2 = _NamedShape("Pic")
    page = _RemovablePage([s1, s2])
    doc = _DocWithPages([page])

    with pytest.raises(ValueError, match="Several shapes named 'Pic'"):
        visual_helpers.remove_graphic_from_draw_pages(doc, target)
    assert page.removed == []
def test_get_active_draw_page_reraises_disposed() -> None:
    import pytest

    from plugin.framework.errors import DocumentDisposedError

    class DisposedException(Exception):
        pass

    class DisposedDoc:
        @property
        def CurrentController(self):
            raise DisposedException("closed")

        def getCurrentController(self):
            raise DisposedException("closed")

    doc = DisposedDoc()
    with pytest.raises(DocumentDisposedError):
        visual_helpers.get_active_draw_page(doc, "calc")
    with pytest.raises(DocumentDisposedError):
        visual_helpers.get_active_draw_page(doc, "draw")

    class DisposedWriterDoc(FakeWriterDoc):
        def getDrawPage(self):
            raise DisposedException("closed")

    with pytest.raises(DocumentDisposedError):
        visual_helpers.get_active_draw_page(DisposedWriterDoc({}), "writer")

    assert visual_helpers.get_active_draw_page(FakeWriterDoc({}), "writer") is None


def test_list_graphic_objects_reraises_disposed() -> None:
    import pytest

    from plugin.framework.errors import DocumentDisposedError

    class DisposedException(Exception):
        pass

    class DisposedDrawDoc:
        def getDrawPages(self):
            raise DisposedException("closed")

    with pytest.raises(DocumentDisposedError):
        visual_helpers.list_graphic_objects(DisposedDrawDoc(), "draw")



def test_uno_read_probes_raise_on_disposal_and_default_otherwise():
    from unittest.mock import MagicMock

    from plugin.doc.visual_helpers import _graphics_in_text_range, safe_get_property, selected_graphic_object
    from plugin.framework.errors import DocumentDisposedError

    class DisposedException(Exception):
        pass

    class RuntimeException(Exception):
        pass

    closed = MagicMock()
    closed.getPropertyValue.side_effect = DisposedException("closed")
    with pytest.raises(DocumentDisposedError):
        safe_get_property(closed, "Size")

    odd = MagicMock()
    odd.getPropertyValue.side_effect = RuntimeException("uno")
    assert safe_get_property(odd, "HyperLinkURL", "") == ""

    rng = MagicMock()
    rng.getText.side_effect = RuntimeException("uno")
    assert _graphics_in_text_range(MagicMock(), rng) == []

    # selected_graphic_object keeps its own log-and-None outer handler.
    model = MagicMock()
    model.getCurrentController.side_effect = DisposedException("closed")
    assert selected_graphic_object(model) is None


def _graphic(px=(2000, 1000), hmm=(52917, 26458)):
    props = {"SizePixel": SimpleNamespace(Width=px[0], Height=px[1]), "Size100thMM": SimpleNamespace(Width=hmm[0], Height=hmm[1])}
    return SimpleNamespace(getPropertyValue=props.__getitem__)


def test_pixel_size_uses_pixels():
    assert visual_helpers.pixel_size(_graphic()) == (2000, 1000)


def test_pixel_size_vector_picture_gets_96_dpi_frame():
    # SVG/WMF report no pixels: 2540 (1/100 mm) = 1 inch = 96 px.
    assert visual_helpers.pixel_size(_graphic(px=(0, 0), hmm=(2540, 5080))) == (96, 192)


def test_pixel_size_unmeasurable_raises():
    with pytest.raises(ValueError, match="Cannot determine this picture's pixel size"):
        visual_helpers.pixel_size(_graphic(px=(0, 0), hmm=(0, 0)))


def test_draw_page_for_insert_draw_impress():
    from unittest.mock import MagicMock

    model = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 3
    page0, page1, page2 = MagicMock(), MagicMock(), MagicMock()
    pages.getByIndex.side_effect = lambda idx: [page0, page1, page2][idx]
    model.getDrawPages.return_value = pages

    assert visual_helpers.draw_page_for_insert(model, "draw", page_index=1) is page1
    assert visual_helpers.draw_page_for_insert(model, "impress", page_index=2) is page2

    with pytest.raises(IndexError, match="Page index -1 out of range"):
        visual_helpers.draw_page_for_insert(model, "draw", page_index=-1)

    with pytest.raises(IndexError, match="Page index 3 out of range"):
        visual_helpers.draw_page_for_insert(model, "impress", page_index=3)


def test_draw_page_for_insert_calc():
    from unittest.mock import MagicMock

    model = MagicMock()
    sheets = MagicMock()
    sheets.getCount.return_value = 2
    sheet0, sheet1 = MagicMock(), MagicMock()
    sheet1_dp = MagicMock()
    sheet1.getDrawPage.return_value = sheet1_dp
    sheets.getByIndex.side_effect = lambda idx: [sheet0, sheet1][idx]
    model.getSheets.return_value = sheets

    assert visual_helpers.draw_page_for_insert(model, "calc", page_index=1) is sheet1_dp

    with pytest.raises(IndexError, match="Sheet index -1 out of range"):
        visual_helpers.draw_page_for_insert(model, "calc", page_index=-1)

    with pytest.raises(IndexError, match="Sheet index 2 out of range"):
        visual_helpers.draw_page_for_insert(model, "calc", page_index=2)


def test_draw_page_for_insert_fallback_and_unresolved():
    from unittest.mock import MagicMock, patch

    model = MagicMock()
    sentinel_dp = MagicMock()
    with patch.object(visual_helpers, "get_active_draw_page", return_value=sentinel_dp):
        assert visual_helpers.draw_page_for_insert(model, "draw", page_index=None) is sentinel_dp

    with patch.object(visual_helpers, "get_active_draw_page", return_value=None):
        with pytest.raises(RuntimeError, match="Could not resolve draw page for writer"):
            visual_helpers.draw_page_for_insert(model, "writer", page_index=None)


def test_position_on_draw_page():
    from unittest.mock import MagicMock

    page = MagicMock()
    page.Width = 20000
    page.Height = 10000

    # Centered when x_mm and y_mm are None
    pos = visual_helpers.position_on_draw_page(page, width=4000, height=2000, x_mm=None, y_mm=None)
    assert pos.X == 8000
    assert pos.Y == 4000

    # Explicit mm
    pos_explicit = visual_helpers.position_on_draw_page(page, width=4000, height=2000, x_mm=15.5, y_mm=25.0)
    assert pos_explicit.X == visual_helpers.mm_to_unit(15.5)
    assert pos_explicit.Y == visual_helpers.mm_to_unit(25.0)

    # Partial mm
    pos_x_only = visual_helpers.position_on_draw_page(page, width=4000, height=2000, x_mm=10, y_mm=None)
    assert pos_x_only.X == visual_helpers.mm_to_unit(10)
    assert pos_x_only.Y == 0


