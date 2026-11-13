# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc spill array tracking, collision detection, and orphan cleanup."""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import json
import logging
import math
from typing import Any

from plugin.calc.calc_utils import EMPTY
from plugin.calc.datetime_wire import (
    coalesce_temporal_apply_rects,
    format_category_from_type,
    should_preserve_temporal_format,
)
from plugin.calc.python.addin_values import (
    ERR_NA,
    coerce_spill_value,
    spill_value_from_json,
)
from plugin.calc.python.formula_locator_cache import (
    FormulaOrigin,
    is_matching_py_formula,
    locate_formula_cell_in_doc,
    search_sheet_for_formula,
)
from plugin.calc.python.undo import undo_lock
from plugin.calc.python.workbook_state import WorkbookState
from plugin.framework.errors import uno_read
from plugin.framework.queue_executor import main

log = logging.getLogger(__name__)

# CellFlags bitmask: VALUE (1) | DATETIME (2) | STRING (4) | FORMULA (16) | HARDATTR (32) = 55.
# Clears values, formulas, and direct hard cell formatting/number formats without removing
# annotations, styles, or draw objects.
_SPILL_CLEAR_FLAGS = 1 | 2 | 4 | 16 | 32


@dataclass(frozen=True)
class SpillCell:
    row: int
    col: int
    kind: str  # "n" float, "s" str, "e" empty, "err" formula error code
    value: float | str | int | None


@dataclass
class SpillRecord:
    code: str
    cells: list[SpillCell]

    @property
    def spills(self) -> list[tuple[int, int]]:
        return [(cell.row, cell.col) for cell in self.cells]


def _cell_matches_stored_value(sheet: Any, row: int, col: int, cell_rec: SpillCell) -> bool:
    """Check if the cell at (col, row) matches the stored SpillCell value."""
    try:
        cell = sheet.getCellByPosition(col, row)
        if cell_rec.kind == "e":
            return cell.getType() == 0 or cell.getString() == ""
        if cell_rec.kind == "n":
            kind_type = cell.getType()
            if kind_type != 1 and getattr(kind_type, "value", None) != "VALUE":
                return False
            v = cell.getValue()
            if isinstance(cell_rec.value, (int, float)):
                return math.isclose(float(v), float(cell_rec.value), rel_tol=1e-9, abs_tol=1e-9)
            return False
        if cell_rec.kind == "s":
            kind_type = cell.getType()
            if kind_type != 2 and getattr(kind_type, "value", None) != "TEXT":
                return False
            return cell.getString() == (cell_rec.value or "")
        if cell_rec.kind == "err":
            return cell_rec.value is not None and int(cell.getError()) == int(cell_rec.value)
    except Exception:
        return False
    return False


def _cell_is_formula(cell: Any) -> bool:
    """True if cell contains a formula (getType() == 3 or starts with '=')."""
    return bool(
        uno_read(
            lambda: cell.getType() == 3 or str(cell.getFormula() or "").startswith("="),
            False,
        )
    )


def cleanup_orphans(
    state: WorkbookState,
    sheet: Any,
    *,
    doc: Any,
    sheet_name: str,
) -> None:
    """Orphaned-spill cleanup: remap moved formula cells or clear deleted spill cells."""
    try:
        if not main.is_current():
            return

        from plugin.calc.python.sheet_modify import sheet_modify_scope
        from plugin.calc.python.cell_discovery import is_py_formula_text

        with sheet_modify_scope():
            to_remove: list[tuple[str, int, int]] = []
            to_remap: list[tuple[tuple[str, int, int], tuple[str, int, int], SpillRecord]] = []
            for (s_name, frow, fcol), record in list(state.spills.items()):
                if s_name == sheet_name:
                    try:
                        cell = sheet.getCellByPosition(fcol, frow)
                        formula = cell.getFormula()
                        formula_str = str(formula) if formula else ""
                        formula_matches = (
                            is_matching_py_formula(formula_str, record.code)
                            if record.code
                            else is_py_formula_text(formula_str)
                        )
                        if formula_matches:
                            continue

                        remapped = False
                        if record.code:
                            loc = search_sheet_for_formula(sheet, record.code, doc=doc)
                            if loc is not None:
                                _loc_cell, new_row, new_col = loc
                                d_row = new_row - frow
                                d_col = new_col - fcol
                                if d_row != 0 or d_col != 0:
                                    all_match = all(
                                        _cell_matches_stored_value(sheet, c.row + d_row, c.col + d_col, c)
                                        for c in record.cells
                                    )
                                    if all_match:
                                        new_cells = [
                                            SpillCell(c.row + d_row, c.col + d_col, c.kind, c.value)
                                            for c in record.cells
                                        ]
                                        new_key = (sheet_name, new_row, new_col)
                                        to_remap.append(((sheet_name, frow, fcol), new_key, SpillRecord(code=record.code, cells=new_cells)))
                                        remapped = True

                        if not remapped:
                            to_remove.append((sheet_name, frow, fcol))
                    except Exception:
                        log.debug("Failed to inspect formula cell %r", (sheet_name, frow, fcol), exc_info=True)

            if to_remove or to_remap:
                with undo_lock(doc):
                    for key in to_remove:
                        removed_rec = state.spills.pop(key, None)
                        if removed_rec is not None:
                            frow, fcol = key[1], key[2]
                            for c in removed_rec.cells:
                                if (c.row, c.col) != (frow, fcol):
                                    try:
                                        spill_cell = sheet.getCellByPosition(c.col, c.row)
                                        if not _cell_is_formula(spill_cell):
                                            if _cell_matches_stored_value(sheet, c.row, c.col, c):
                                                spill_cell.clearContents(_SPILL_CLEAR_FLAGS)
                                    except Exception:
                                        log.debug("cleanup_orphans: failed to clear cell (%d, %d)", c.row, c.col, exc_info=True)
                    for old_k, new_k, new_rec in to_remap:
                        state.spills.pop(old_k, None)
                        state.spills[new_k] = new_rec
                    save_spill_registry_for_doc(doc, state)
    except Exception:
        log.exception("Error in cleanup_orphans")


def load_spill_registry_for_doc(doc: Any, state: WorkbookState) -> None:
    """Load the document's spill registry from its UserDefinedProperties."""
    try:
        from plugin.doc.udprops import get_document_property

        raw = get_document_property(doc, "WriterAgentSpillRegistry", None)
        if not isinstance(raw, str) or not raw.strip():
            return
        data = json.loads(raw)
        if not state.key:
            return
        for key, value in data.items():
            try:
                parts = key.split(":")
                if len(parts) == 2:
                    sheet_name, coords = parts
                    row_col = coords.split(",")
                    if len(row_col) == 2:
                        frow, fcol = int(row_col[0]), int(row_col[1])
                        if isinstance(value, dict):
                            code = str(value.get("code") or "")
                            raw_cells = value.get("cells") or []
                            cells = []
                            for c in raw_cells:
                                if isinstance(c, dict) and "r" in c and "c" in c and "t" in c:
                                    r = int(c["r"] or 0)
                                    col = int(c["c"] or 0)
                                    kind = str(c["t"])
                                    v = spill_value_from_json(kind, c.get("v"))
                                    cells.append(SpillCell(row=r, col=col, kind=kind, value=v))
                            state.spills[(sheet_name, frow, fcol)] = SpillRecord(code=code, cells=cells)
            except Exception:
                log.warning("spill registry skipped malformed entry %r", key, exc_info=True)
                continue
    except Exception:
        log.exception("Failed to load spill registry from document property")


def save_spill_registry_for_doc(doc: Any, state: WorkbookState) -> None:
    """Save the document's spill registry to its UserDefinedProperties."""
    try:
        from plugin.doc.udprops import set_document_property

        if not state.key:
            return
        doc_spills: dict[str, Any] = {}
        for (sheet_name, frow, fcol), record in state.spills.items():
            cells_data = []
            for c in record.cells:
                item: dict[str, Any] = {"r": c.row, "c": c.col, "t": c.kind}
                if c.kind == "n" and isinstance(c.value, float) and not math.isfinite(c.value):
                    item["v"] = "nan" if math.isnan(c.value) else ("inf" if c.value > 0 else "-inf")
                elif c.value is not None:
                    item["v"] = c.value
                cells_data.append(item)
            doc_spills[f"{sheet_name}:{frow},{fcol}"] = {
                "code": record.code,
                "cells": cells_data,
            }
        new_val = json.dumps(doc_spills)
        set_document_property(doc, "WriterAgentSpillRegistry", new_val)
    except Exception:
        log.exception("Failed to save spill registry to document property")


def perform_deferred_spill(
    sheet_name: str,
    formula_row: int,
    formula_col: int,
    grid: list[list[Any]],
    *,
    doc: Any,
    sheet: Any,
    code: str,
    key: str,
) -> None:
    """Clear old spilled cells and write new values deferred (collision check is done synchronously).

    The caller passes the sheet it located, the code it located it by, and ``WorkbookState.key``.
    The state key, the sheet looked up by name and the origin formula are checked again here
    on purpose: 0.1 s has passed since ``prepare_auto_spill``, and the sheet may have been
    renamed or the formula edited in between.
    """
    try:
        if not main.is_current():
            return

        state = WorkbookState.for_doc(doc)
        if state is None:
            return

        with undo_lock(doc):
            if not state.scheduled_key_matches(key):
                return

            resolved_sheet = sheet
            try:
                named = doc.getSheets().getByName(sheet_name)
                if named is not None:
                    resolved_sheet = named
            except Exception:  # noqa: S110
                pass
            sheet = resolved_sheet
            live_sheet_name = ""
            try:
                if hasattr(sheet, "getName"):
                    live_sheet_name = str(sheet.getName() or "")
            except Exception:
                return
            if live_sheet_name and live_sheet_name != sheet_name:
                try:
                    from plugin.calc.python.sheet_modify import migrate_sheet_rename

                    migrate_sheet_rename(doc, sheet_name, live_sheet_name)
                except Exception:
                    log.debug("perform_deferred_spill: rename migration failed", exc_info=True)
                sheet_name = live_sheet_name

            try:
                origin_cell = sheet.getCellByPosition(formula_col, formula_row)
                if not is_matching_py_formula(origin_cell.getFormula(), code):
                    return
            except Exception:
                log.debug("perform_deferred_spill: origin formula check failed", exc_info=True)
                return

            reg_key = (sheet_name, formula_row, formula_col)

            from plugin.calc.python.sheet_modify import sheet_modify_scope

            with sheet_modify_scope():
                previous_record = state.spills.get(reg_key)
                if previous_record is not None:
                    old_coords = previous_record.spills
                    for r, c in old_coords:
                        if (r, c) != (formula_row, formula_col):
                            try:
                                cell = sheet.getCellByPosition(c, r)
                                cell.clearContents(_SPILL_CLEAR_FLAGS)
                            except Exception:
                                log.debug("perform_deferred_spill: failed to clear cell (%d, %d)", r, c, exc_info=True)

                num_rows = len(grid)
                num_cols = max(len(row) for row in grid) if num_rows > 0 else 0
                if num_rows == 0 or num_cols == 0:
                    state.spills[reg_key] = SpillRecord(code=code, cells=[])
                    save_spill_registry_for_doc(doc, state)
                    return

                settings = doc.getNumberFormatSettings()
                nd = settings.getPropertyValue("NullDate")
                null_dt = datetime.date(nd.Year, nd.Month, nd.Day)

                coerced_grid: list[list[Any]] = []
                cell_metas: list[list[dict[str, Any]]] = []
                has_any_temporal = False
                new_cells: list[SpillCell] = []
                error_writes: list[tuple[int, int, int]] = []

                for r_offset, row in enumerate(grid):
                    coerced_row: list[Any] = []
                    meta_row: list[dict[str, Any]] = []
                    for c_offset in range(num_cols):
                        val = row[c_offset] if c_offset < len(row) else None
                        calc_val, meta = coerce_spill_value(val, null_dt)
                        if meta.get("is_temporal"):
                            has_any_temporal = True
                        coerced_row.append(calc_val)
                        meta_row.append(meta)
                        if (r_offset, c_offset) == (0, 0):
                            continue
                        stored_val: float | str | int | None = None
                        if meta.get("is_calc_error"):
                            kind = "err"
                            stored_val = int(meta["error_code"])
                            if meta.get("error_code") != ERR_NA:
                                error_writes.append((formula_col + c_offset, formula_row + r_offset, int(meta["error_code"])))
                        elif meta.get("is_empty"):
                            kind = "e"
                        elif isinstance(calc_val, float):
                            kind = "n"
                            stored_val = calc_val
                        elif isinstance(calc_val, str):
                            kind = "s"
                            stored_val = calc_val
                        else:
                            kind = "e"
                        new_cells.append(SpillCell(row=formula_row + r_offset, col=formula_col + c_offset, kind=kind, value=stored_val))
                    coerced_grid.append(coerced_row)
                    cell_metas.append(meta_row)

                if num_cols > 1:
                    first_row_range = sheet.getCellRangeByPosition(formula_col + 1, formula_row, formula_col + num_cols - 1, formula_row)
                    first_row_range.setDataArray((tuple(coerced_grid[0][1:]),))

                if num_rows > 1:
                    remaining_range = sheet.getCellRangeByPosition(formula_col, formula_row + 1, formula_col + num_cols - 1, formula_row + num_rows - 1)
                    remaining_range.setDataArray(tuple(tuple(row) for row in coerced_grid[1:]))

                for err_col, err_row, err_code in error_writes:
                    sheet.getCellByPosition(err_col, err_row).setFormula(f"=#ERR{err_code}!")
                state.spills[reg_key] = SpillRecord(code=code, cells=new_cells)
                save_spill_registry_for_doc(doc, state)

                if has_any_temporal:
                    try:
                        formats = doc.getNumberFormats()
                        locale = doc.getPropertyValue("CharLocale")
                        if not getattr(locale, "Language", None):
                            import uno

                            locale = uno.createUnoStruct("com.sun.star.lang.Locale", Language="en", Country="US", Variant="")

                        standard_keys: dict[str, int] = {}
                        standard_keys["date"] = int(formats.getStandardFormat(2, locale))
                        standard_keys["time"] = int(formats.getStandardFormat(4, locale))
                        standard_keys["datetime"] = int(formats.getStandardFormat(6, locale))
                        dur_key = formats.getFormatIndex(43, locale)
                        standard_keys["duration"] = int(dur_key) if dur_key != -1 else 43

                        category_cache: dict[int, str | None] = {}
                        decisions: list[list[Any]] = []

                        for r_idx in range(num_rows):
                            row_dec: list[Any] = []
                            for c_idx in range(num_cols):
                                meta = cell_metas[r_idx][c_idx]
                                if meta.get("is_temporal"):
                                    in_cat = meta["input_category"]
                                    serial_val = meta["serial"]
                                    target_c = formula_col + c_idx
                                    target_r = formula_row + r_idx
                                    cell = sheet.getCellByPosition(target_c, target_r)
                                    k = int(cell.getPropertyValue("NumberFormat"))
                                    if k not in category_cache:
                                        props = formats.getByKey(k)
                                        category_cache[k] = format_category_from_type(props.getPropertyValue("Type"))
                                    dest_cat = category_cache[k]
                                    if should_preserve_temporal_format(in_cat, float(serial_val), dest_cat):
                                        row_dec.append(("preserve", None))
                                    else:
                                        apply_k = standard_keys.get(in_cat, standard_keys["date"])
                                        row_dec.append(("apply", int(apply_k)))
                                elif meta.get("is_empty"):
                                    row_dec.append("empty")
                                else:
                                    row_dec.append(None)
                            decisions.append(row_dec)

                        rects = coalesce_temporal_apply_rects(decisions)
                        for r0, r1, c0, c1, k in rects:
                            trange = sheet.getCellRangeByPosition(formula_col + c0, formula_row + r0, formula_col + c1, formula_row + r1)
                            trange.setPropertyValue("NumberFormat", int(k))
                    except Exception:
                        log.exception("Error applying temporal number formats during deferred spill")

    except Exception:
        log.exception("Error in perform_deferred_spill")


def cell_is_matrix(sheet: Any, cell: Any) -> bool:
    """True when the located formula cell is part of a multi-cell array formula block."""

    def _probe() -> bool:
        if hasattr(sheet, "createCursorByRange") and cell is not None:
            cursor = sheet.createCursorByRange(cell)
            if hasattr(cursor, "collapseToCurrentArray"):
                cursor.collapseToCurrentArray()
                addr = cursor.getRangeAddress()
                return (addr.EndColumn > addr.StartColumn) or (addr.EndRow > addr.StartRow)
        return False

    return bool(uno_read(_probe, False))


@dataclass(frozen=True, slots=True)
class PreparedSpill:
    """A located formula origin whose spill fits: prepare_auto_spill returns it on the UI thread and queue_deferred_spill_write writes the neighbors from it.

    ``key`` is the workbook's ``WorkbookState.key``; ``code`` is what the origin was located by and what ``perform_deferred_spill`` re-matches.
    """

    sheet_name: str
    formula_row: int
    formula_col: int
    sheet: Any
    key: str
    code: str


def prepare_auto_spill(
    ctx: Any,
    doc: Any,
    state: WorkbookState,
    origin: FormulaOrigin,
    code: str,
    grid_to_spill: list[list[Any]],
) -> PreparedSpill | str | None:
    """Check spill collisions for an already-located formula origin (UNO / UI thread).

    Called with the origin already located and known not to be an array block
    (``CellContext.is_matrix`` on the main path, ``cell_is_matrix`` in the off-main
    callback). Returns ``"#SPILL!"`` on collision, ``PreparedSpill`` when the neighbor
    write should proceed, or ``None`` when ``state.key`` is empty.
    """
    if not state.key:
        return None
    sheet = origin.sheet
    formula_row = origin.row
    formula_col = origin.col
    sheet_name = origin.sheet_name
    log.debug("Spill: checking formula cell at %r on sheet %r for code %r", (formula_row, formula_col), sheet_name, code)

    if not state.loaded:
        load_spill_registry_for_doc(doc, state)
        state.loaded = True

    try:
        from plugin.calc.python.sheet_modify import ensure_sheet_modify_listener

        ensure_sheet_modify_listener(ctx, doc, sheet)
    except Exception:
        log.exception("Failed to register modify listener on sheet")

    num_rows = len(grid_to_spill)
    num_cols = max(len(row) for row in grid_to_spill) if num_rows > 0 else 0
    reg_key = (sheet_name, formula_row, formula_col)
    previous_record = state.spills.get(reg_key)
    previous_spills = previous_record.spills if previous_record is not None else []
    prev_spill_set = set(previous_spills)

    log.debug("Spill: previous spills for cell %r: %r", reg_key, previous_spills)

    max_cols = int(sheet.getColumns().getCount())
    max_rows = int(sheet.getRows().getCount())

    for r_idx in range(num_rows):
        for c_idx in range(num_cols):
            if r_idx == 0 and c_idx == 0:
                continue
            target_r = formula_row + r_idx
            target_c = formula_col + c_idx
            if target_r >= max_rows or target_c >= max_cols:
                log.debug("Spill: collision: target coordinate %r is out of bounds", (target_r, target_c))
                return "#SPILL!"
            if (target_r, target_c) in prev_spill_set:
                continue
            cell = sheet.getCellByPosition(target_c, target_r)
            cell_type = cell.getType()
            if cell_type != EMPTY:
                log.debug("Spill: collision: cell at %r (type=%s, val=%r, formula=%r) is not empty", (target_r, target_c), cell_type, cell.getValue() or cell.getString(), cell.getFormula())
                return "#SPILL!"
    return PreparedSpill(sheet_name, formula_row, formula_col, sheet=sheet, key=state.key, code=code)


def queue_deferred_spill_write(state: WorkbookState, doc: Any, prepared: PreparedSpill, grid_to_spill: list[list[Any]]) -> None:
    """Post neighbor writes to the UI thread after the usual 0.1s settle."""
    token = f"spill:{prepared.sheet_name}:{prepared.formula_row}:{prepared.formula_col}"

    def _write() -> None:
        perform_deferred_spill(
            prepared.sheet_name,
            prepared.formula_row,
            prepared.formula_col,
            grid_to_spill,
            doc=doc,
            sheet=prepared.sheet,
            code=prepared.code,
            key=prepared.key,
        )

    state.debounce.schedule(token, 0.1, _write)


def queue_off_main_auto_spill(ctx: Any, code: str, grid_to_spill: list[list[Any]], doc: Any) -> None:
    """Locate and write the spill on the UI thread in the calling document.

    Off-main finalize must not touch UNO: *doc* (the add-in caller) is
    only passed through to the UI-thread callback. Collision ``#SPILL!`` is too
    late to change the add-in return; the deferred path just skips the write.
    """
    log.debug("Spill: scheduling off-main deferred locate code=%r", code)

    def _on_main(state: WorkbookState) -> None:
        try:
            # Locate on the UI thread because the off-main add-in call must not touch UNO on the document.
            located = locate_formula_cell_in_doc(doc, code)
            if located is None:
                return
            origin = FormulaOrigin.from_located(located)
            if cell_is_matrix(origin.sheet, origin.cell):
                return
            prepared = prepare_auto_spill(ctx, doc, state, origin, code, grid_to_spill)
        except Exception:
            log.exception("Error checking spill collision or locating formula cell")
            return
        if not isinstance(prepared, PreparedSpill):
            return

        perform_deferred_spill(
            prepared.sheet_name,
            prepared.formula_row,
            prepared.formula_col,
            grid_to_spill,
            doc=doc,
            sheet=prepared.sheet,
            code=prepared.code,
            key=prepared.key,
        )

    def _arm_on_ui() -> None:
        # UI callback must create state when known is empty (multi-doc spill).
        state = WorkbookState.lookup(doc)
        if state is None or not state.key:
            log.debug("Spill: no workbook state on UI thread; skipping off-main auto-spill")
            return
        state.debounce.schedule(f"code:{code}", 0.1, lambda: _on_main(state))

    main.post(_arm_on_ui)
