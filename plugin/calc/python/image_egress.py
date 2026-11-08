# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Insert matplotlib image payloads on Calc sheets (=PYTHON / chat tool).

Supports multiple plot shapes per cell via indexed shape slots with reuse.
"""

from __future__ import annotations

from functools import partial
import hashlib
import logging
import os
import re
import struct
from typing import Any, NoReturn

from plugin.framework.errors import best_effort
from plugin.framework.queue_executor import main
from plugin.scripting.payload_codec import write_image_payload_to_temp

log = logging.getLogger(__name__)


# Default chart overlay size for unmerged single cells: 10 cm x 6 cm (10000 x 6000 in 1/100 mm).
# Design decision: When a user merges a block of cells (e.g. B2:H18) as a chart placeholder,
# we fit the shape to that merged area (ResizeWithCell=True). For an ordinary 1x1 cell or a
# thin 1-row merged strip (e.g. A1:H1 banner), setting shape size to cell_size would crush
# the chart to a tiny sliver; instead we use DEFAULT_CHART_SIZE and keep ResizeWithCell=False.
DEFAULT_CHART_SIZE_WIDTH = 10000
DEFAULT_CHART_SIZE_HEIGHT = 6000
MIN_CHART_PLACEHOLDER_WIDTH = 4000
MIN_CHART_PLACEHOLDER_HEIGHT = 3000

# Only shapes we created for a plot are replaced on the next insert. A user
# image is also a GraphicObjectShape; matching the anchor alone overwrote it.
_DRAW_GRAPHIC_SERVICE = "com.sun.star.drawing.GraphicObjectShape"
_PLOT_SHAPE_NAME_PREFIX = "WriterAgentPlot"
_IMAGE_ANNOTATION_PREFIX = "WriterAgent image: "


def _plot_slot_name(image_index: int) -> str:
    """Slot name for a plot shape: WriterAgentPlot for 0, WriterAgentPlot_{index+1} for rest."""
    return _PLOT_SHAPE_NAME_PREFIX if image_index == 0 else f"{_PLOT_SHAPE_NAME_PREFIX}_{image_index + 1}"


def _plot_slot_index(name: str) -> int | None:
    """Slot index: 0 for WriterAgentPlot, N-1 for WriterAgentPlot_N (N>=2).

    Only WriterAgentPlot or WriterAgentPlot_N (N≥2, no sign/underscore/leading zero) are slots;
    loose int() parsing mis-reads names like WriterAgentPlot_1_0.
    """
    match = re.fullmatch(
        rf"{re.escape(_PLOT_SHAPE_NAME_PREFIX)}(?:_([2-9]|[1-9][0-9]+))?",
        name,
    )
    if match is None:
        return None
    suffix = match.group(1)
    return 0 if suffix is None else int(suffix) - 1


_SVG_UNIT_TO_MM = {
    "px": 25.4 / 96.0,
    "pt": 0.3528,
    "in": 25.4,
    "cm": 10.0,
    "mm": 1.0,
}


def _parse_svg_dimension(val_str: str) -> float | None:
    m = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*(pt|px|in|cm|mm)?\s*$", val_str)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2) or "px"
    return num * _SVG_UNIT_TO_MM[unit]


def _intrinsic_aspect(data: bytes) -> tuple[int, int] | None:
    """Parse intrinsic width and height from PNG or SVG image bytes."""
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        if w > 0 and h > 0:
            return (w, h)
        return None

    text = data[:8192].decode("utf-8", "ignore")

    svg_match = re.search(r"<svg\b([^>]*)>", text, re.IGNORECASE)
    if svg_match:
        attrs = svg_match.group(1)
        w_match = re.search(r'\bwidth\s*=\s*["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        h_match = re.search(r'\bheight\s*=\s*["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        if w_match and h_match:
            w_val = _parse_svg_dimension(w_match.group(1))
            h_val = _parse_svg_dimension(h_match.group(1))
            if w_val is not None and h_val is not None and w_val > 0 and h_val > 0:
                return (round(w_val * 1000), round(h_val * 1000))
        vb_match = re.search(r'\bviewBox\s*=\s*["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        if vb_match:
            parts = re.split(r"[\s,]+", vb_match.group(1).strip())
            if len(parts) >= 4:
                try:
                    vb_w = float(parts[2])
                    vb_h = float(parts[3])
                    if vb_w > 0 and vb_h > 0:
                        return (round(vb_w * 1000), round(vb_h * 1000))
                except ValueError:
                    pass
    return None


# Session cache of plot shapes by cell address and slot index.
# Allows chained slot placement when stock Calc has re-anchored shapes off the formula cell.
_session_slots: dict[tuple[Any, int, int], dict[int, Any]] = {}


def _session_slot_key(sheet: Any, col: int, row: int) -> tuple[Any, int, int]:
    try:
        hash(sheet)
        sheet_key = sheet
    except (TypeError, Exception):
        sheet_key = id(sheet)
    return (sheet_key, col, row)


def _record_session_slot(sheet: Any, target_cell: Any, index: int, shape: Any) -> None:
    if target_cell is None:
        return
    addr_key = _cell_address_key(target_cell)
    if addr_key is None:
        return
    key = _session_slot_key(sheet, addr_key[1], addr_key[2])
    _session_slots.setdefault(key, {})[index] = shape


def _drop_session_slot(sheet: Any, target_cell: Any, index: int) -> None:
    if target_cell is None:
        return
    addr_key = _cell_address_key(target_cell)
    if addr_key is None:
        return
    key = _session_slot_key(sheet, addr_key[1], addr_key[2])
    if key in _session_slots:
        _session_slots[key].pop(index, None)


def _aspect_size(aspect: tuple[int, int] | None, long_side: int = DEFAULT_CHART_SIZE_WIDTH) -> Any:
    """Calculate Size maintaining aspect ratio, with the longer side set to long_side."""
    from com.sun.star.awt import Size

    if aspect is None:
        if long_side == DEFAULT_CHART_SIZE_WIDTH:
            return Size(DEFAULT_CHART_SIZE_WIDTH, DEFAULT_CHART_SIZE_HEIGHT)
        return Size(long_side, round(long_side * DEFAULT_CHART_SIZE_HEIGHT / DEFAULT_CHART_SIZE_WIDTH))
    w, h = aspect
    major = max(w, h)
    minor = min(w, h)
    short_side = max(1000, round(long_side * minor / major))
    if w >= h:
        return Size(long_side, short_side)
    else:
        return Size(short_side, long_side)


def _aspect_ratio_changed(old_w: int, old_h: int, new_w: int, new_h: int) -> bool:
    """True when new aspect ratio differs from old aspect ratio by more than 1%."""
    if old_w <= 0 or old_h <= 0 or new_w <= 0 or new_h <= 0:
        return False
    old_ratio = old_w / old_h
    new_ratio = new_w / new_h
    return abs(old_ratio - new_ratio) > 0.01 * max(old_ratio, new_ratio)


def _plot_byte_tag(data: bytes) -> str:
    """Format the alt text tag representing the SHA-256 digest of plot image bytes."""
    return f"wa-plot:{hashlib.sha256(data).hexdigest()}"


def _bytes_match(shape: Any, data: bytes) -> bool:
    """True when shape's Description matches the SHA-256 digest of data.

    Any other description, including empty string, is not a match.
    """
    try:
        desc = shape.getPropertyValue("Description")
        return isinstance(desc, str) and desc == _plot_byte_tag(data)
    except (AttributeError, TypeError, Exception):
        return False


def _record_plot_byte_tag(shape: Any, data: bytes) -> None:
    """Write SHA-256 byte tag to shape Description when allowed.

    Preserves custom user alt text: writes only when Description is empty
    or already starts with the wa-plot: prefix.
    """
    try:
        desc = shape.getPropertyValue("Description")
        if not isinstance(desc, str) or not desc or desc.startswith("wa-plot:"):
            shape.setPropertyValue("Description", _plot_byte_tag(data))
    except (AttributeError, TypeError, Exception):
        log.debug("could not record plot byte tag", exc_info=True)


def _sheet_is_rtl(sheet: Any) -> bool:
    """True when sheet writing direction is right-to-left (TableLayout == 1)."""
    try:
        return int(sheet.getPropertyValue("TableLayout")) == 1
    except (AttributeError, TypeError, ValueError, Exception):
        try:
            return int(sheet.TableLayout) == 1
        except (AttributeError, TypeError, ValueError, Exception):
            return False


def _is_visible(obj: Any) -> bool:
    """Check IsVisible attribute or UNO property, defaulting to visible (True)."""
    try:
        return bool(obj.IsVisible)
    except (AttributeError, TypeError, Exception):
        try:
            return bool(obj.getPropertyValue("IsVisible"))
        except (AttributeError, TypeError, Exception):
            return True


def _formula_axis_hidden(sheet: Any, cell: Any) -> bool:
    """True when the formula cell's row or column is hidden.

    Checks IsVisible on the formula cell's row and column. Missing API means visible.
    """
    try:
        addr = cell.getCellAddress()
        row_idx = int(addr.Row)
        col_idx = int(addr.Column)

        row_hidden = False
        try:
            row_hidden = not _is_visible(sheet.getRows().getByIndex(row_idx))
        except (AttributeError, TypeError, IndexError, Exception):
            log.debug("could not inspect formula cell row visibility", exc_info=True)
        if row_hidden:
            return True

        col_hidden = False
        try:
            col_hidden = not _is_visible(sheet.getColumns().getByIndex(col_idx))
        except (AttributeError, TypeError, IndexError, Exception):
            log.debug("could not inspect formula cell column visibility", exc_info=True)
        if col_hidden:
            return True
    except (AttributeError, TypeError, Exception):
        log.debug("could not resolve formula cell address for visibility", exc_info=True)
    return False


def _size_width(size: Any) -> int:
    try:
        return int(size.Width)
    except (AttributeError, TypeError, IndexError, KeyError):
        try:
            return int(size[1])
        except (AttributeError, TypeError, IndexError, KeyError):
            return 0


def _size_height(size: Any) -> int:
    try:
        return int(size.Height)
    except (AttributeError, TypeError, IndexError, KeyError):
        try:
            return int(size[2])
        except (AttributeError, TypeError, IndexError, KeyError):
            return 0


def _pos_x(pos: Any) -> int:
    try:
        return int(pos.X)
    except (AttributeError, TypeError, IndexError, KeyError):
        try:
            return int(pos[1])
        except (AttributeError, TypeError, IndexError, KeyError):
            return 0


def _pos_y(pos: Any) -> int:
    try:
        return int(pos.Y)
    except (AttributeError, TypeError, IndexError, KeyError):
        try:
            return int(pos[2])
        except (AttributeError, TypeError, IndexError, KeyError):
            return 0


def _slot_edge(pos: Any, size: Any, rtl: bool) -> int:
    """Calculate the far outer edge coordinate for slot chaining."""
    x = _pos_x(pos)
    if rtl:
        return x
    return x + _size_width(size)


def _resolve_slot(
    draw_page: Any,
    target_cell: Any,
    sheet: Any,
    index: int,
    slots: list[tuple[Any, int]],
) -> Any | None:
    """Resolve an existing plot shape by slot index.

    First checks shapes anchored to target_cell on draw_page. If not found, falls back
    to shapes recorded in _session_slots for this cell, provided getPosition still works.
    """
    shape = next((s for s, slot_idx in slots if slot_idx == index), None)
    if shape is not None:
        return shape
    if target_cell is None:
        return None
    addr_key = _cell_address_key(target_cell)
    if addr_key is None:
        return None
    cell_key = _session_slot_key(sheet, addr_key[1], addr_key[2])
    candidate = _session_slots.get(cell_key, {}).get(index)
    if candidate is not None:
        try:
            candidate.getPosition()
            return candidate
        except (AttributeError, TypeError, Exception):
            _session_slots.get(cell_key, {}).pop(index, None)
    return None


def _cursor_x(
    draw_page: Any,
    sheet: Any,
    target_cell: Any,
    cell_pos: Any,
    cell_size: Any,
    image_index: int,
    slots: list[tuple[Any, int]],
    rtl: bool,
) -> tuple[int, int]:
    """Calculate the cursor (x, y) for placing the shape at image_index.

    Starts at the merged visual edge (Position.X in LTR, Position.X + Width in RTL).
    For each slot index < image_index, if the slot shape is resolved, advances the
    cursor to that shape's live outer edge and aligns to its top Y. If any intermediate
    slot is missing, stops advancing and places at the last known edge.
    """
    cursor_x = _pos_x(cell_pos) + _size_width(cell_size) if rtl else _pos_x(cell_pos)
    cursor_y = _pos_y(cell_pos)

    for idx in range(image_index):
        prev_shape = _resolve_slot(draw_page, target_cell, sheet, idx, slots)
        if prev_shape is None:
            break
        try:
            pos = prev_shape.getPosition()
            size = prev_shape.getSize()
            cursor_x = _slot_edge(pos, size, rtl)
            cursor_y = _pos_y(pos)
        except (AttributeError, TypeError, Exception):
            break

    return cursor_x, cursor_y


def _anchored_plot_slots(draw_page: Any, target_cell: Any) -> list[tuple[Any, int]]:
    """Return (shape, slot_index) for all plot shapes anchored to target_cell."""
    try:
        slots: list[tuple[Any, int]] = []
        for index in range(draw_page.getCount()):
            shape = draw_page.getByIndex(index)
            if not _supports_service(shape, _DRAW_GRAPHIC_SERVICE):
                continue
            name = shape.getPropertyValue("Name")
            slot_idx = _plot_slot_index(name if isinstance(name, str) else "")
            if slot_idx is None:
                continue
            if not _shape_anchor_matches_cell(shape, target_cell):
                continue
            slots.append((shape, slot_idx))
        return slots
    except Exception:
        log.debug("_anchored_plot_slots scan failed", exc_info=True)
        return []


def _drop_extra_plot_slots(
    draw_page: Any,
    slots: list[tuple[Any, int]],
    image_count: int,
    *,
    sheet: Any = None,
    target_cell: Any = None,
) -> None:
    """Remove existing plot shapes on target_cell with slot index >= image_count."""
    cell_key = None
    if sheet is not None and target_cell is not None:
        addr_key = _cell_address_key(target_cell)
        if addr_key is not None:
            cell_key = _session_slot_key(sheet, addr_key[1], addr_key[2])

    for s, slot_idx in slots:
        if slot_idx >= image_count:
            best_effort(partial(draw_page.remove, s), "_drop_extra_plot_slots: remove shape")
            if cell_key is not None and cell_key in _session_slots:
                _session_slots[cell_key].pop(slot_idx, None)

    if cell_key is not None and cell_key in _session_slots:
        stale_indices = [idx for idx in _session_slots[cell_key] if idx >= image_count]
        for idx in stale_indices:
            _session_slots[cell_key].pop(idx, None)


def _read_image_bytes(payload: dict[str, Any] | None, tmp_path: str | None) -> bytes | None:
    if tmp_path:
        try:
            with open(tmp_path, "rb") as f:
                return f.read()
        except OSError:
            pass
    if payload and isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
    return None


def _located_image_cell(doc: Any, code: str | None) -> tuple[Any, Any, tuple[int, int]] | None:
    if not code or doc is None:
        return None
    try:
        from plugin.calc.python.formula_locator_cache import locate_formula_cell_in_doc

        return locate_formula_cell_in_doc(doc, code)
    except Exception:
        return None


def _set_image_cell_annotation(doc: Any, code: str | None, error_message: str) -> None:
    """Set error annotation on the located formula cell after off-main egress failure."""
    located = _located_image_cell(doc, code)
    if located is None:
        return
    sheet, cell, _unused = located
    try:
        text = f"{_IMAGE_ANNOTATION_PREFIX}{error_message}"
        ann = cell.getAnnotation()
        # ScCellObj::getAnnotation (cellsuno.cxx) always returns a ScAnnotationObj.
        note = ann.getString() or ""
        if note.startswith(_IMAGE_ANNOTATION_PREFIX):
            ann.setString(text)
            return
        if note:
            # User notes stay: setString only for WriterAgent image: prefix; insertNew only when empty
            # (ReplaceNote would overwrite the cell's sole annotation).
            log.debug("_set_image_cell_annotation: user annotation left in place")
            return
        sheet.getAnnotations().insertNew(cell.getCellAddress(), text)
    except Exception:
        log.debug("_set_image_cell_annotation failed", exc_info=True)


def _clear_image_cell_annotation(doc: Any, code: str | None) -> None:
    """Remove WriterAgent image: annotation on the located formula cell on success."""
    located = _located_image_cell(doc, code)
    if located is None:
        return
    sheet, cell, (row, col) = located
    try:
        ann = cell.getAnnotation()
        val = ann.getString() or ""
        if val.startswith(_IMAGE_ANNOTATION_PREFIX):
            annotations = sheet.getAnnotations()
            for i in range(annotations.getCount()):
                item = annotations.getByIndex(i)
                pos = item.getPosition() if hasattr(item, "getPosition") else None
                if pos is not None and getattr(pos, "Column", None) == col and getattr(pos, "Row", None) == row:
                    annotations.removeByIndex(i)
                    break
            else:
                ann.setString("")
    except Exception:
        log.debug("_clear_image_cell_annotation failed", exc_info=True)


class ImageEgressError(Exception):
    """Raised when a Calc plot was not placed on the sheet.

    Raised when document, sheet, or draw page is missing so callers cannot report a successful insert.
    """


def _egress_fail(message: str) -> NoReturn:
    """Log *message* and raise so callers cannot report a successful insert."""
    log.debug("insert_image_result_on_sheet: %s", message)
    raise ImageEgressError(message)


def _cell_address_key(cell: Any) -> tuple[Any, int, int] | None:
    """Sheet + column + row for a cell-like UNO object, or None."""
    if hasattr(cell, "getRangeAddress"):
        addr = cell.getRangeAddress()
        sheet_idx = getattr(addr, "Sheet", None)
        return (sheet_idx, int(addr.StartColumn), int(addr.StartRow))
    if hasattr(cell, "getCellAddress"):
        addr = cell.getCellAddress()
        return (getattr(addr, "Sheet", None), int(addr.Column), int(addr.Row))
    return None


def _supports_service(obj: Any, name: str) -> bool:
    """True only when obj.supportsService(name) returns True or 1."""
    supports = getattr(obj, "supportsService", None)
    if not callable(supports):
        return False
    hit = supports(name)
    return hit is True or (type(hit) is int and hit == 1)


def _shape_anchor_matches_cell(shape: Any, target_cell: Any) -> bool:
    """True when the shape is already anchored to the same grid cell (not UNO identity).

    A page-anchored shape's Anchor is the sheet (range 0,0); that must not count as matching a cell.
    """
    anchor = shape.getPropertyValue("Anchor")
    if _supports_service(anchor, "com.sun.star.sheet.Spreadsheet"):
        return False
    left = _cell_address_key(anchor)
    right = _cell_address_key(target_cell)
    return left is not None and left == right


def _unlink_temp_image(path: str) -> None:
    """Best-effort delete of the PNG/SVG written for GraphicURL.

    The GraphicURL setter reads the file before it returns and stores the
    bitmap on the shape (desktop GraphicObjectShape embeds on set). The path
    is not a live link after that call, so unlink in ``finally`` — including
    when a later anchor step fails — instead of leaking a file per recalc.
    A build that kept only the URL would show a broken picture; that is not
    what the desktop setter does.
    """
    try:
        os.unlink(path)
    except OSError:
        log.debug("insert_image_result_on_sheet: temp image cleanup failed for %s", path, exc_info=True)


def insert_image_result_on_sheet(
    ctx: Any,
    payload: dict[str, Any],
    *,
    code: str | None = None,
    doc: Any | None = None,
    image_index: int = 0,
    image_path: str | None = None,
    image_count: int | None = None,
    notify_cell: bool = False,
) -> None:
    """Write image payload bytes to a temp file and insert as a cell-anchored shape on the target sheet.

    Posts execution asynchronously to the main VCL UI thread if invoked from a background worker thread.
    *doc* is required (=PY() passes its caller argument; tools pass their target
    document). With no doc the insert fails rather than using the front window.

    Raises:
        ImageEgressError: the synchronous (main-thread) insert did not place a graphic.
            Off-main, this function posts and returns; the posted call's exception is
            caught and logged by MainThread.process_queue when notify_cell is false.
            When notify_cell is true, a posted ImageEgressError is written to the formula
            cell's note only if that note is empty or already starts with
            "WriterAgent image: "; any other note is left unchanged. main.post
            can still raise TimeoutError or RuntimeError to this caller.
    """
    # Thread safety invariant: Drawing layer manipulation (DrawPage, GraphicObjectShape, cell geometry)
    # must run on LibreOffice's main VCL thread to prevent internal C++ state corruption and deadlocks.
    # If called from a background recalculation or script worker thread, post asynchronously to the main thread.
    # The main-thread path lets ImageEgressError propagate and the off-main notify_cell path annotates
    # instead, because the formula has already been answered by the time the posted callable runs.
    def _run() -> None:
        _insert_image_result_on_sheet_impl(
            payload,
            code=code,
            doc=doc,
            image_index=image_index,
            image_path=image_path,
            image_count=image_count,
        )
        if notify_cell:
            _clear_image_cell_annotation(doc, code)

    is_ui_thread = main.is_current()
    if not is_ui_thread:
        def _off_main_wrapper() -> None:
            try:
                _run()
            except ImageEgressError as err:
                if notify_cell:
                    _set_image_cell_annotation(doc, code, str(err))
                else:
                    raise

        main.post(_off_main_wrapper)
        return

    _run()


def _insert_image_result_on_sheet_impl(
    payload: dict[str, Any],
    code: str | None = None,
    doc: Any | None = None,
    image_index: int = 0,
    *,
    image_path: str | None = None,
    image_count: int | None = None,
) -> None:
    """Main-thread implementation of graphic shape creation and anchoring."""
    import uno
    from com.sun.star.awt import Point

    # Resolve the target sheet and cell from the formula when the code is
    # known. During a workbook recalc (Ctrl+Shift+F9, or file open) the
    # active sheet can be sheet 0 while the formula is on another sheet, and
    # a one-row merged selection there crushes the chart to about 12.7mm.
    # Fall back to the active sheet and selection, and keep a minimum size
    # when the anchor is a merged range.
    #
    # Raise ImageEgressError when the document, sheet, or draw page is
    # missing, or when UNO throws. Logging and returning made =PY(), the
    # venv, and plot_data report the picture as inserted.
    created_path: str | None = None
    try:
        from plugin.calc.calc_utils import get_cell_geometry

        # Callers pass the document (=PY() passes its caller argument). Do not
        # fall back to the front window.
        if doc is None:
            _egress_fail("no Calc document for image insertion")

        sheet = None
        target_cell = None

        if code:
            located = _located_image_cell(doc, code)
            if located is None:
                _egress_fail("could not locate formula cell for formula code; image was not inserted")
            sheet, target_cell, _unused = located

        ctrl = doc.getCurrentController()

        if sheet is None:
            if ctrl is not None and ctrl.getActiveSheet():
                sheet = ctrl.getActiveSheet()
            elif hasattr(doc, "getSheets") and doc.getSheets().getCount() > 0:
                sheet = doc.getSheets().getByIndex(0)
            else:
                _egress_fail("could not resolve a sheet for image insertion")

        draw_page = getattr(sheet, "DrawPage", None)
        if draw_page is None:
            _egress_fail("target sheet has no DrawPage; image was not inserted")

        if target_cell is None and ctrl is not None:
            try:
                addr = ctrl.getSelection().getRangeAddress()
                target_cell = sheet.getCellByPosition(addr.StartColumn, addr.StartRow)
            except Exception:
                log.debug("insert_image_result_on_sheet: selection fallback failed", exc_info=True)

        if image_path:
            tmp_path = image_path
        elif payload.get("image_path"):
            tmp_path = str(payload["image_path"])
        else:
            created_path = write_image_payload_to_temp(payload)
            tmp_path = created_path
        file_url = uno.systemPathToFileUrl(os.path.abspath(tmp_path))

        data_bytes = _read_image_bytes(payload, tmp_path)
        aspect = _intrinsic_aspect(data_bytes) if data_bytes else None
        aspect_size = _aspect_size(aspect)

        slots = _anchored_plot_slots(draw_page, target_cell) if target_cell is not None else []
        shape = _resolve_slot(draw_page, target_cell, sheet, image_index, slots)
        rtl = _sheet_is_rtl(sheet)

        created_shape = False
        if shape is None:
            shape = doc.createInstance(_DRAW_GRAPHIC_SERVICE)
            shape.setSize(aspect_size)
            draw_page.add(shape)
            created_shape = True
            shape.setPropertyValue("Name", _plot_slot_name(image_index))

        if created_shape:
            graphic_ok = True
            try:
                # Setter loads the file into the shape's Graphic before returning.
                # _unlink_temp_image runs in finally, after this call.
                shape.setPropertyValue("GraphicURL", file_url)
            except (RuntimeError, Exception):
                # A failed graphic load leaves the newly created shape on the drawing page
                # as an empty graphic placeholder. LibreOffice renders this as a draft placeholder
                # while keeping the slot allocated for future updates.
                graphic_ok = False

            if target_cell is not None:
                try:
                    cell_pos, cell_size = get_cell_geometry(sheet, target_cell)
                    try:
                        is_merged = bool(target_cell.IsMerged)
                    except (AttributeError, TypeError):
                        is_merged = False
                    width = _size_width(cell_size)
                    height = _size_height(cell_size)
                    is_large_placeholder = (
                        is_merged
                        and width >= MIN_CHART_PLACEHOLDER_WIDTH
                        and height >= MIN_CHART_PLACEHOLDER_HEIGHT
                    )

                    cursor_x, cursor_y = _cursor_x(
                        draw_page, sheet, target_cell, cell_pos, cell_size, image_index, slots, rtl
                    )

                    shape.setPropertyValue("Anchor", target_cell)

                    # Only slot 0 can fill a merged placeholder block. Later slots are placed
                    # beside the merged block at their intrinsic aspect ratio so they do not stack
                    # on top of each other or stretch to the same placeholder.
                    if image_index == 0 and is_large_placeholder:
                        shape.setPropertyValue("ResizeWithCell", True)
                        shape.setSize(cell_size)
                        target_width = width
                    else:
                        shape.setPropertyValue("ResizeWithCell", False)
                        shape.setSize(aspect_size)
                        target_width = _size_width(aspect_size)

                    # Each successive plot slot chains horizontally from the outer edge of the
                    # preceding slot. Reading the live rectangle of each resolved slot ensures
                    # slots fit flush against each other without gaps or overlaps.
                    if image_index == 0 and not rtl:
                        shape.setPosition(cell_pos)
                    else:
                        place_x = cursor_x - target_width if rtl else cursor_x
                        shape.setPosition(Point(place_x, cursor_y))

                    # When the formula cell's row or column is hidden, the plot shape is also hidden.
                    # Otherwise, shapes anchored to hidden cells would remain floating visibly over adjacent content.
                    if _formula_axis_hidden(sheet, target_cell):
                        shape.setPropertyValue("Visible", False)

                    _record_session_slot(sheet, target_cell, image_index, shape)
                    if graphic_ok and data_bytes is not None:
                        _record_plot_byte_tag(shape, data_bytes)
                except (RuntimeError, Exception):
                    best_effort(
                        lambda: draw_page.remove(shape),
                        "insert_image_result_on_sheet: failed to remove failed shape",
                    )
                    _drop_session_slot(sheet, target_cell, image_index)
                    raise ImageEgressError("could not anchor plot to the formula cell")
        else:
            if data_bytes is not None and _bytes_match(shape, data_bytes):
                # LibreOffice's UNO API does not expose the raw graphic link or byte identity of
                # an embedded GraphicObjectShape. We store the SHA-256 digest tag in Description
                # so subsequent recalculations can detect unchanged image bytes and avoid reloading
                # the graphic or resetting user adjustments.
                _record_session_slot(sheet, target_cell, image_index, shape)
            else:
                graphic_ok = True
                try:
                    shape.setPropertyValue("GraphicURL", file_url)
                except (RuntimeError, Exception):
                    # Leave existing picture untouched when graphic update fails.
                    graphic_ok = False

                if graphic_ok:
                    if data_bytes is not None:
                        _record_plot_byte_tag(shape, data_bytes)

                    try:
                        resize_with_cell = bool(shape.getPropertyValue("ResizeWithCell"))
                    except (AttributeError, TypeError, Exception):
                        resize_with_cell = False

                    if not resize_with_cell:
                        try:
                            old_pos = shape.getPosition()
                            old_size = shape.getSize()
                            old_w = _size_width(old_size)
                            old_h = _size_height(old_size)
                            old_x = _pos_x(old_pos)
                            old_y = _pos_y(old_pos)

                            if aspect is not None and _aspect_ratio_changed(old_w, old_h, aspect[0], aspect[1]):
                                new_size = _aspect_size(aspect, long_side=max(old_w, old_h))
                                shape.setSize(new_size)
                                if rtl:
                                    new_x = old_x + old_w - _size_width(new_size)
                                    shape.setPosition(Point(new_x, old_y))
                        except (AttributeError, TypeError, Exception):
                            log.debug("could not update existing plot geometry", exc_info=True)

                    _record_session_slot(sheet, target_cell, image_index, shape)

        # Only a caller that passes image_count owns the slot family. =PY() and
        # run_venv_python_script pass it and enumerate image_index from 0, so the
        # sweep runs once on index 0. Omitting image_count (chat plot_data / viz)
        # must not remove sibling WriterAgentPlot_N shapes; they may belong to a
        # formula on the selected cell.
        if image_count is not None and image_index == 0 and target_cell is not None:
            _drop_extra_plot_slots(draw_page, slots, image_count, sheet=sheet, target_cell=target_cell)
    except ImageEgressError:
        raise
    except Exception as exc:
        log.exception("insert_image_result_on_sheet failed to insert graphic shape")
        raise ImageEgressError("insert_image_result_on_sheet failed to insert graphic shape") from exc
    finally:
        if created_path is not None:
            _unlink_temp_image(created_path)

