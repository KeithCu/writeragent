# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared helpers for visual UNO objects across Writer, Calc, Draw, and Impress.

Also covers color parsing (``parse_color_to_uno_int``) and Char* property batching
(``apply_character_properties``) used by shapes, charts, cells, and styles.

Px ↔ 1/100 mm conversion rounds in both directions (`UNITS_PER_INCH` / `PX_PER_INCH`).
"""

from __future__ import annotations

from functools import cmp_to_key
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

from com.sun.star.awt import Point

from plugin.framework.doc_type import (
    DOCUMENT_SERVICES,
    WEB_DOCUMENT_SERVICE,
    DocumentType,
    doc_type_label_for_enum,
    get_document_type,
)
from plugin.doc.text_helpers import FAILED
from plugin.framework.errors import is_disposal, reraise_if_disposed, uno_read

log = logging.getLogger(__name__)

WRITER_GRAPHIC_SERVICE = "com.sun.star.text.TextGraphicObject"
_TEXT_GRAPHIC_SERVICE = "com.sun.star.text.GraphicObject"
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

    AnchorType is a pyuno enum: use .value name or the IDL index map—int(enum) fails silently otherwise.
    """
    if type(raw) is int and raw in _ANCHOR_TYPE_INDEX.values():
        return raw
    name = getattr(raw, "value", raw)
    return _ANCHOR_TYPE_INDEX.get(name) if isinstance(name, str) else None


def get_visual_doc_type(doc: Any) -> str:
    """Return the visual-tool document label used by image and shape helpers.

    Delegates to :func:`plugin.framework.doc_type.get_document_type` for the
    shared Writer/Calc/Draw/Impress map. Web documents are checked first because
    they also support TextDocument and would otherwise look like Writer.
    Unknown models keep the legacy ``\"writer\"`` default (not ``\"unknown\"``).
    """
    try:
        if doc is not None and doc.supportsService(WEB_DOCUMENT_SERVICE):
            return "web"
    except Exception as exc:
        log.debug("get_visual_doc_type: WebDocument probe failed (%s)", type(exc).__name__)

    doc_type = get_document_type(doc)
    if doc_type == DocumentType.UNKNOWN:
        return "writer"
    return doc_type_label_for_enum(doc_type)


def is_writer_like(doc_type: str) -> bool:
    """True for "writer" and "web": text documents whose graphics live in the text, not on draw pages."""
    return doc_type in ("writer", "web")


# 1 inch = 2540 1/100 mm = 96 CSS px. Round both directions: truncating
# px_to_units made units_to_px(px_to_units(10)) return 9.
UNITS_PER_INCH = 2540
PX_PER_INCH = 96


# round(float(mm) * 100) preserves fractional millimetres in 1/100 mm units—int(mm) truncates.
def mm_to_unit(mm: int | float) -> int:
    """Nearest LibreOffice 1/100 mm. Python round (half to even)."""
    return int(round(float(mm) * 100))


def mm_to_units(width_mm: int | float, height_mm: int | float) -> tuple[int, int]:
    return mm_to_unit(width_mm), mm_to_unit(height_mm)


def px_to_units(width_px: int | float, height_px: int | float) -> tuple[int, int]:
    """Convert 96-DPI pixels to LibreOffice 1/100 mm units."""
    return int(round(width_px * UNITS_PER_INCH / PX_PER_INCH)), int(round(height_px * UNITS_PER_INCH / PX_PER_INCH))


# On-page inset for generated (and other px-sized) graphics. API pixels are
# generate resolution, not print size: 1024px at 96 DPI is ~10.7" and overflows
# a Writer page. 512px at 96 DPI was ~135mm. Cap the longer edge there so
# 1024/1536 look sharper at the same footprint, not larger.
GENERATED_IMAGE_MAX_DISPLAY_MM = 135


def px_to_display_units(width_px: int | float, height_px: int | float) -> tuple[int, int]:
    """Convert 96-DPI pixels to 1/100 mm, then cap the longer edge.

    Generate resolution and on-page size are independent. Smaller images are
    not scaled up.
    """
    width_units, height_units = px_to_units(width_px, height_px)
    if width_units <= 0 or height_units <= 0:
        return width_units, height_units
    max_units = mm_to_unit(GENERATED_IMAGE_MAX_DISPLAY_MM)
    longer = max(width_units, height_units)
    if longer <= max_units:
        return width_units, height_units
    scale = max_units / longer
    return round(width_units * scale), round(height_units * scale)


def units_to_px(width_units: int | float, height_units: int | float, *, minimum: int = 1) -> tuple[int, int]:
    """Convert LibreOffice 1/100 mm units to 96-DPI pixels."""
    width_px = int(round(width_units * PX_PER_INCH / UNITS_PER_INCH))
    height_px = int(round(height_units * PX_PER_INCH / UNITS_PER_INCH))
    return max(minimum, width_px), max(minimum, height_px)


def graphic_pixel_size(graphic: Any) -> tuple[int | None, int | None]:
    """Read pixel dimensions from an XGraphic, falling back to a 96-DPI frame from Size100thMM."""
    if graphic is None:
        return None, None
    try:
        px = safe_get_property(graphic, "SizePixel")
        if px is not None and getattr(px, "Width", 0) > 0 and getattr(px, "Height", 0) > 0:
            return int(px.Width), int(px.Height)
    except Exception as ex:
        if is_disposal(ex):
            raise
    try:
        hmm = safe_get_property(graphic, "Size100thMM")
        if hmm is not None and (getattr(hmm, "Width", 0) > 0 or getattr(hmm, "Height", 0) > 0):
            # units_to_px rounds (1000 1/100 mm → 38 px) and applies minimum=1.
            return units_to_px(float(hmm.Width), float(hmm.Height))
    except Exception as ex:
        if is_disposal(ex):
            raise
    return None, None


def pixel_size(graphic: Any) -> tuple[int, int]:
    """The picture's pixel frame that every box refers to."""
    size = graphic_pixel_size(graphic)
    if size[0] is not None and size[1] is not None:
        return size[0], size[1]
    raise ValueError("Cannot determine this picture's pixel size (no SizePixel or Size100thMM). Boxes need a measurable picture.")


def has_uno_property(obj: Any, name: str) -> bool:
    """True when *name* exists on the UNO PropertySet.

    PyUNO can raise while probing missing properties, so visual tools should use
    PropertySetInfo rather than Python attribute checks for UNO properties.
    """
    try:
        psi = obj.getPropertySetInfo()
        return bool(psi.hasPropertyByName(name))
    except Exception as exc:
        reraise_if_disposed(exc)
        log.debug("has_uno_property %s probe failed (%s)", name, type(exc).__name__)
    return False


def safe_set_property(obj: Any, name: str, value: Any) -> bool:
    if not has_uno_property(obj, name):
        return False
    try:
        obj.setPropertyValue(name, value)
        return True
    except Exception as ex:
        reraise_if_disposed(ex)
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


def _pack_rgb(r: int, g: int, b: int) -> int | None:
    if 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255:
        return (r << 16) | (g << 8) | b
    return None


def parse_color_to_uno_int(color_val: Any) -> int | None:
    """Convert hex, named, rgb()/rgba(), int, or RGB tuple/list to a UNO RGB int.

    Returns ``None`` when *color_val* is falsy or unparseable. Integers are masked
    to 24-bit RGB. Callers that only accept strings (e.g. Calc ``set_style``) should
    reject non-strings before calling this.
    """
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
        return _pack_rgb(r, g, b)
    if not isinstance(color_val, str):
        return None

    color_str = color_val.strip().lower()
    norm_name = color_str.replace(" ", "").replace("_", "").replace("-", "")
    if norm_name in _COLOR_NAMES:
        return _COLOR_NAMES[norm_name]

    rgb_match = _RGB_RE.match(color_str)
    if rgb_match:
        return _pack_rgb(int(rgb_match.group(1)), int(rgb_match.group(2)), int(rgb_match.group(3)))

    hex_str = color_str[2:] if color_str.startswith("0x") else color_str.lstrip("#")
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
        results["CharColor"] = parsed is not None and safe_set_property(target, "CharColor", parsed)
    if underline is not None:
        results["CharUnderline"] = safe_set_property(target, "CharUnderline", underline)
    return results


def safe_try_method(obj: Any, method_name: str, *args: Any) -> bool:
    try:
        getattr(obj, method_name)(*args)
        return True
    except Exception as ex:
        reraise_if_disposed(ex)
        log.debug("safe_try_method %s failed: %s", method_name, ex)
    return False


def safe_get_property(obj: Any, name: str, default: Any = None) -> Any:
    return uno_read(lambda: obj.getPropertyValue(name), default)


def is_graphic_object(obj: Any) -> bool:
    if obj is None:
        return False

    # Service probe first—Graphic property reads fail on non-graphic shapes.
    try:
        if obj.supportsService(WRITER_GRAPHIC_SERVICE) or obj.supportsService(_TEXT_GRAPHIC_SERVICE) or obj.supportsService(DRAW_GRAPHIC_SERVICE):
            return True
    except Exception as exc:
        log.debug("is_graphic_object service probe failed (%s)", type(exc).__name__)

    graphic = safe_get_property(obj, "Graphic")
    if graphic is not None:
        return True

    # Some TextGraphicObject instances expose GraphicURL even when Graphic cannot
    # be read directly. The property read is guarded to avoid PyUNO probe errors.
    return safe_get_property(obj, "GraphicURL") is not None


# Every UNO read below uses the method spelling rather than property aliases.
# PyUNO serves getX() and the pseudo-property X from one Invocation
# (pyuno.cxx:PyUNO_getattr, introspection.cxx get-method rule), so a second
# spelling never succeeds where the first failed.
def _controller_selection(model: Any) -> Any | None:
    """Current selection through the model's controller, or None.

    A closed document re-raises DocumentDisposedError (uno_read); any other read
    failure reads as no selection.
    """
    controller = uno_read(lambda: model.getCurrentController(), None)
    if controller is None:
        return None
    return uno_read(lambda: controller.getSelection(), None) or None


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
        except Exception as exc:
            log.debug("graphic %s anchor compare failed (%s)", name, type(exc).__name__)
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

        if not is_writer_like(get_visual_doc_type(doc)):
            graphic = selected_graphic_object(doc)
            if graphic is None:
                return []
            name = _graphic_object_name(graphic)
            return [(name, graphic)]

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
        # Re-raise disposal; other errors return [] so live documents can report no selection.
        reraise_if_disposed(ex)
        log.debug("graphic_objects_in_selection failed: %s", ex)
        return []


def get_active_draw_page(doc: Any, doc_type: str | None = None) -> Any | None:
    """Return the active draw page for Writer, Calc, Draw, or Impress.

    Calc uses the active sheet draw page; Draw/Impress use the current slide;
    Writer uses ``doc.getDrawPage()`` (document canvas for forms/shapes), with or without a controller.
    """
    inside = doc_type or get_visual_doc_type(doc)
    if is_writer_like(inside):
        # Writer has one document draw page (forms, shapes); no controller needed.
        return uno_read(lambda: doc.getDrawPage(), None)
    # A closed document raises DocumentDisposedError (uno_read) so execute_safe reports
    # DOCUMENT_DISPOSED; any other failure stays on the missing-page path.
    controller = uno_read(lambda: doc.getCurrentController(), None)
    if controller is None:
        return None
    if inside == "calc":
        return uno_read(lambda: controller.getActiveSheet().getDrawPage(), None)
    page = uno_read(lambda: controller.getCurrentPage(), FAILED)
    if page is not FAILED:
        return page

    # getCurrentPage() failed without disposal: take the first page of the deck.
    def first_page() -> Any | None:
        pages = doc.getDrawPages()
        return pages.getByIndex(0) if pages.getCount() > 0 else None

    return uno_read(first_page, None)


# Calc: page_index selects sheet.getDrawPage(); getDrawPages() is Draw/Impress-only.
def draw_page_for_insert(model: Any, inside: str, page_index: int | None = None) -> Any:
    if page_index is not None and inside in ("draw", "impress", "calc"):
        calc = inside == "calc"
        container = model.getSheets() if calc else model.getDrawPages()
        count = container.getCount()
        if page_index < 0 or page_index >= count:
            label = "Sheet" if calc else "Page"
            raise IndexError("%s index %s out of range (0..%s)." % (label, page_index, count - 1))
        item = container.getByIndex(page_index)
        return item.getDrawPage() if calc else item
    draw_page = get_active_draw_page(model, inside)
    if draw_page is None:
        raise RuntimeError(f"Could not resolve draw page for {inside}")
    return draw_page


def position_on_draw_page(draw_page: Any, width: int, height: int, x_mm: int | float | None, y_mm: int | float | None) -> Any:
    if x_mm is not None or y_mm is not None:
        x = mm_to_unit(x_mm) if x_mm is not None else 0
        y = mm_to_unit(y_mm) if y_mm is not None else 0
        return Point(x, y)
    return Point((draw_page.Width - width) // 2, (draw_page.Height - height) // 2)


class GraphicEntry(tuple[str, Any]):
    """(name, graphic) pair preserving Calc sheet metadata when available."""

    sheet_index: int | None = None
    sheet_name: str | None = None


class AmbiguousGraphicNameError(ValueError):
    """Raised when looking up a graphic by name matches multiple objects across sheets."""

    def __init__(self, message: str, sheet_indices: list[int] | None = None) -> None:
        super().__init__(message)
        self.sheet_indices: list[int] = sheet_indices or []


def _graphics_on_page(page: Any) -> Iterator[tuple[str, Any]]:
    """Yield ``(name, shape)`` for every graphic shape on one draw page, in page order.

    Non-graphic shapes are skipped. Errors propagate so the caller's disposal arm
    decides; a closed document must not read as "no graphics".
    """
    for i in range(page.getCount()):
        shape = page.getByIndex(i)
        if is_graphic_object(shape):
            yield shape.getName(), shape


def list_graphic_objects(doc: Any, doc_type: str | None = None) -> list[tuple[str, Any]]:
    """Return ``(name, object)`` pairs for document-level graphic objects."""
    inside = doc_type or get_visual_doc_type(doc)
    graphics: list[tuple[str, Any]] = []

    if inside == "calc":
        try:
            sheets: Any = doc.getSheets()
            for sheet_idx in range(sheets.getCount()):
                sheet = sheets.getByIndex(sheet_idx)
                sheet_name = sheet.getName()
                for name, shape in _graphics_on_page(sheet.getDrawPage()):
                    item = GraphicEntry((name, shape))
                    item.sheet_index = sheet_idx
                    item.sheet_name = sheet_name
                    graphics.append(item)
            return graphics
        except Exception as ex:
            if is_disposal(ex):
                raise
            log.debug("list_graphic_objects calc sheets failed: %s", ex)

        draw_page = get_active_draw_page(doc, inside)
        if draw_page is None:
            return graphics
        try:
            graphics.extend(_graphics_on_page(draw_page))
        except Exception as ex:
            if is_disposal(ex):
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
                if is_disposal(ex):
                    raise
                log.debug("list_graphic_objects draw/impress pages failed: %s", ex)
        if not page_list:
            draw_page = get_active_draw_page(doc, inside)
            if draw_page is not None:
                page_list = [draw_page]
        try:
            for draw_page in page_list:
                graphics.extend(_graphics_on_page(draw_page))
        except Exception as ex:
            if is_disposal(ex):
                raise
            log.debug("list_graphic_objects draw/impress failed: %s", ex)
        return graphics

    try:
        graphic_objects: Any = doc.getGraphicObjects()
        for name in graphic_objects.getElementNames():
            graphics.append((name, graphic_objects.getByName(name)))
    except Exception as ex:
        if is_disposal(ex):
            raise
        log.debug("list_graphic_objects writer failed: %s", ex)
    return graphics


def remove_graphic_from_draw_pages(doc: Any, graphic: Any) -> bool:
    """Remove a GraphicObjectShape from Calc/Draw/Impress pages. Returns True if removed."""
    if graphic is None or doc is None:
        return False
    target_name = uno_read(lambda: str(graphic.getName() or ""), "")

    inside = get_visual_doc_type(doc)
    pages_list: list[Any] = []
    if inside == "calc":
        try:
            sheets: Any = doc.getSheets()
            for i in range(sheets.getCount()):
                pages_list.append(sheets.getByIndex(i).getDrawPage())
        except Exception as exc:
            if is_disposal(exc):
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
            if is_disposal(exc):
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
                # Identity match first: == on PyUNO wrappers is UNO identity (pyuno.cxx
                # PyUNO_cmp), so a shape read fresh from the page matches the graphic
                # resolved earlier by name; the dev thread-guard proxy's __eq__ unwraps its
                # right operand so proxied pairs match too.
                if shape == graphic:
                    page.remove(graphic)
                    return True
                # Without an identity match, collect same-name hits and delete only when unique on the page.
                if target_name and uno_read(lambda: str(shape.getName() or "") == target_name, False):
                    name_hits.append((page, shape))
    except Exception as ex:
        # Re-raise disposal; other errors return False (IMAGE_NOT_FOUND on live docs).
        if is_disposal(ex):
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
    matches = [(item[1], getattr(item, "sheet_index", None)) for item in list_graphic_objects(doc, doc_type=doc_type) if item[0] == image_name]
    if sheet_index is not None:
        return next((graphic for graphic, idx in matches if idx == sheet_index), None)
    if len(matches) == 1:
        return matches[0][0]
    if len(matches) < 2:
        return None
    sheet_indices = [idx for _g, idx in matches if idx is not None]
    sheets_info = f" (sheets {sheet_indices})" if sheet_indices else ""
    raise AmbiguousGraphicNameError(
        f"Several shapes named '{image_name}'{sheets_info}; specify sheet_index.",
        sheet_indices=sheet_indices,
    )


def graphic_from_object(obj: Any) -> Any | None:
    """Return the UNO Graphic for a text graphic or draw GraphicObjectShape."""
    if obj is None:
        return None
    graphic = safe_get_property(obj, "Graphic")
    if graphic is not None:
        return graphic
    try:
        return obj.Graphic
    except Exception as ex:
        reraise_if_disposed(ex)
        log.debug("graphic_from_object missing Graphic: %s", ex)
    return None
