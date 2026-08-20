# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Insert matplotlib image payloads on Calc sheets (=PYTHON / chat tool)."""

from __future__ import annotations

import logging
import os
from typing import Any

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


def insert_image_result_on_sheet(ctx: Any, payload: dict[str, Any], *, code: str | None = None) -> None:
    """Write image payload bytes to a temp file and insert as a cell-anchored shape on the target sheet.

    Marshals execution to the main VCL UI thread if invoked from a background worker thread.
    """
    from plugin.framework.queue_executor import execute_on_main_thread
    from plugin.framework.thread_guard import on_main_thread

    # Thread safety invariant: Drawing layer manipulation (DrawPage, GraphicObjectShape, cell geometry)
    # must run on LibreOffice's main VCL thread to prevent internal C++ state corruption and deadlocks.
    # If called from a background recalculation or script worker thread, marshal via execute_on_main_thread.
    if not on_main_thread():
        execute_on_main_thread(_insert_image_result_on_sheet_impl, ctx, payload, code)
        return

    _insert_image_result_on_sheet_impl(ctx, payload, code)


def _insert_image_result_on_sheet_impl(ctx: Any, payload: dict[str, Any], code: str | None = None) -> None:
    """Main-thread implementation of graphic shape creation and anchoring."""
    import uno
    from com.sun.star.awt import Size

    # Bugfix (#385): Previously, insert_image_result_on_sheet always used the active sheet and active
    # selection from the controller. During workbook recalc (Ctrl+Shift+F9 or file open), the active sheet
    # may be Sheet 0 (e.g. Overview) while formula cells are on another sheet (e.g. Viz_Gallery).
    # Furthermore, if Sheet 0 had a 1-row merged hero banner selected, charts were crushed to ~12.7mm height.
    # Fix: Resolve the target sheet and cell by locating the formula cell via code if available;
    # otherwise fallback gracefully to active sheet/selection, and enforce minimum dimensions for merged sizing.
    try:
        from plugin.calc.calc_utils import get_cell_geometry
        from plugin.scripting.document_scripts import get_calc_document_from_ctx

        doc = get_calc_document_from_ctx(ctx)
        if doc is None:
            log.debug("insert_image_result_on_sheet: no active Calc document resolved from context")
            return

        sheet = None
        target_cell = None

        if code:
            try:
                from plugin.calc.python.formula_locator_cache import locate_formula_cell_in_doc

                located = locate_formula_cell_in_doc(ctx, doc, code)
                if located is not None:
                    sheet, target_cell, _ = located
            except Exception:
                log.debug("insert_image_result_on_sheet: locate_formula_cell_in_doc failed", exc_info=True)

            if sheet is None or target_cell is None:
                # Bugfix (#385/#389): When formula code is provided (=PYTHON / =PY), failing to locate
                # the formula cell must NOT fall back to the controller's active sheet or active selection.
                # Falling back causes plots to be inserted on whatever sheet/cell is active (e.g. analysis!A1
                # during Ctrl+Shift+F9 recalc).
                log.warning(
                    "insert_image_result_on_sheet: could not locate formula cell for formula code; aborting egress to prevent wrong-sheet placement"
                )
                return

        ctrl = doc.getCurrentController() if hasattr(doc, "getCurrentController") else None

        if sheet is None:
            if ctrl is not None and hasattr(ctrl, "getActiveSheet") and ctrl.getActiveSheet():
                sheet = ctrl.getActiveSheet()
            elif hasattr(doc, "getSheets") and doc.getSheets().getCount() > 0:
                sheet = doc.getSheets().getByIndex(0)
            else:
                log.debug("insert_image_result_on_sheet: could not resolve sheet")
                return

        draw_page = getattr(sheet, "DrawPage", None)
        if draw_page is None:
            log.debug("insert_image_result_on_sheet: target sheet has no DrawPage")
            return

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
                    for i in range(raw_count):
                        s = draw_page.getByIndex(i)
                        if _shape_anchor_matches_cell(s, target_cell):
                            shape = s
                            break
            except Exception:
                shape = None

        default_size = Size(DEFAULT_CHART_SIZE_WIDTH, DEFAULT_CHART_SIZE_HEIGHT)
        if shape is None:
            shape = doc.createInstance("com.sun.star.drawing.GraphicObjectShape")
            shape.setSize(default_size)
            draw_page.add(shape)

        shape.setPropertyValue("GraphicURL", file_url)

        if target_cell is not None:
            try:
                cell_pos, cell_size = get_cell_geometry(sheet, target_cell)
                is_merged = bool(getattr(target_cell, "IsMerged", False))
                w = getattr(cell_size, "Width", 0)
                h = getattr(cell_size, "Height", 0)
                is_large_placeholder = is_merged and w >= MIN_CHART_PLACEHOLDER_WIDTH and h >= MIN_CHART_PLACEHOLDER_HEIGHT

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
    except Exception:
        log.exception("insert_image_result_on_sheet failed to insert graphic shape")
