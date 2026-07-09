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

    def __init__(self, graphics: dict[str, object]):
        self._graphics = graphics

    def supportsService(self, service: str) -> bool:
        return service == visual_helpers.WRITER_DOCUMENT_SERVICE

    def getGraphicObjects(self):
        return FakeGraphicCollection(self._graphics)


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


def test_active_draw_page_resolves_calc_sheet_and_draw_current_page():
    calc_page = FakeDrawPage([])
    draw_page = FakeDrawPage([])

    assert visual_helpers.get_active_draw_page(FakeCalcDoc(calc_page), "calc") is calc_page
    assert visual_helpers.get_active_draw_page(FakeDrawDoc(draw_page), "draw") is draw_page


def test_list_graphic_objects_reads_writer_graphic_collection():
    graphic = FakePropertyObject({"Graphic": object()}, {visual_helpers.WRITER_GRAPHIC_SERVICE})
    doc = FakeWriterDoc({"Image 1": graphic})

    assert visual_helpers.get_visual_doc_type(doc) == "writer"
    assert visual_helpers.list_graphic_objects(doc) == [("Image 1", graphic)]
    assert visual_helpers.get_graphic_object_by_name(doc, "Image 1") is graphic


def test_list_graphic_objects_reads_calc_draw_page_graphic_shapes():
    graphic = FakeGraphicShape("Calc Image")
    other = FakePropertyObject({}, set())
    doc = FakeCalcDoc(FakeDrawPage([other, graphic]))

    assert visual_helpers.get_visual_doc_type(doc) == "calc"
    assert visual_helpers.list_graphic_objects(doc) == [("Calc Image", graphic)]
    assert visual_helpers.get_graphic_object_by_name(doc, "Calc Image") is graphic


def test_unit_conversions_match_existing_image_tool_assumptions():
    assert visual_helpers.mm_to_units(12.9, 3.1) == (1200, 300)
    assert visual_helpers.px_to_units(10, 20) == (264, 529)
    assert visual_helpers.units_to_px(2540, 1270) == (96, 48)
