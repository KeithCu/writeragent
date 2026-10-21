# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Insert matplotlib image payloads on Calc sheets (=PYTHON / chat tool).

Supports multiple plot shapes per cell via indexed shape slots with reuse.
"""

from __future__ import annotations

from functools import partial
import logging
import os
import re
import struct
from typing import Any, NoReturn

from plugin.framework.errors import best_effort
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
    """Slot index for a plot shape: 0 for WriterAgentPlot, N-1 for WriterAgentPlot_N (N>=2).

    The suffix is a decimal integer N >= 2 with no sign, whitespace, underscore,
    or leading zero. _plot_slot_name is the only writer of names that are
    stored on the shape.
    """
    if name == _PLOT_SHAPE_NAME_PREFIX:
        return 0
    prefix = f"{_PLOT_SHAPE_NAME_PREFIX}_"
    if name.startswith(prefix):
        suffix = name[len(prefix):]
        if not re.fullmatch(r"[1-9][0-9]*", suffix):
            return None
        # Bugfix: What was wrong: a shape named WriterAgentPlot_1_0, WriterAgentPlot_+2,
        # or WriterAgentPlot_02 was treated as a plot slot and could be removed or reused.
        # How it happened: int(suffix) accepts underscores, a leading plus, surrounding
        # whitespace, and leading zeros (int("1_0") == 10, int("+2") == 2, int("02") == 2).
        # Why this change fixes it: the suffix must be a full match of [1-9][0-9]* and
        # the value must be >= 2. Names _plot_slot_name writes are unchanged, and those
        # strings are what the document stores.
        n = int(suffix)
        if n >= 2:
            return n - 1
    return None


_SVG_UNIT_TO_MM = {
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
    scale = _SVG_UNIT_TO_MM.get(unit)
    return num * scale if scale is not None else num


def _intrinsic_aspect(data: bytes) -> tuple[int, int] | None:
    """Parse intrinsic width and height from PNG or SVG image bytes."""
    if not data or not isinstance(data, (bytes, bytearray)):
        return None
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


def _aspect_size(aspect: tuple[int, int] | None) -> Any:
    """Calculate Size maintaining aspect ratio, longer side 10000 (10 cm)."""
    from com.sun.star.awt import Size

    if aspect is None:
        return Size(DEFAULT_CHART_SIZE_WIDTH, DEFAULT_CHART_SIZE_HEIGHT)
    w, h = aspect
    if w <= 0 or h <= 0:
        return Size(DEFAULT_CHART_SIZE_WIDTH, DEFAULT_CHART_SIZE_HEIGHT)
    major = max(w, h)
    minor = min(w, h)
    short_side = max(1000, round(10000 * minor / major))
    if w >= h:
        return Size(10000, short_side)
    else:
        return Size(short_side, 10000)


def _drop_extra_plot_slots(draw_page: Any, target_cell: Any, image_count: int) -> None:
    """Remove existing plot shapes on target_cell with slot index >= image_count."""
    if draw_page is None or target_cell is None or not hasattr(draw_page, "remove"):
        return
    try:
        raw_count = getattr(draw_page, "getCount", lambda: 0)()
        if not isinstance(raw_count, int) or raw_count <= 0:
            return
        to_remove = []
        for index in range(raw_count):
            shape = draw_page.getByIndex(index)
            if not _supports_service(shape, _DRAW_GRAPHIC_SERVICE):
                continue
            if not _shape_anchor_matches_cell(shape, target_cell):
                continue
            slot_idx = _plot_slot_index(_shape_name(shape))
            if slot_idx is not None and slot_idx >= image_count:
                to_remove.append(shape)
        for s in to_remove:
            best_effort(partial(draw_page.remove, s), "_drop_extra_plot_slots: remove shape")
    except Exception:
        log.debug("_drop_extra_plot_slots scan failed", exc_info=True)


def _read_image_bytes(payload: dict[str, Any] | None, tmp_path: str | None) -> bytes | None:
    if tmp_path and os.path.isfile(tmp_path):
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


def _located_image_cell(ctx: Any, doc: Any, code: str | None) -> tuple[Any, Any, tuple[int, int]] | None:
    if not code or doc is None:
        return None
    try:
        from plugin.calc.python.formula_locator_cache import locate_formula_cell_in_doc

        return locate_formula_cell_in_doc(ctx, doc, code)
    except Exception:
        return None


def _set_image_cell_annotation(ctx: Any, doc: Any, code: str | None, error_message: str) -> None:
    """Set error annotation on the located formula cell after off-main egress failure."""
    located = _located_image_cell(ctx, doc, code)
    if located is None:
        return
    sheet, cell, (row, col) = located
    try:
        text = f"{_IMAGE_ANNOTATION_PREFIX}{error_message}"
        ann = cell.getAnnotation() if hasattr(cell, "getAnnotation") else None
        note_str = ""
        if ann is not None and hasattr(ann, "getString"):
            val = ann.getString()
            if val is None:
                note_str = ""
            elif isinstance(val, str):
                note_str = val
            else:
                log.debug("_set_image_cell_annotation: note text is not a string")
                return

        if note_str.startswith(_IMAGE_ANNOTATION_PREFIX) and ann is not None and hasattr(ann, "setString"):
            ann.setString(text)
            return

        if note_str and not note_str.startswith(_IMAGE_ANNOTATION_PREFIX):
            # Bugfix: What was wrong: a failed off-main plot overwrote whatever comment was
            # already on the =PY() cell, and a later success deleted that note.
            # How it happened: the branch was "getAnnotation() returned an object and
            # getString() was non-empty", so any user comment took setString. The other
            # branch calls insertNew. ScCellObj::getAnnotation (cellsuno.cxx) always returns
            # a ScAnnotationObj, and ScAnnotationsObj::insertNew (docuno.cxx) calls
            # ScDocFunc::ReplaceNote, which releases the cell's only note and creates a new
            # one. There is no second comment to write into.
            # Why this change fixes it: setString runs only when the note already starts
            # with the WriterAgent image prefix. Any other non-empty text is left as-is.
            # insertNew runs only for an empty note, which is what creates the error comment.
            log.debug("_set_image_cell_annotation: user annotation left in place")
            return

        if hasattr(sheet, "getAnnotations"):
            annotations = sheet.getAnnotations()
            if hasattr(annotations, "insertNew"):
                addr: Any = None
                if hasattr(cell, "getCellAddress"):
                    addr = cell.getCellAddress()
                if addr is None:
                    sheet_idx = getattr(sheet.getRangeAddress(), "Sheet", 0) if hasattr(sheet, "getRangeAddress") else 0
                    from com.sun.star.table import CellAddress

                    addr = CellAddress(Sheet=sheet_idx, Column=col, Row=row)
                annotations.insertNew(addr, text)
    except Exception:
        log.debug("_set_image_cell_annotation failed", exc_info=True)


def _clear_image_cell_annotation(ctx: Any, doc: Any, code: str | None) -> None:
    """Remove WriterAgent image: annotation on the located formula cell on success."""
    located = _located_image_cell(ctx, doc, code)
    if located is None:
        return
    sheet, cell, (row, col) = located
    try:
        ann = cell.getAnnotation() if hasattr(cell, "getAnnotation") else None
        if ann is not None and hasattr(ann, "getString"):
            val = ann.getString() or ""
            if val.startswith(_IMAGE_ANNOTATION_PREFIX):
                if hasattr(sheet, "getAnnotations"):
                    annotations = sheet.getAnnotations()
                    if hasattr(annotations, "getCount") and hasattr(annotations, "getByIndex") and hasattr(annotations, "removeByIndex"):
                        for i in range(annotations.getCount()):
                            item = annotations.getByIndex(i)
                            pos = item.getPosition() if hasattr(item, "getPosition") else None
                            if pos is not None and getattr(pos, "Column", None) == col and getattr(pos, "Row", None) == row:
                                annotations.removeByIndex(i)
                                break
                        else:
                            if hasattr(ann, "setString"):
                                ann.setString("")
    except Exception:
        log.debug("_clear_image_cell_annotation failed", exc_info=True)


class ImageEgressError(Exception):
    """Raised when a Calc plot was not placed on the sheet.

    Callers used to treat a silent return as success ("plot inserted" /
    "Image inserted") when the document, sheet, or draw page was missing.
    """


def _egress_fail(message: str, *, level: str = "debug") -> NoReturn:
    """Log *message* and raise so callers cannot report a successful insert."""
    if level == "warning":
        log.warning("insert_image_result_on_sheet: %s", message)
    else:
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

    A page-anchored shape's Anchor is the sheet, whose range address starts at
    0,0, so it used to "match" A1. It matches no cell.
    """
    if not hasattr(shape, "getPropertyValue"):
        return False
    anchor = shape.getPropertyValue("Anchor")
    if _supports_service(anchor, "com.sun.star.sheet.Spreadsheet"):
        return False
    left = _cell_address_key(anchor)
    right = _cell_address_key(target_cell)
    return left is not None and left == right


def _shape_name(shape: Any) -> str:
    if hasattr(shape, "getPropertyValue"):
        value = shape.getPropertyValue("Name")
    else:
        value = getattr(shape, "Name", "")
    return value if isinstance(value, str) else ""


def _is_reusable_plot_shape(shape: Any, target_cell: Any, slot_name: str) -> bool:
    """Graphic we previously inserted at *target_cell* in *slot_name*, not a user image or other shape.

    Require GraphicObjectShape plus matching the specific slot name. An
    anchor match alone replaces GraphicURL on a rectangle or a photo the
    user placed on the formula cell. Other WriterAgentPlot* shapes stay put.
    """
    if not _supports_service(shape, _DRAW_GRAPHIC_SERVICE):
        return False
    if _shape_name(shape) != slot_name:
        return False
    return _shape_anchor_matches_cell(shape, target_cell)


def _unlink_temp_image(path: str | None) -> None:
    """Best-effort delete of the PNG/SVG written for GraphicURL.

    The GraphicURL setter reads the file before it returns and stores the
    bitmap on the shape (desktop GraphicObjectShape embeds on set). The path
    is not a live link after that call, so unlink in ``finally`` — including
    when a later anchor step fails — instead of leaking a file per recalc.
    A build that kept only the URL would show a broken picture; that is not
    what the desktop setter does.
    """
    if not path:
        return
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
            Off-main, this function posts and returns. The posted call raises on the main
            thread when notify_cell is false. When notify_cell is true, a posted
            ImageEgressError is written to the formula cell's note only if that note is
            empty or already starts with "WriterAgent image: "; any other note is left
            unchanged and the exception is not raised. post_to_main_thread can still raise
            TimeoutError or RuntimeError to this caller.
    """
    from plugin.framework.queue_executor import post_to_main_thread
    from plugin.framework.thread_guard import on_main_thread

    # Thread safety invariant: Drawing layer manipulation (DrawPage, GraphicObjectShape, cell geometry)
    # must run on LibreOffice's main VCL thread to prevent internal C++ state corruption and deadlocks.
    # If called from a background recalculation or script worker thread, post asynchronously to the main thread.
    # The main-thread path lets ImageEgressError propagate and the off-main notify_cell path annotates
    # instead, because the formula has already been answered by the time the posted callable runs.
    if not on_main_thread():
        def _off_main_wrapper() -> None:
            try:
                _insert_image_result_on_sheet_impl(
                    ctx,
                    payload,
                    code=code,
                    doc=doc,
                    image_index=image_index,
                    image_path=image_path,
                    image_count=image_count,
                )
                if notify_cell:
                    _clear_image_cell_annotation(ctx, doc, code)
            except ImageEgressError as err:
                if notify_cell:
                    _set_image_cell_annotation(ctx, doc, code, str(err))
                else:
                    raise

        post_to_main_thread(_off_main_wrapper)
        return

    _insert_image_result_on_sheet_impl(
        ctx,
        payload,
        code=code,
        doc=doc,
        image_index=image_index,
        image_path=image_path,
        image_count=image_count,
    )
    if notify_cell:
        _clear_image_cell_annotation(ctx, doc, code)


def _insert_image_result_on_sheet_impl(
    ctx: Any,
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
    tmp_path: str | None = None
    we_created_temp = False
    try:
        from plugin.framework.thread_guard import on_main_thread

        if not on_main_thread():
            # A literal raise is the exit the thread-safety linter
            # recognizes. _egress_fail raises at runtime but is only a Call
            # in the AST, so the linter still flags the document access
            # below as unguarded. This raise still refuses to touch the
            # document off the main thread.
            log.debug(
                "insert_image_result_on_sheet: image insertion must run on the main thread"
            )
            raise ImageEgressError("image insertion must run on the main thread")

        from plugin.calc.calc_utils import get_cell_geometry

        # Callers pass the document (=PY() passes its caller argument). Do not
        # fall back to the front window.
        if doc is None:
            _egress_fail("no Calc document for image insertion")

        sheet = None
        target_cell = None

        if code:
            try:
                from plugin.calc.python.formula_locator_cache import locate_formula_cell_in_doc

                located = locate_formula_cell_in_doc(ctx, doc, code)
                if located is not None:
                    sheet, target_cell, _unused_anchor = located
            except Exception:
                log.debug("insert_image_result_on_sheet: locate_formula_cell_in_doc failed", exc_info=True)

            if sheet is None or target_cell is None:
                # Bugfix (#385/#389): When formula code is provided (=PYTHON / =PY), failing to locate
                # the formula cell must NOT fall back to the controller's active sheet or active selection.
                # Falling back causes plots to be inserted on whatever sheet/cell is active (e.g. analysis!A1
                # during Ctrl+Shift+F9 recalc).
                _egress_fail(
                    "could not locate formula cell for formula code; image was not inserted",
                    level="warning",
                )

        ctrl = doc.getCurrentController() if hasattr(doc, "getCurrentController") else None

        if sheet is None:
            if ctrl is not None and hasattr(ctrl, "getActiveSheet") and ctrl.getActiveSheet():
                sheet = ctrl.getActiveSheet()
            elif hasattr(doc, "getSheets") and doc.getSheets().getCount() > 0:
                sheet = doc.getSheets().getByIndex(0)
            else:
                _egress_fail("could not resolve a sheet for image insertion")

        draw_page = getattr(sheet, "DrawPage", None)
        if draw_page is None:
            _egress_fail("target sheet has no DrawPage; image was not inserted")

        if target_cell is None and ctrl is not None and hasattr(ctrl, "getSelection"):
            try:
                selection = ctrl.getSelection()
                if selection is not None and hasattr(selection, "getRangeAddress"):
                    addr = selection.getRangeAddress()
                    target_cell = sheet.getCellByPosition(addr.StartColumn, addr.StartRow)
            except Exception:
                log.debug("insert_image_result_on_sheet: selection fallback failed", exc_info=True)

        # Only a caller that passes image_count owns the slot family. =PY() and
        # run_venv_python_script pass it and enumerate image_index from 0, so the
        # sweep runs once on index 0. Omitting image_count (chat plot_data / viz)
        # must not remove sibling WriterAgentPlot_N shapes; they may belong to a
        # formula on the selected cell.
        if image_count is not None and image_index == 0 and target_cell is not None:
            _drop_extra_plot_slots(draw_page, target_cell, image_count)

        we_created_temp = False
        if image_path:
            tmp_path = image_path
        elif payload is not None and isinstance(payload, dict) and payload.get("image_path"):
            tmp_path = str(payload["image_path"])
        else:
            tmp_path = write_image_payload_to_temp(payload)
            we_created_temp = True
        file_url = uno.systemPathToFileUrl(os.path.abspath(tmp_path))

        data_bytes = _read_image_bytes(payload, tmp_path)
        aspect = _intrinsic_aspect(data_bytes) if data_bytes else None
        aspect_size = _aspect_size(aspect)

        slot_name = _plot_slot_name(image_index)
        shape = None
        if target_cell is not None:
            try:
                raw_count = getattr(draw_page, "getCount", lambda: 0)()
                if isinstance(raw_count, int) and raw_count > 0:
                    for index in range(raw_count):
                        candidate = draw_page.getByIndex(index)
                        if _is_reusable_plot_shape(candidate, target_cell, slot_name=slot_name):
                            shape = candidate
                            break
            except Exception:
                shape = None

        created_shape = False
        if shape is None:
            shape = doc.createInstance(_DRAW_GRAPHIC_SERVICE)
            if hasattr(shape, "setSize"):
                shape.setSize(aspect_size)
            draw_page.add(shape)
            created_shape = True
            shape.setPropertyValue("Name", slot_name)

        try:
            # Setter loads the file into the shape's Graphic before returning.
            # _unlink_temp_image runs in finally, after this call.
            shape.setPropertyValue("GraphicURL", file_url)
        except Exception:
            # The add already published the shape to the draw page; the GraphicURL
            # setter is what embeds the bitmap. If embedding fails on a shape we
            # just created, remove it so we do not leave an empty blank shape behind.
            if created_shape and hasattr(draw_page, "remove"):
                best_effort(lambda: draw_page.remove(shape), "insert_image_result_on_sheet: failed to remove failed shape")
            raise

        if target_cell is not None:
            try:
                cell_pos, cell_size = get_cell_geometry(sheet, target_cell)
                is_merged = bool(getattr(target_cell, "IsMerged", False))
                width = getattr(cell_size, "Width", 0)
                height = getattr(cell_size, "Height", 0)
                is_large_placeholder = is_merged and width >= MIN_CHART_PLACEHOLDER_WIDTH and height >= MIN_CHART_PLACEHOLDER_HEIGHT

                shape.setPropertyValue("Anchor", target_cell)
                if hasattr(shape, "setPosition"):
                    if image_index == 0:
                        shape.setPosition(cell_pos)
                    else:
                        pos_x = getattr(cell_pos, "X", 0) + (image_index * aspect_size.Width)
                        pos_y = getattr(cell_pos, "Y", 0)
                        shape.setPosition(Point(pos_x, pos_y))

                if is_large_placeholder:
                    shape.setPropertyValue("ResizeWithCell", True)
                    if hasattr(shape, "setSize"):
                        shape.setSize(cell_size)
                else:
                    shape.setPropertyValue("ResizeWithCell", False)
                    if hasattr(shape, "setSize"):
                        shape.setSize(aspect_size)
            except Exception:
                # Bugfix: What was wrong: Anchor failure on a newly created shape left a floating orphan shape on the draw page and reported success.
                # How it happened: The anchor block caught all exceptions and logged a warning without raising or rolling back the added shape.
                # Why this change fixes it: For shapes created in this call, we remove the shape from the draw page and raise ImageEgressError; reused shapes keep their existing position and only log a warning.
                if created_shape:
                    if hasattr(draw_page, "remove"):
                        best_effort(lambda: draw_page.remove(shape), "insert_image_result_on_sheet: failed to remove failed shape")
                    raise ImageEgressError("could not anchor plot to the formula cell")
                log.warning("insert_image_result_on_sheet: could not anchor to cell", exc_info=True)
    except ImageEgressError:
        raise
    except Exception as exc:
        log.exception("insert_image_result_on_sheet failed to insert graphic shape")
        raise ImageEgressError("insert_image_result_on_sheet failed to insert graphic shape") from exc
    finally:
        if we_created_temp:
            _unlink_temp_image(tmp_path)

