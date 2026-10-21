# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared helpers for visual UNO objects across Writer, Calc, Draw, and Impress.

Also covers color parsing (``parse_color_to_uno_int``) and Char* property batching
(``apply_character_properties``) used by shapes, charts, cells, and styles.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from plugin.doc.doc_type import (
    DOCUMENT_SERVICES,
    WEB_DOCUMENT_SERVICE,
    DocumentType,
    doc_type_label_for_enum,
    get_document_type,
)
from plugin.framework.errors import is_real_disposal, uno_read

log = logging.getLogger(__name__)

# uno_read default where None is a real UNO result (an attribute that exists but is None).
_FAILED: Any = object()

WRITER_GRAPHIC_SERVICE = "com.sun.star.text.TextGraphicObject"
TEXT_GRAPHIC_SERVICE = "com.sun.star.text.GraphicObject"
DRAW_GRAPHIC_SERVICE = "com.sun.star.drawing.GraphicObjectShape"

SHAPE_TOOL_UNO_SERVICES = list(DOCUMENT_SERVICES)

# IDL order of com.sun.star.text.TextContentAnchorType. image_set_properties
# and frame_set_properties write these same integers.
_ANCHOR_TYPE_INDEX = {
    "AT_PARAGRAPH": 0,
    "AS_CHARACTER": 1,
    "AT_PAGE": 2,
    "AT_FRAME": 3,
    "AT_CHARACTER": 4,
}


def anchor_type_index(raw: Any) -> int | None:
    """Map TextContentAnchorType to the 0-4 index the set-properties tools write.

    What was wrong: image_get_info and frame_get_info always returned anchor_type None.
    How it happened: getPropertyValue("AnchorType") is a pyuno enum. .value is the
    name ("AT_PARAGRAPH"), so int(name) raises ValueError, and int(enum) raises
    TypeError. Both were swallowed. html_import.py already reads that .value name.
    Why this change: the name is the stable token. The integers are the IDL order
    those tools pass to setPropertyValue.
    """
    if type(raw) is int and raw in _ANCHOR_TYPE_INDEX.values():
        return raw
    name = getattr(raw, "value", None)
    if not isinstance(name, str):
        name = raw
    if isinstance(name, str):
        return _ANCHOR_TYPE_INDEX.get(name)
    return None


def get_visual_doc_type(doc: Any) -> str:
    """Return the visual-tool document label used by image and shape helpers.

    Delegates to :func:`plugin.doc.doc_type.get_document_type` for the
    shared Writer/Calc/Draw/Impress map. Web documents are checked first because
    they also support TextDocument and would otherwise look like Writer.
    Unknown models keep the legacy ``\"writer\"`` default (not ``\"unknown\"``).
    """
    if doc is not None:
        try:
            if doc.supportsService(WEB_DOCUMENT_SERVICE):
                return "web"
        except Exception:
            pass

    doc_type = get_document_type(doc)
    if doc_type == DocumentType.UNKNOWN:
        return "writer"
    return doc_type_label_for_enum(doc_type)


# Bugfix: int(mm) * 100 truncated fractional millimetres to whole millimetres
# (e.g. 12.7 mm became 12 mm / 1200 units). Rounding float(mm) * 100 preserves
# fractional millimetre inputs to the nearest 1/100 mm LibreOffice unit.
def mm_to_unit(mm: int | float) -> int:
    """Nearest LibreOffice 1/100 mm. Python round (half to even)."""
    return int(round(float(mm) * 100))


def mm_to_units(width_mm: int | float, height_mm: int | float) -> tuple[int, int]:
    return mm_to_unit(width_mm), mm_to_unit(height_mm)


def px_to_units(width_px: int | float, height_px: int | float) -> tuple[int, int]:
    """Convert 96-DPI pixels to LibreOffice 1/100 mm units."""
    return int(width_px * 26.46), int(height_px * 26.46)


# On-page inset for generated (and other px-sized) graphics. API pixels are
# generate resolution, not print size: 1024px at 96 DPI is ~10.7" and overflows
# a Writer page. 512px at 96 DPI was ~135mm. Cap the longer edge there so
# 1024/1536 look sharper at the same footprint, not larger.
GENERATED_IMAGE_MAX_DISPLAY_MM = 135


def px_to_display_units(
    width_px: int | float,
    height_px: int | float,
    *,
    max_mm: int = GENERATED_IMAGE_MAX_DISPLAY_MM,
) -> tuple[int, int]:
    """Convert 96-DPI pixels to 1/100 mm, then cap the longer edge.

    Generate resolution and on-page size are independent. Smaller images are
    not scaled up.
    """
    width_units, height_units = px_to_units(width_px, height_px)
    if width_units <= 0 or height_units <= 0:
        return width_units, height_units
    max_units = max(1, int(max_mm) * 100)
    longer = max(width_units, height_units)
    if longer <= max_units:
        return width_units, height_units
    scale = max_units / longer
    return int(width_units * scale), int(height_units * scale)


def units_to_px(width_units: int | float, height_units: int | float, *, minimum: int = 1) -> tuple[int, int]:
    """Convert LibreOffice 1/100 mm units to 96-DPI pixels."""
    width_px = int(width_units * 96 / 2540)
    height_px = int(height_units * 96 / 2540)
    return max(minimum, width_px), max(minimum, height_px)


def mm_to_px(width_mm: int | float, height_mm: int | float, *, minimum: int = 1) -> tuple[int, int]:
    width_units, height_units = mm_to_units(width_mm, height_mm)
    return units_to_px(width_units, height_units, minimum=minimum)


def graphic_pixel_size(graphic: Any) -> tuple[int | None, int | None]:
    """Read pixel dimensions from an XGraphic, falling back to a 96-DPI frame from Size100thMM."""
    if graphic is None:
        return None, None
    try:
        px = safe_get_property(graphic, "SizePixel")
        if px is None and hasattr(graphic, "getPropertyValue"):
            px = graphic.getPropertyValue("SizePixel")
        if px is not None and getattr(px, "Width", 0) > 0 and getattr(px, "Height", 0) > 0:
            return int(px.Width), int(px.Height)
    except Exception as ex:
        if is_real_disposal(ex):
            raise
    try:
        hmm = safe_get_property(graphic, "Size100thMM")
        if hmm is None and hasattr(graphic, "getPropertyValue"):
            hmm = graphic.getPropertyValue("Size100thMM")
        if hmm is not None and (getattr(hmm, "Width", 0) > 0 or getattr(hmm, "Height", 0) > 0):
            # 1 px = 2540 / 96 (1/100 mm). Use round so 1000 1/100mm is 38 px.
            w = max(1, round(float(hmm.Width) * 96 / 2540))
            h = max(1, round(float(hmm.Height) * 96 / 2540))
            return w, h
    except Exception as ex:
        if is_real_disposal(ex):
            raise
    return None, None


def has_uno_property(obj: Any, name: str) -> bool:
    """True when *name* exists on the UNO PropertySet.

    PyUNO can raise while probing missing properties, so visual tools should use
    PropertySetInfo rather than Python attribute checks for UNO properties.
    """
    try:
        psi = obj.getPropertySetInfo()
        if psi is not None and hasattr(psi, "hasPropertyByName"):
            return bool(psi.hasPropertyByName(name))
    except Exception:
        pass
    return False


def safe_set_property(obj: Any, name: str, value: Any) -> bool:
    if not has_uno_property(obj, name):
        return False
    try:
        obj.setPropertyValue(name, value)
        return True
    except Exception as ex:
        log.debug("safe_set_property %s failed: %s", name, ex)
        return False


# CSS / X11 names used by chart and cell color args (flexible spacing/hyphens via normalize below).
_COLOR_NAMES: dict[str, int] = {
    "black": 0x000000,
    "silver": 0xC0C0C0,
    "gray": 0x808080,
    "white": 0xFFFFFF,
    "maroon": 0x800000,
    "red": 0xFF0000,
    "purple": 0x800080,
    "fuchsia": 0xFF00FF,
    "green": 0x008000,
    "lime": 0x00FF00,
    "olive": 0x808000,
    "yellow": 0xFFFF00,
    "navy": 0x000080,
    "blue": 0x0000FF,
    "teal": 0x008080,
    "aqua": 0x00FFFF,
    "cyan": 0x00FFFF,
    "magenta": 0xFF00FF,
    "orange": 0xFFA500,
    "pink": 0xFFC0CB,
    "gold": 0xFFD700,
    "brown": 0xA52A2A,
    "violet": 0xEE82EE,
    "indigo": 0x4B0082,
    "turquoise": 0x40E0D0,
    "lavender": 0xE6E6FA,
    "beige": 0xF5F5DC,
    "salmon": 0xFA8072,
    "olivedrab": 0x6B8E23,
    "darkgreen": 0x006400,
    "darkred": 0x8B0000,
    "darkblue": 0x00008B,
    "lightblue": 0xADD8E6,
    "lightgreen": 0x90EE90,
}

_RGB_RE = re.compile(r"^rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*[\d.]+\s*)?\)$")


def parse_color_to_uno_int(color_val: Any) -> int | None:
    """Convert hex, named, rgb()/rgba(), int, or RGB tuple/list to a UNO RGB int.

    Returns ``None`` when *color_val* is falsy or unparseable. Integers are masked
    to 24-bit RGB. Callers that only accept strings (e.g. Calc ``set_style``) should
    reject non-strings before calling this.
    """
    if color_val is None or color_val is False:
        return None
    if isinstance(color_val, bool):
        # bool is a subclass of int; never treat True/False as colors.
        return None
    if isinstance(color_val, int):
        return color_val & 0xFFFFFF
    if isinstance(color_val, (tuple, list)) and len(color_val) >= 3:
        try:
            r, g, b = int(color_val[0]), int(color_val[1]), int(color_val[2])
        except (TypeError, ValueError):
            return None
        if 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255:
            return (r << 16) | (g << 8) | b
        return None
    if not isinstance(color_val, str):
        return None
    if not color_val.strip():
        return None

    color_str = color_val.strip().lower()
    norm_name = color_str.replace(" ", "").replace("_", "").replace("-", "")
    if norm_name in _COLOR_NAMES:
        return _COLOR_NAMES[norm_name]

    rgb_match = _RGB_RE.match(color_str)
    if rgb_match:
        try:
            r = int(rgb_match.group(1))
            g = int(rgb_match.group(2))
            b = int(rgb_match.group(3))
        except ValueError:
            return None
        if 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255:
            return (r << 16) | (g << 8) | b
        return None

    hex_str = color_str
    if hex_str.startswith("0x"):
        hex_str = hex_str[2:]
    else:
        hex_str = hex_str.lstrip("#")
    if len(hex_str) == 3:
        hex_str = "".join(c * 2 for c in hex_str)
    if len(hex_str) == 6:
        try:
            return int(hex_str, 16)
        except ValueError:
            return None
    return None


def _font_slant(italic: bool) -> Any:
    """Return ``FontSlant.ITALIC`` or ``FontSlant.NONE``.

    Integer 1 is ``OBLIQUE``, not italic. Pytest has no pyuno enum module, so
    the import falls back to the IDL integers (ITALIC=2, NONE=0).
    """
    try:
        from com.sun.star.awt.FontSlant import ITALIC, NONE
    except ImportError:
        # Stubs type those names as FontSlant, so the integers stay on this path only.
        return 2 if italic else 0
    return ITALIC if italic else NONE


def apply_character_properties(
    target: Any,
    *,
    font_name: str | None = None,
    font_size_pt: float | int | None = None,
    bold: bool | None = None,
    italic: bool | None = None,
    color: Any = None,
    underline: int | None = None,
) -> dict[str, bool]:
    """Batch-set common Char* properties via :func:`safe_set_property`.

    Returns a map of UNO property name → whether the set succeeded. Missing keys
    were not requested. Color values go through :func:`parse_color_to_uno_int`.
    """
    results: dict[str, bool] = {}
    if font_name is not None:
        results["CharFontName"] = safe_set_property(target, "CharFontName", font_name)
    if font_size_pt is not None:
        results["CharHeight"] = safe_set_property(target, "CharHeight", float(font_size_pt))
    if bold is not None:
        results["CharWeight"] = safe_set_property(target, "CharWeight", 150.0 if bold else 100.0)
    if italic is not None:
        # FontSlant IDL is NONE=0, OBLIQUE=1, ITALIC=2 (offapi
        # awt/FontSlant.idl). Storing 1 for italic makes italic text
        # oblique. Named constants match the calc manipulator.
        results["CharPosture"] = safe_set_property(target, "CharPosture", _font_slant(italic))
    if color is not None:
        parsed = parse_color_to_uno_int(color)
        if parsed is not None:
            results["CharColor"] = safe_set_property(target, "CharColor", parsed)
        else:
            results["CharColor"] = False
    if underline is not None:
        results["CharUnderline"] = safe_set_property(target, "CharUnderline", underline)
    return results


def safe_try_method(obj: Any, method_name: str, *args: Any) -> bool:
    try:
        method = getattr(obj, method_name, None)
        if callable(method):
            method(*args)
            return True
    except Exception as ex:
        log.debug("safe_try_method %s failed: %s", method_name, ex)
    return False


def safe_get_property(obj: Any, name: str, default: Any = None) -> Any:
    return uno_read(lambda: obj.getPropertyValue(name), default)


def is_graphic_object(obj: Any) -> bool:
    if obj is None:
        return False

    graphic = safe_get_property(obj, "Graphic")
    if graphic is not None:
        return True

    try:
        if obj.supportsService(WRITER_GRAPHIC_SERVICE) or obj.supportsService(TEXT_GRAPHIC_SERVICE) or obj.supportsService(DRAW_GRAPHIC_SERVICE):
            return True
    except Exception:
        pass

    # Some TextGraphicObject instances expose GraphicURL even when Graphic cannot
    # be read directly. The property read is guarded to avoid PyUNO probe errors.
    return safe_get_property(obj, "GraphicURL") is not None


def _controller_selection(model: Any) -> Any | None:
    # Callers catch Exception. uno_read covers getCurrentController and getSelection.
    controller = uno_read(lambda: model.getCurrentController(), _FAILED)
    if controller is _FAILED:
        controller = getattr(model, "CurrentController", None)
    if controller is None:
        return None
    selection = uno_read(lambda: controller.getSelection(), None)
    if selection is None:
        selection = getattr(controller, "Selection", None)
    return selection if selection else None


def _graphic_object_name(obj: Any) -> str:
    return uno_read(lambda: str(obj.getName() or "").strip(), "")


def selected_graphic_object(model: Any) -> Any | None:
    try:
        selection = _controller_selection(model)
        if not selection:
            return None
        if hasattr(selection, "getCount"):
            if selection.getCount() != 1:
                return None
            obj = selection.getByIndex(0)
        else:
            obj = selection
        return obj if is_graphic_object(obj) else None
    except Exception as ex:
        log.debug("selected_graphic_object failed: %s", ex)
        return None


def _anchor_start(graphic: Any) -> Any | None:
    def start() -> Any:
        anchor = graphic.getAnchor()
        if anchor is not None and hasattr(anchor, "getStart"):
            return anchor.getStart()
        return anchor

    return uno_read(start, None)


def _sort_graphics_by_anchor(text: Any, pairs: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Stable document-order sort using ``compareRegionStarts`` on graphic anchors."""
    if len(pairs) <= 1 or text is None:
        return pairs

    def _cmp(a: tuple[str, Any], b: tuple[str, Any]) -> int:
        a_start = _anchor_start(a[1])
        b_start = _anchor_start(b[1])
        if a_start is None or b_start is None:
            return 0
        # compareRegionStarts(A,B): 1 if A before B → A should sort first → negative cmp.
        return uno_read(lambda: -int(text.compareRegionStarts(a_start, b_start)), 0)

    from functools import cmp_to_key

    return sorted(pairs, key=cmp_to_key(_cmp))


def _graphics_in_text_range(doc: Any, range_obj: Any) -> list[tuple[str, Any]]:
    """Named graphics whose anchors fall inside *range_obj* (intervening text ignored)."""
    bounds = uno_read(lambda: (range_obj.getText(), range_obj.getStart(), range_obj.getEnd()), None)
    if bounds is None:
        return []
    text, sel_start, sel_end = bounds
    if text is None or sel_start is None or sel_end is None:
        return []

    found: list[tuple[str, Any]] = []
    for name, graphic in list_graphic_objects(doc):
        if not name:
            continue
        anchor_start = _anchor_start(graphic)
        if anchor_start is None:
            continue
        try:
            # sel_start <= anchor_start <= sel_end
            if text.compareRegionStarts(sel_start, anchor_start) < 0:
                continue
            if text.compareRegionStarts(anchor_start, sel_end) < 0:
                continue
            found.append((name, graphic))
        except Exception:
            continue
    return _sort_graphics_by_anchor(text, found)


def graphic_objects_in_selection(doc: Any) -> list[tuple[str, Any]]:
    """Return named ``(name, graphic)`` pairs covered by the current selection.

    Writer supports: a single selected graphic, multi-selected graphics, or a text
    range that contains embedded images (text in the range is ignored). Non-Writer
    documents return at most the single selected graphic (Calc multi stays out of scope).
    """
    try:
        selection = _controller_selection(doc)
        if not selection:
            return []

        if get_visual_doc_type(doc) != "writer":
            graphic = selected_graphic_object(doc)
            if graphic is None:
                return []
            name = _graphic_object_name(graphic)
            return [(name, graphic)] if name else []

        if hasattr(selection, "getCount"):
            graphics_from_sel: list[tuple[str, Any]] = []
            for i in range(selection.getCount()):
                obj = selection.getByIndex(i)
                if not is_graphic_object(obj):
                    continue
                name = _graphic_object_name(obj)
                if name:
                    graphics_from_sel.append((name, obj))
            if graphics_from_sel:

                def first_anchor_text() -> Any:
                    anchor = graphics_from_sel[0][1].getAnchor()
                    return anchor.getText() if anchor is not None else None

                return _sort_graphics_by_anchor(uno_read(first_anchor_text, None), graphics_from_sel)

            if selection.getCount() > 0:
                range_obj = selection.getByIndex(0)
                if hasattr(range_obj, "getStart") and hasattr(range_obj, "getEnd"):
                    return _graphics_in_text_range(doc, range_obj)
            return []

        if is_graphic_object(selection):
            name = _graphic_object_name(selection)
            return [(name, selection)] if name else []

        if hasattr(selection, "getStart") and hasattr(selection, "getEnd"):
            return _graphics_in_text_range(doc, selection)
        return []
    except Exception as ex:
        log.debug("graphic_objects_in_selection failed: %s", ex)
        return []


def get_active_draw_page(doc: Any, doc_type: str | None = None) -> Any | None:
    """Return the active draw page for Writer, Calc, Draw, or Impress.

    Calc uses the active sheet draw page; Draw/Impress use the current slide;
    Writer falls back to ``doc.getDrawPage()`` (document canvas for forms/shapes).
    """
    inside = doc_type or get_visual_doc_type(doc)
    # A closed document raises DocumentDisposedError (uno_read) so execute_safe reports
    # DOCUMENT_DISPOSED; any other failure stays on the missing-page path.
    controller = uno_read(lambda: doc.CurrentController, _FAILED)
    if controller is _FAILED:
        controller = uno_read(lambda: doc.getCurrentController(), None)

    def _writer_draw_page() -> Any | None:
        return uno_read(lambda: doc.getDrawPage(), None)

    if controller is None:
        # Writer forms/shapes only need getDrawPage(); no controller required.
        if inside in ("writer", "web"):
            return _writer_draw_page()
        return None

    if inside == "calc":
        sheet = uno_read(lambda: controller.ActiveSheet, None)
        if sheet is None:
            sheet = uno_read(lambda: controller.getActiveSheet(), None)
        if sheet is None:
            return None
        page = uno_read(lambda: sheet.getDrawPage(), _FAILED)
        return uno_read(lambda: sheet.DrawPage, None) if page is _FAILED else page

    page = uno_read(lambda: controller.CurrentPage, None)
    if page is not None:
        return page
    page = uno_read(lambda: controller.getCurrentPage(), _FAILED)
    if page is not _FAILED:
        return page

    def first_page() -> Any:
        pages = doc.getDrawPages()
        return pages.getByIndex(0) if pages.getCount() > 0 else _FAILED

    page = uno_read(first_page, _FAILED)
    return _writer_draw_page() if page is _FAILED else page


class GraphicEntry(tuple[str, Any]):
    """(name, graphic) pair preserving Calc sheet metadata when available."""

    sheet_index: int | None = None
    sheet_name: str | None = None


class AmbiguousGraphicNameError(ValueError):
    """Raised when looking up a graphic by name matches multiple objects across sheets."""

    def __init__(self, message: str, sheet_indices: list[int] | None = None) -> None:
        super().__init__(message)
        self.sheet_indices: list[int] = sheet_indices or []


def list_graphic_objects(doc: Any, doc_type: str | None = None) -> list[tuple[str, Any]]:
    """Return ``(name, object)`` pairs for document-level graphic objects."""
    inside = doc_type or get_visual_doc_type(doc)
    graphics: list[tuple[str, Any]] = []

    if inside == "calc":
        get_sheets = getattr(doc, "getSheets", None)
        if callable(get_sheets):
            try:
                sheets: Any = get_sheets()
                count = sheets.getCount()
                for sheet_idx in range(count):
                    sheet = sheets.getByIndex(sheet_idx)
                    dp = getattr(sheet, "getDrawPage", None)
                    draw_page: Any = dp() if callable(dp) else getattr(sheet, "DrawPage", None)
                    if draw_page is None:
                        continue
                    sheet_name = getattr(sheet, "Name", None)
                    if sheet_name is None and hasattr(sheet, "getName"):
                        try:
                            sheet_name = sheet.getName()
                        except Exception:
                            pass
                    for i in range(draw_page.getCount()):
                        shape = draw_page.getByIndex(i)
                        if is_graphic_object(shape):
                            name = shape.getName() if hasattr(shape, "getName") else ""
                            item = GraphicEntry((name, shape))
                            item.sheet_index = sheet_idx
                            item.sheet_name = str(sheet_name) if sheet_name is not None else None
                            try:
                                shape._calc_sheet_index = sheet_idx
                                if sheet_name is not None:
                                    shape._calc_sheet_name = str(sheet_name)
                            except (AttributeError, TypeError):
                                pass
                            graphics.append(item)
                return graphics
            except Exception as ex:
                if is_real_disposal(ex):
                    raise
                log.debug("list_graphic_objects calc sheets failed: %s", ex)

        draw_page = get_active_draw_page(doc, inside)
        if draw_page is None:
            return graphics
        try:
            for i in range(draw_page.getCount()):
                shape = draw_page.getByIndex(i)
                if is_graphic_object(shape):
                    graphics.append((shape.getName() if hasattr(shape, "getName") else "", shape))
        except Exception as ex:
            if is_real_disposal(ex):
                raise
            log.debug("list_graphic_objects calc failed: %s", ex)
        return graphics

    if inside in ("draw", "impress"):
        pages = uno_read(lambda: doc.getDrawPages(), None)
        page_list = []
        if pages is not None:
            try:
                page_list = [pages.getByIndex(i) for i in range(pages.getCount())]
            except Exception as ex:
                if is_real_disposal(ex):
                    raise
                log.debug("list_graphic_objects draw/impress pages failed: %s", ex)
        if not page_list:
            draw_page = get_active_draw_page(doc, inside)
            if draw_page is not None:
                page_list = [draw_page]
        try:
            for draw_page in page_list:
                for i in range(draw_page.getCount()):
                    shape = draw_page.getByIndex(i)
                    if is_graphic_object(shape):
                        graphics.append((shape.getName(), shape))
        except Exception as ex:
            if is_real_disposal(ex):
                raise
            log.debug("list_graphic_objects draw/impress failed: %s", ex)
        return graphics

    try:
        get_graphics = getattr(doc, "getGraphicObjects", None)
        if not callable(get_graphics):
            return graphics
        graphic_objects: Any = get_graphics()
        for name in graphic_objects.getElementNames():
            graphics.append((name, graphic_objects.getByName(name)))
    except Exception as ex:
        if is_real_disposal(ex):
            raise
        log.debug("list_graphic_objects writer failed: %s", ex)
    return graphics


def remove_graphic_from_draw_pages(doc: Any, graphic: Any) -> bool:
    """Remove a GraphicObjectShape from Calc/Draw/Impress pages. Returns True if removed."""
    if graphic is None or doc is None:
        return False
    target_name = uno_read(lambda: str(graphic.getName() or "") if hasattr(graphic, "getName") else "", "")

    inside = get_visual_doc_type(doc)
    pages_list: list[Any] = []
    if inside == "calc":
        get_sheets = getattr(doc, "getSheets", None)
        if callable(get_sheets):
            try:
                sheets: Any = get_sheets()
                for i in range(sheets.getCount()):
                    sheet = sheets.getByIndex(i)
                    dp = getattr(sheet, "getDrawPage", None)
                    p: Any = dp() if callable(dp) else getattr(sheet, "DrawPage", None)
                    if p is not None:
                        pages_list.append(p)
            except Exception as exc:
                if is_real_disposal(exc):
                    raise
                log.debug("remove_graphic_from_draw_pages getSheets failed: %s", exc)
        if not pages_list:
            active_p = get_active_draw_page(doc, inside)
            if active_p is not None:
                pages_list.append(active_p)
    else:
        try:
            pages = doc.getDrawPages()
            pages_list = [pages.getByIndex(i) for i in range(pages.getCount())]
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            page = get_active_draw_page(doc)
            if page is None:
                return False
            pages_list = [page]

    name_hits: list[tuple[Any, Any]] = []
    try:
        for page in pages_list:
            for j in range(page.getCount()):
                shape = page.getByIndex(j)
                if shape is graphic or shape == graphic:
                    page.remove(graphic)
                    return True
                # Identity is the shape the caller resolved. A same name on
                # a different shape used to delete the first hit, so two
                # shapes named alike could drop the wrong one. Remember name
                # hits and delete only when the name is unique.
                if target_name and uno_read(lambda: hasattr(shape, "getName") and str(shape.getName() or "") == target_name, False):
                    name_hits.append((page, shape))
    except Exception as ex:
        # Disposal used to become False, and image_delete reported
        # IMAGE_NOT_FOUND for a closed document.
        if is_real_disposal(ex):
            raise
        log.debug("remove_graphic_from_draw_pages failed: %s", ex)
        return False

    if len(name_hits) > 1:
        # More than one shape shares this name and none of them was the object
        # itself. Do not guess. image_delete reports AMBIGUOUS_NAME.
        raise ValueError("Several shapes named '%s'; not deleted." % target_name)
    if len(name_hits) == 1:
        hit_page, hit_shape = name_hits[0]
        hit_page.remove(hit_shape)
        return True
    return False


def get_graphic_object_by_name(
    doc: Any,
    image_name: str,
    doc_type: str | None = None,
    sheet_index: int | None = None,
) -> Any | None:
    if not image_name:
        return None
    matches: list[tuple[Any, int | None]] = []
    for item in list_graphic_objects(doc, doc_type=doc_type):
        name = item[0]
        graphic = item[1]
        s_idx = getattr(item, "sheet_index", getattr(graphic, "_calc_sheet_index", None))
        if name == image_name:
            if sheet_index is not None:
                if s_idx == sheet_index:
                    return graphic
            else:
                matches.append((graphic, s_idx))
    if sheet_index is not None:
        return None
    if len(matches) == 1:
        return matches[0][0]
    if len(matches) > 1:
        sheet_indices = [idx for _g, idx in matches if idx is not None]
        sheets_info = f" (sheets {sheet_indices})" if sheet_indices else ""
        err = AmbiguousGraphicNameError(
            f"Several shapes named '{image_name}'{sheets_info}; specify sheet_index.",
            sheet_indices=sheet_indices,
        )
        raise err
    return None


def graphic_from_object(obj: Any) -> Any | None:
    """Return the UNO Graphic for a text graphic or draw GraphicObjectShape."""
    if obj is None:
        return None
    graphic = safe_get_property(obj, "Graphic")
    if graphic is not None:
        return graphic
    try:
        graphic = obj.Graphic
        return graphic if graphic is not None else None
    except Exception as ex:
        log.debug("graphic_from_object missing Graphic: %s", ex)
    return None
