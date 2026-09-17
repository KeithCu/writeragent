# WriterAgent - AI Writing Assistant for LibreOffice
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for shared visual UNO helper functions."""

from __future__ import annotations

from plugin.doc import visual_helpers


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
        return service == visual_helpers.WRITER_DOCUMENT_SERVICE

    def getGraphicObjects(self):
        return FakeGraphicCollection(self._graphics)

    def getDrawPage(self):
        if self._draw_page is None:
            raise AttributeError("no draw page")
        return self._draw_page


class FakeSheet:
    def __init__(self, draw_page: FakeDrawPage):
        self.DrawPage = draw_page

    def getDrawPage(self):
        return self.DrawPage


class FakeCalcDoc:
    def __init__(self, draw_page: FakeDrawPage):
        self.CurrentController = FakeController(active_sheet=FakeSheet(draw_page))

    def supportsService(self, service: str) -> bool:
        return service == visual_helpers.CALC_DOCUMENT_SERVICE


class FakeDrawDoc:
    def __init__(self, draw_page: FakeDrawPage):
        self.CurrentController = FakeController(current_page=draw_page)

    def supportsService(self, service: str) -> bool:
        return service == visual_helpers.DRAW_DOCUMENT_SERVICE


class FakeImpressDoc:
    """PresentationDocument only — not DrawingDocument (isolates impress label)."""

    def supportsService(self, service: str) -> bool:
        return service == visual_helpers.IMPRESS_DOCUMENT_SERVICE


class FakeWebDoc:
    def supportsService(self, service: str) -> bool:
        return service in (visual_helpers.WEB_DOCUMENT_SERVICE, visual_helpers.WRITER_DOCUMENT_SERVICE)


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


def test_get_visual_doc_type_web_before_writer():
    assert visual_helpers.get_visual_doc_type(FakeWebDoc()) == "web"


def test_get_visual_doc_type_draw_and_impress():
    assert visual_helpers.get_visual_doc_type(FakeDrawDoc(FakeDrawPage([]))) == "draw"
    assert visual_helpers.get_visual_doc_type(FakeImpressDoc()) == "impress"


def test_unit_conversions_match_existing_image_tool_assumptions():
    assert visual_helpers.mm_to_units(12.9, 3.1) == (1200, 300)
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


def test_apply_character_properties_invalid_color() -> None:
    target = FakePropertyObject({"CharColor": 0})
    results = visual_helpers.apply_character_properties(target, color="nope")
    assert results == {"CharColor": False}
    assert target._properties["CharColor"] == 0
