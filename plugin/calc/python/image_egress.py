# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Insert matplotlib image payloads on Calc sheets (=PYTHON / chat tool)."""

from __future__ import annotations

import logging
import os
from typing import Any, NoReturn

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
    try:
        if hasattr(cell, "getRangeAddress"):
            addr = cell.getRangeAddress()
            sheet_idx = getattr(addr, "Sheet", None)
            return (sheet_idx, int(addr.StartColumn), int(addr.StartRow))
        if hasattr(cell, "getCellAddress"):
            addr = cell.getCellAddress()
            return (getattr(addr, "Sheet", None), int(addr.Column), int(addr.Row))
    except Exception:
        return None
    return None


def _shape_anchor_matches_cell(shape: Any, target_cell: Any) -> bool:
    """True when the shape is already anchored to the same grid cell (not UNO identity)."""
    try:
        if not hasattr(shape, "getPropertyValue"):
            return False
        anchor = shape.getPropertyValue("Anchor")
        left = _cell_address_key(anchor)
        right = _cell_address_key(target_cell)
        return left is not None and left == right
    except Exception:
        return False


def _supports_draw_graphic(shape: Any) -> bool:
    """True only for a real GraphicObjectShape.

    UNO ``supportsService`` returns True or 1. A test double's MagicMock is
    truthy too, so only those two results count.
    """
    supports = getattr(shape, "supportsService", None)
    if not callable(supports):
        return False
    try:
        hit = supports(_DRAW_GRAPHIC_SERVICE)
    except Exception:
        return False
    if hit is True:
        return True
    return type(hit) is int and hit == 1


def _shape_name(shape: Any) -> str:
    try:
        if hasattr(shape, "getPropertyValue"):
            value = shape.getPropertyValue("Name")
        else:
            value = getattr(shape, "Name", "")
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _is_plot_shape_name(name: str) -> bool:
    if name == _PLOT_SHAPE_NAME_PREFIX:
        return True
    return name.startswith(_PLOT_SHAPE_NAME_PREFIX + "_")


def _is_reusable_plot_shape(shape: Any, target_cell: Any) -> bool:
    """Graphic we previously inserted at *target_cell*, not a user image or other shape.

    Require GraphicObjectShape plus the WriterAgentPlot name prefix. An
    anchor match alone replaces GraphicURL on a rectangle or a photo the
    user placed on the formula cell. Plots from before the prefix stay in
    place (one extra shape) rather than guessing which graphic is ours.
    """
    if not _supports_draw_graphic(shape):
        return False
    if not _is_plot_shape_name(_shape_name(shape)):
        return False
    return _shape_anchor_matches_cell(shape, target_cell)


def _assign_plot_shape_name(shape: Any, draw_page: Any) -> None:
    """Give a new plot shape a unique WriterAgentPlot name so the next insert can find it."""
    used: set[str] = set()
    try:
        raw_count = getattr(draw_page, "getCount", lambda: 0)()
        if isinstance(raw_count, int) and raw_count > 0:
            for index in range(raw_count):
                used.add(_shape_name(draw_page.getByIndex(index)))
    except Exception:
        log.debug("insert_image_result_on_sheet: could not list shape names", exc_info=True)
    name = _PLOT_SHAPE_NAME_PREFIX
    suffix = 2
    while name in used and suffix < 10000:
        name = f"{_PLOT_SHAPE_NAME_PREFIX}_{suffix}"
        suffix += 1
    try:
        shape.setPropertyValue("Name", name)
    except Exception:
        log.debug("insert_image_result_on_sheet: could not name plot shape", exc_info=True)


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


def insert_image_result_on_sheet(ctx: Any, payload: dict[str, Any], *, code: str | None = None, doc: Any | None = None) -> None:
    """Write image payload bytes to a temp file and insert as a cell-anchored shape on the target sheet.

    Posts execution asynchronously to the main VCL UI thread if invoked from a background worker thread.
    *doc* is required (=PY() passes its caller argument; tools pass their target
    document). With no doc the insert fails rather than using the front window.

    Raises:
        ImageEgressError: the synchronous (main-thread) insert did not place a graphic.
            The off-main path only posts; the posted call raises on the main thread.
    """
    from plugin.framework.queue_executor import post_to_main_thread
    from plugin.framework.thread_guard import on_main_thread

    # Thread safety invariant: Drawing layer manipulation (DrawPage, GraphicObjectShape, cell geometry)
    # must run on LibreOffice's main VCL thread to prevent internal C++ state corruption and deadlocks.
    # If called from a background recalculation or script worker thread, post asynchronously to the main thread.
    if not on_main_thread():
        post_to_main_thread(_insert_image_result_on_sheet_impl, ctx, payload, code, doc)
        return

    _insert_image_result_on_sheet_impl(ctx, payload, code, doc)


def _insert_image_result_on_sheet_impl(ctx: Any, payload: dict[str, Any], code: str | None = None, doc: Any | None = None) -> None:
    """Main-thread implementation of graphic shape creation and anchoring."""
    import uno
    from com.sun.star.awt import Size

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

        tmp_path = write_image_payload_to_temp(payload)
        file_url = uno.systemPathToFileUrl(os.path.abspath(tmp_path))

        shape = None
        if target_cell is not None:
            try:
                raw_count = getattr(draw_page, "getCount", lambda: 0)()
                if isinstance(raw_count, int) and raw_count > 0:
                    for index in range(raw_count):
                        candidate = draw_page.getByIndex(index)
                        if _is_reusable_plot_shape(candidate, target_cell):
                            shape = candidate
                            break
            except Exception:
                shape = None

        default_size = Size(DEFAULT_CHART_SIZE_WIDTH, DEFAULT_CHART_SIZE_HEIGHT)
        if shape is None:
            shape = doc.createInstance(_DRAW_GRAPHIC_SERVICE)
            shape.setSize(default_size)
            draw_page.add(shape)
            _assign_plot_shape_name(shape, draw_page)

        # Setter loads the file into the shape's Graphic before returning.
        # _unlink_temp_image runs in finally, after this call.
        shape.setPropertyValue("GraphicURL", file_url)

        if target_cell is not None:
            try:
                cell_pos, cell_size = get_cell_geometry(sheet, target_cell)
                is_merged = bool(getattr(target_cell, "IsMerged", False))
                width = getattr(cell_size, "Width", 0)
                height = getattr(cell_size, "Height", 0)
                is_large_placeholder = is_merged and width >= MIN_CHART_PLACEHOLDER_WIDTH and height >= MIN_CHART_PLACEHOLDER_HEIGHT

                shape.setPropertyValue("Anchor", target_cell)
                if hasattr(shape, "setPosition"):
                    shape.setPosition(cell_pos)

                if is_large_placeholder:
                    shape.setPropertyValue("ResizeWithCell", True)
                    if hasattr(shape, "setSize"):
                        shape.setSize(cell_size)
                else:
                    shape.setPropertyValue("ResizeWithCell", False)
                    if hasattr(shape, "setSize"):
                        shape.setSize(default_size)
            except Exception:
                log.debug("insert_image_result_on_sheet: could not anchor to cell", exc_info=True)
    except ImageEgressError:
        raise
    except Exception as exc:
        log.exception("insert_image_result_on_sheet failed to insert graphic shape")
        raise ImageEgressError("insert_image_result_on_sheet failed to insert graphic shape") from exc
    finally:
        _unlink_temp_image(tmp_path)
