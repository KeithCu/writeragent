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
# image is also a GraphicObjectShape, so matching on the anchor alone would
# overwrite it; the ``WriterAgentPlot`` name prefix is checked too.
_DRAW_GRAPHIC_SERVICE = "com.sun.star.drawing.GraphicObjectShape"
_PLOT_SHAPE_NAME_PREFIX = "WriterAgentPlot"
_IMAGE_ANNOTATION_PREFIX = "WriterAgent image: "

# Each plot shape carries a wa-plot:<col>,<row>[:<sha>] tag naming its formula cell and
# the SHA-256 digest of its image. It lives in UserDefinedAttributes, which ODF keeps as a
# namespaced attribute on the shape, so the shape's Description (alt text) stays the user's.
_PLOT_TAG_ATTR = "wa:plot"
_PLOT_TAG_NAMESPACE = "urn:writeragent:plot"


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


def _aspect_size(aspect: tuple[int, int] | None, long_side: int = DEFAULT_CHART_SIZE_WIDTH) -> Any:
    """Calculate Size maintaining aspect ratio, with the longer side set to long_side."""
    from com.sun.star.awt import Size

    if aspect is None:
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


def _format_plot_tag(col: int, row: int, sha: str | None = None) -> str:
    """Format the owner cell and optional SHA-256 image digest as wa-plot:<col>,<row>[:<sha>]."""
    return f"wa-plot:{col},{row}:{sha}" if sha else f"wa-plot:{col},{row}"


def _parse_plot_tag(desc: Any) -> tuple[tuple[int, int], str | None] | None:
    """Parse a wa-plot tag into ((col, row), sha_hex).

    Returns None if desc is not a valid wa-plot tag in the format
    wa-plot:<col>,<row>[:<sha>].
    """
    if not isinstance(desc, str) or not desc.startswith("wa-plot:"):
        return None
    rest = desc[len("wa-plot:"):]
    if ":" in rest:
        coords_part, sha_part = rest.split(":", 1)
        sha = sha_part if sha_part else None
    else:
        coords_part = rest
        sha = None

    if "," not in coords_part:
        return None
    try:
        c_str, r_str = coords_part.split(",", 1)
        coords = (int(c_str), int(r_str))
    except ValueError:
        return None

    return coords, sha


def _read_plot_tag(shape: Any) -> tuple[tuple[int, int], str | None] | None:
    """Return the shape's parsed wa-plot tag, or None when it has none."""
    try:
        attrs = shape.getPropertyValue("UserDefinedAttributes")
        if attrs is None or not attrs.hasByName(_PLOT_TAG_ATTR):
            return None
        return _parse_plot_tag(attrs.getByName(_PLOT_TAG_ATTR).Value)
    except (AttributeError, TypeError):
        return None


def _record_plot_tag(shape: Any, target_cell: Any, sha: str | None) -> None:
    """Store the owner formula cell and image digest in the shape's UserDefinedAttributes.

    Edits to the attribute container take effect when it is set back on the shape. An
    unchanged tag skips that write, so a recalc with the same image does not mark the
    document modified.
    """
    from com.sun.star.xml import AttributeData

    addr_key = _cell_address_key(target_cell)
    if addr_key is None:
        return
    tag = _format_plot_tag(addr_key[1], addr_key[2], sha)
    try:
        attrs = shape.getPropertyValue("UserDefinedAttributes")
        data = AttributeData()
        data.Namespace = _PLOT_TAG_NAMESPACE
        data.Type = "CDATA"
        data.Value = tag
        if not attrs.hasByName(_PLOT_TAG_ATTR):
            attrs.insertByName(_PLOT_TAG_ATTR, data)
        elif attrs.getByName(_PLOT_TAG_ATTR).Value != tag:
            attrs.replaceByName(_PLOT_TAG_ATTR, data)
        else:
            return
        shape.setPropertyValue("UserDefinedAttributes", attrs)
    except (AttributeError, TypeError):
        log.debug("could not record plot tag", exc_info=True)


def _graphic_aspect(shape: Any) -> tuple[int, int] | None:
    """Logical width and height of the shape's current graphic, or None when unknown."""
    try:
        size = shape.getPropertyValue("Graphic").getPropertyValue("Size100thMM")
        return int(size.Width), int(size.Height)
    except (AttributeError, TypeError):
        return None


def _sheet_is_rtl(sheet: Any) -> bool:
    """True when sheet writing direction is right-to-left (TableLayout == 1)."""
    try:
        return int(sheet.TableLayout) == 1
    except (AttributeError, TypeError, ValueError):
        return False


def _formula_axis_hidden(sheet: Any, cell: Any) -> bool:
    """True when the formula cell's row or column is hidden.

    Checks IsVisible on the formula cell's row and column. Missing API means visible.
    """
    try:
        addr = cell.getCellAddress()
        row = sheet.getRows().getByIndex(int(addr.Row))
        col = sheet.getColumns().getByIndex(int(addr.Column))
        return not bool(row.IsVisible) or not bool(col.IsVisible)
    except (AttributeError, TypeError, IndexError):
        return False


def _slot_edge(pos: Any, size: Any, rtl: bool) -> int:
    """Calculate the far outer edge coordinate for slot chaining."""
    if rtl:
        return int(pos.X)
    return int(pos.X) + int(size.Width)


def _resolve_slot(slots: list[tuple[Any, int]], index: int) -> Any | None:
    """Resolve an existing plot shape by slot index from the scanned slots."""
    return next((s for s, slot_idx in slots if slot_idx == index), None)


def _slot_cursor(
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

    Sign convention matches the slot loop in Collabora's ScDocument::ProcessPendingPythonPlots
    (pythonplots.cxx:547-548, 583, 596). In RTL sheets (TableLayout == 1), Calc core mirrors
    visual cell bounds (sc/source/core/data/documen3.cxx:2092 ScDocument::GetMMRect via
    MirrorRectRTL when IsNegativePage). UNO X values are then negative (mirrored): Position.X
    is the left visual edge and Position.X + Width is the right visual edge. Placement starts
    at the right visual edge and chains leftwards.
    """
    cursor_x = int(cell_pos.X) + int(cell_size.Width) if rtl else int(cell_pos.X)
    cursor_y = int(cell_pos.Y)

    for idx in range(image_index):
        prev_shape = _resolve_slot(slots, idx)
        if prev_shape is None:
            break
        try:
            pos = prev_shape.getPosition()
            size = prev_shape.getSize()
            cursor_x = _slot_edge(pos, size, rtl)
            cursor_y = int(pos.Y)
        except (AttributeError, TypeError):
            break

    return cursor_x, cursor_y


def _shape_matches_target_cell(shape: Any, target_cell: Any) -> bool:
    """True when the plot shape belongs to the formula cell target_cell.

    Positioning a cell-anchored shape makes stock Calc move its anchor to the cell under
    the shape's top-left corner (ScDrawView::adjustAnchoredPosition, drawvie3.cxx:175-224,
    calling ScDrawLayer::SetCellAnchoredFromPosition). Slots after the first lie beyond the
    formula cell, so they end up anchored to other cells, and LibrePy cannot pin the anchor
    back. The wa-plot tag therefore decides ownership whatever the anchor says; only a shape
    without a tag is matched by its anchor. Inserting rows or columns above or left of the
    formula moves the cell but not the tag, so such a shape is no longer matched and the
    next recalc creates a new sibling beside it.
    """
    tag = _read_plot_tag(shape)
    if tag is None:
        return _shape_anchor_matches_cell(shape, target_cell)
    addr_key = _cell_address_key(target_cell)
    return addr_key is not None and tag[0] == (addr_key[1], addr_key[2])


def _anchored_plot_slots(draw_page: Any, target_cell: Any) -> list[tuple[Any, int]]:
    """Return (shape, slot_index) for all plot shapes belonging to target_cell, in draw-page order."""
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
            if not _shape_matches_target_cell(shape, target_cell):
                continue
            slots.append((shape, slot_idx))
        return slots
    except Exception:
        log.debug("_anchored_plot_slots scan failed", exc_info=True)
        return []


def _drop_extra_plot_slots(draw_page: Any, slots: list[tuple[Any, int]], image_count: int) -> None:
    """Remove target_cell's plot shapes with slot index >= image_count, and duplicates.

    A second shape for a slot that is already taken (for example a copy-pasted plot) is
    never refreshed, because _resolve_slot uses the first one, so it is removed too. This
    matches removePlotSlotsFrom and PlotSlots::maExtra in Collabora's pythonplots.cxx:137-169.
    """
    taken: set[int] = set()
    for s, slot_idx in slots:
        if slot_idx >= image_count or slot_idx in taken:
            best_effort(partial(draw_page.remove, s), "_drop_extra_plot_slots: remove shape")
        else:
            taken.add(slot_idx)


def _read_image_bytes(payload: dict[str, Any] | None, tmp_path: str | None) -> bytes | None:
    # Read the full file rather than an initial chunk because the SHA-256 byte tag covers all bytes.
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


def _position_new_plot(
    sheet: Any,
    shape: Any,
    target_cell: Any,
    aspect_size: Any,
    image_index: int,
    slots: list[tuple[Any, int]],
    rtl: bool,
) -> None:
    """Anchor, size, position, and set initial visibility on a new plot shape."""
    from com.sun.star.awt import Point
    from plugin.calc.calc_utils import get_cell_geometry

    cell_pos, cell_size = get_cell_geometry(sheet, target_cell)
    try:
        is_merged = bool(target_cell.IsMerged)
    except (AttributeError, TypeError):
        is_merged = False
    width = int(cell_size.Width)
    height = int(cell_size.Height)
    is_large_placeholder = (
        is_merged
        and width >= MIN_CHART_PLACEHOLDER_WIDTH
        and height >= MIN_CHART_PLACEHOLDER_HEIGHT
    )

    cursor_x, cursor_y = _slot_cursor(
        cell_pos, cell_size, image_index, slots, rtl
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
        target_width = int(aspect_size.Width)

    # Each successive plot slot chains horizontally from the outer edge of the
    # preceding slot. Reading the live rectangle of each resolved slot ensures
    # slots fit flush against each other without gaps or overlaps.
    place_x = cursor_x - target_width if rtl else cursor_x
    shape.setPosition(Point(place_x, cursor_y))

    # Same rule as Collabora's setPlotSlotCellAnchor (pythonplots.cxx:228-234): a new
    # plot is hidden when the formula cell's row or column is hidden.
    shape.setPropertyValue("Visible", not _formula_axis_hidden(sheet, target_cell))


def _place_new_plot(
    doc: Any,
    sheet: Any,
    draw_page: Any,
    target_cell: Any,
    file_url: str,
    data_sha: str | None,
    aspect_size: Any,
    image_index: int,
    slots: list[tuple[Any, int]],
    rtl: bool,
) -> Any:
    """Create, load, anchor, and place a new plot shape on the sheet.

    When the image cannot be loaded, the slot stays on the sheet as an empty-graphic
    placeholder of the default 10 cm x 6 cm size, the failure is logged, and nothing is
    raised, so the remaining slots and the cell value are unaffected. This is the rule in
    the slot loop of Collabora's ScDocument::ProcessPendingPythonPlots
    (pythonplots.cxx:563-571, 579-592; logicSizeFromGraphic gives 10000 x 6000 for an
    empty Graphic). Without a formula cell there is no slot to keep, so the shape is
    removed and ImageEgressError is raised.
    """
    shape = doc.createInstance(_DRAW_GRAPHIC_SERVICE)
    shape.setSize(aspect_size)
    draw_page.add(shape)
    shape.setPropertyValue("Name", _plot_slot_name(image_index))

    graphic_exc: Exception | None = None
    try:
        # Setter loads the file into the shape's Graphic before returning.
        # _unlink_temp_image runs in finally, after this call.
        shape.setPropertyValue("GraphicURL", file_url)
    except Exception as exc:
        graphic_exc = exc

    if target_cell is None:
        if graphic_exc is not None:
            best_effort(
                lambda: draw_page.remove(shape),
                "insert_image_result_on_sheet: failed to remove failed shape without target cell",
            )
            raise ImageEgressError("could not load image into graphic shape") from graphic_exc
        return shape

    try:
        _position_new_plot(
            sheet,
            shape,
            target_cell,
            aspect_size if graphic_exc is None else _aspect_size(None),
            image_index,
            slots,
            rtl,
        )
        # A placeholder gets no digest, so the next recalc loads the image again.
        _record_plot_tag(shape, target_cell, data_sha if graphic_exc is None else None)
    except Exception as exc:
        best_effort(
            lambda: draw_page.remove(shape),
            "insert_image_result_on_sheet: failed to remove failed shape",
        )
        raise ImageEgressError("could not anchor plot to the formula cell") from (
            graphic_exc if graphic_exc is not None else exc
        )

    if graphic_exc is not None:
        log.warning("insert_image_result_on_sheet: could not load image into plot slot %d: %s", image_index, graphic_exc)
    return shape


def _refresh_existing_plot(
    shape: Any,
    target_cell: Any,
    file_url: str,
    data_sha: str | None,
    aspect: tuple[int, int] | None,
    rtl: bool,
) -> None:
    """Update the graphic on an existing plot shape, keeping the user's rectangle where possible."""
    from com.sun.star.awt import Point

    # A GraphicObjectShape does not expose the bytes it was loaded from, so the wa-plot
    # tag stores their SHA-256 digest. When the new image has the same digest, the reload
    # is skipped: the document is not marked modified, and the user's size, position,
    # and crop stay as they are. The tag's owner already matches, because a tagged shape
    # is only found through its tag.
    tag = _read_plot_tag(shape)
    if data_sha is not None and tag is not None and tag[1] == data_sha:
        return

    old_aspect = _graphic_aspect(shape)
    try:
        shape.setPropertyValue("GraphicURL", file_url)
    except Exception as exc:
        # The UNO Graphic setter ignores an empty graphic, so a failed load keeps the
        # previous picture and its old digest; the next recalc tries the load again. The
        # failure is only logged so the remaining slots still update. Collabora's slot
        # loop (pythonplots.cxx:563-577) instead shows the empty-graphic placeholder.
        log.warning("insert_image_result_on_sheet: could not update plot graphic: %s", exc)
        return

    _record_plot_tag(shape, target_cell, data_sha)

    # Collabora never resizes an existing slot (pythonplots.cxx:573-577). Here the box
    # changes only when the new graphic's aspect differs from the old graphic's, so a box
    # the user stretched keeps its shape while the plot's proportions stay the same.
    if aspect is None or old_aspect is None or not _aspect_ratio_changed(*old_aspect, *aspect):
        return
    try:
        if bool(shape.getPropertyValue("ResizeWithCell")):
            return
        old_pos = shape.getPosition()
        old_size = shape.getSize()
        old_w = int(old_size.Width)
        old_h = int(old_size.Height)
        # Keep the long side and the corner the slot chains from: top-left in LTR,
        # top-right in RTL. Slots are flush only when created; resizing an earlier slot
        # here can leave a gap or an overlap next to a later slot, which stays put.
        new_size = _aspect_size(aspect, long_side=max(old_w, old_h))
        shape.setSize(new_size)
        if rtl:
            shape.setPosition(Point(int(old_pos.X) + old_w - int(new_size.Width), int(old_pos.Y)))
    except (AttributeError, TypeError):
        log.debug("could not update existing plot geometry", exc_info=True)


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

    # Resolve the target sheet and cell from the formula when the code is
    # known. During a workbook recalc (Ctrl+Shift+F9, or file open) the
    # active sheet can be sheet 0 while the formula is on another sheet, and
    # a one-row merged selection there crushes the chart to about 12.7mm.
    # Fall back to the active sheet and selection, and keep a minimum size
    # when the anchor is a merged range.
    #
    # Raise ImageEgressError when the document, sheet, or draw page is missing,
    # or when UNO throws: a silent return would let =PY(), the venv and
    # plot_data report the picture as inserted.
    created_path: str | None = None
    try:
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
        data_sha = hashlib.sha256(data_bytes).hexdigest() if data_bytes is not None else None
        aspect = _intrinsic_aspect(data_bytes) if data_bytes else None
        aspect_size = _aspect_size(aspect)

        slots = _anchored_plot_slots(draw_page, target_cell) if target_cell is not None else []
        shape = _resolve_slot(slots, image_index)
        rtl = _sheet_is_rtl(sheet)

        try:
            if shape is None:
                _place_new_plot(
                    doc, sheet, draw_page, target_cell, file_url, data_sha, aspect_size, image_index, slots, rtl
                )
            else:
                _refresh_existing_plot(shape, target_cell, file_url, data_sha, aspect, rtl)
        finally:
            # Only a caller that passes image_count owns the slot family. =PY() and
            # run_venv_python_script pass it and enumerate image_index from 0, so the
            # sweep runs once on index 0. Stale slots >= image_count and duplicate shapes
            # for one slot are swept even when slot 0 raises, so they do not linger.
            # Omitting image_count (chat plot_data / viz) must not remove sibling WriterAgentPlot_N shapes.
            if image_count is not None and image_index == 0 and target_cell is not None:
                _drop_extra_plot_slots(draw_page, slots, image_count)
    except ImageEgressError:
        raise
    except Exception as exc:
        log.exception("insert_image_result_on_sheet failed to insert graphic shape")
        raise ImageEgressError("insert_image_result_on_sheet failed to insert graphic shape") from exc
    finally:
        if created_path is not None:
            _unlink_temp_image(created_path)

