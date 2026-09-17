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

log = logging.getLogger(__name__)

# Same Writer/Calc/Draw/Impress strings as plugin.doc.doc_type._DOCUMENT_SERVICE_MAP.
# WebDocument also supports TextDocument, so get_visual_doc_type checks WEB first.
WRITER_DOCUMENT_SERVICE = "com.sun.star.text.TextDocument"
WEB_DOCUMENT_SERVICE = "com.sun.star.text.WebDocument"
CALC_DOCUMENT_SERVICE = "com.sun.star.sheet.SpreadsheetDocument"
DRAW_DOCUMENT_SERVICE = "com.sun.star.drawing.DrawingDocument"
IMPRESS_DOCUMENT_SERVICE = "com.sun.star.presentation.PresentationDocument"

WRITER_GRAPHIC_SERVICE = "com.sun.star.text.TextGraphicObject"
TEXT_GRAPHIC_SERVICE = "com.sun.star.text.GraphicObject"
DRAW_GRAPHIC_SERVICE = "com.sun.star.drawing.GraphicObjectShape"

SHAPE_TOOL_UNO_SERVICES = [
    WRITER_DOCUMENT_SERVICE,
    CALC_DOCUMENT_SERVICE,
    DRAW_DOCUMENT_SERVICE,
    IMPRESS_DOCUMENT_SERVICE,
]


def get_visual_doc_type(doc: Any) -> str:
    """Return the visual-tool document label used by image and shape helpers.

    Delegates to :func:`plugin.doc.doc_type.get_document_type` for the
    shared Writer/Calc/Draw/Impress map. Web documents are checked first because
    they also support TextDocument and would otherwise look like Writer.
    Unknown models keep the legacy ``\"writer\"`` default (not ``\"unknown\"``).
    """
    try:
        if doc.supportsService(WEB_DOCUMENT_SERVICE):
            return "web"
    except Exception:
        pass
    from plugin.doc.doc_type import DocumentType, doc_type_label_for_enum, get_document_type

    doc_type = get_document_type(doc)
    if doc_type == DocumentType.UNKNOWN:
        return "writer"
    return doc_type_label_for_enum(doc_type)


def mm_to_units(width_mm: int | float, height_mm: int | float) -> tuple[int, int]:
    """Convert millimetres to LibreOffice 1/100 mm units, preserving legacy truncation."""
    return int(width_mm) * 100, int(height_mm) * 100


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
        # FontPosture: NONE=0, ITALIC=1 (matches common UNO/tool integer usage).
        results["CharPosture"] = safe_set_property(target, "CharPosture", 1 if italic else 0)
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
    try:
        return obj.getPropertyValue(name)
    except Exception:
        return default


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
    try:
        controller = None
        try:
            controller = model.getCurrentController()
        except Exception:
            controller = getattr(model, "CurrentController", None)
        if controller is None:
            return None
        selection = None
        try:
            selection = controller.getSelection()
        except Exception:
            selection = None
        if selection is None:
            selection = getattr(controller, "Selection", None)
        return selection if selection else None
    except Exception as ex:
        log.debug("_controller_selection failed: %s", ex)
        return None


def _graphic_object_name(obj: Any) -> str:
    try:
        return str(obj.getName() or "").strip()
    except Exception:
        return ""


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
    try:
        anchor = graphic.getAnchor()
        if anchor is None:
            return None
        if hasattr(anchor, "getStart"):
            return anchor.getStart()
        return anchor
    except Exception:
        return None


def _sort_graphics_by_anchor(text: Any, pairs: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Stable document-order sort using ``compareRegionStarts`` on graphic anchors."""
    if len(pairs) <= 1 or text is None:
        return pairs

    def _cmp(a: tuple[str, Any], b: tuple[str, Any]) -> int:
        a_start = _anchor_start(a[1])
        b_start = _anchor_start(b[1])
        if a_start is None or b_start is None:
            return 0
        try:
            # compareRegionStarts(A,B): 1 if A before B → A should sort first → negative cmp.
            return -int(text.compareRegionStarts(a_start, b_start))
        except Exception:
            return 0

    from functools import cmp_to_key

    return sorted(pairs, key=cmp_to_key(_cmp))


def _graphics_in_text_range(doc: Any, range_obj: Any) -> list[tuple[str, Any]]:
    """Named graphics whose anchors fall inside *range_obj* (intervening text ignored)."""
    try:
        text = range_obj.getText()
        sel_start = range_obj.getStart()
        sel_end = range_obj.getEnd()
    except Exception:
        return []
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
                text = None
                try:
                    anchor = graphics_from_sel[0][1].getAnchor()
                    text = anchor.getText() if anchor is not None else None
                except Exception:
                    text = None
                return _sort_graphics_by_anchor(text, graphics_from_sel)

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
    try:
        controller = doc.CurrentController
    except Exception:
        try:
            controller = doc.getCurrentController()
        except Exception:
            controller = None

    def _writer_draw_page() -> Any | None:
        try:
            return doc.getDrawPage()
        except Exception:
            return None

    if controller is None:
        # Writer forms/shapes only need getDrawPage(); no controller required.
        if inside in ("writer", "web"):
            return _writer_draw_page()
        return None

    if inside == "calc":
        sheet = None
        try:
            sheet = controller.ActiveSheet
        except Exception:
            pass
        if sheet is None:
            try:
                sheet = controller.getActiveSheet()
            except Exception:
                sheet = None
        if sheet is None:
            return None
        try:
            return sheet.getDrawPage()
        except Exception:
            try:
                return sheet.DrawPage
            except Exception:
                return None

    try:
        page = controller.CurrentPage
    except Exception:
        page = None
    if page is not None:
        return page
    try:
        return controller.getCurrentPage()
    except Exception:
        pass
    try:
        pages = doc.getDrawPages()
        if pages.getCount() > 0:
            return pages.getByIndex(0)
    except Exception:
        pass
    return _writer_draw_page()


def list_graphic_objects(doc: Any, doc_type: str | None = None) -> list[tuple[str, Any]]:
    """Return ``(name, object)`` pairs for document-level graphic objects."""
    inside = doc_type or get_visual_doc_type(doc)
    graphics: list[tuple[str, Any]] = []

    if inside == "calc":
        draw_page = get_active_draw_page(doc, inside)
        if draw_page is None:
            return graphics
        try:
            for i in range(draw_page.getCount()):
                shape = draw_page.getByIndex(i)
                if is_graphic_object(shape):
                    graphics.append((shape.getName(), shape))
        except Exception as ex:
            log.debug("list_graphic_objects calc failed: %s", ex)
        return graphics

    if inside in ("draw", "impress"):
        pages = None
        try:
            pages = doc.getDrawPages()
        except Exception:
            pages = None
        page_list = []
        if pages is not None:
            try:
                page_list = [pages.getByIndex(i) for i in range(pages.getCount())]
            except Exception as ex:
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
        log.debug("list_graphic_objects writer failed: %s", ex)
    return graphics


def remove_graphic_from_draw_pages(doc: Any, graphic: Any) -> bool:
    """Remove a GraphicObjectShape from Calc/Draw/Impress pages. Returns True if removed."""
    if graphic is None or doc is None:
        return False
    try:
        pages = doc.getDrawPages()
    except Exception:
        page = get_active_draw_page(doc)
        if page is None:
            return False
        try:
            page.remove(graphic)
            return True
        except Exception:
            return False
    try:
        for i in range(pages.getCount()):
            page = pages.getByIndex(i)
            for j in range(page.getCount()):
                shape = page.getByIndex(j)
                if shape is graphic or shape == graphic:
                    page.remove(graphic)
                    return True
                try:
                    if shape.getName() and graphic.getName() and shape.getName() == graphic.getName():
                        page.remove(shape)
                        return True
                except Exception:
                    pass
    except Exception as ex:
        log.debug("remove_graphic_from_draw_pages failed: %s", ex)
    return False


def get_graphic_object_by_name(doc: Any, image_name: str, doc_type: str | None = None) -> Any | None:
    if not image_name:
        return None
    for name, graphic in list_graphic_objects(doc, doc_type=doc_type):
        if name == image_name:
            return graphic
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
