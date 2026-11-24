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
import re
from typing import Any

from plugin.calc.address_utils import format_address, format_range, qualify_a1
from plugin.calc.calc_utils import EMPTY, NumberFormatCategories, resolve_document_locale, standard_temporal_format_keys
from plugin.calc.datetime_wire import (
    coalesce_temporal_apply_rects,
    should_preserve_temporal_format,
)
from plugin.calc.python.addin_values import (
    ERR_NA,
    SpillValue,
    coerce_spill_value,
)
from plugin.calc.python.cell_discovery import is_py_formula_text
from plugin.calc.python.diagnostics import diagnostics_workbook_key, get_diagnostics_store
from plugin.calc.python.formula_locator_cache import (
    FormulaOrigin,
    is_matching_py_formula,
    locate_formula_cell_in_doc,
    search_sheet_for_formula,
)
from plugin.calc.python.sheet_modify import ensure_sheet_modify_listener, migrate_sheet_rename, sheet_modify_scope
from plugin.calc.python.undo import undo_lock
from plugin.calc.python.workbook_state import WorkbookState
from plugin.doc.udprops import get_document_property, set_document_property
from plugin.framework.errors import best_effort, uno_read
from plugin.framework.queue_executor import main

log = logging.getLogger(__name__)

# CellFlags bitmask: VALUE (1) | DATETIME (2) | STRING (4) | FORMULA (16) | HARDATTR (32) = 55.
# Clears values, formulas, and direct hard cell formatting/number formats without removing
# annotations, styles, or draw objects.
SPILL_CLEAR_FLAGS = 1 | 2 | 4 | 16 | 32


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


# A spilled error cell reads back as =#ERR504!, =#NUM! and so on, a void cell as #N/A, and any error
# as =NA() after .xls. Any bare error literal counts as ours: it holds no user data, and XLSX turns
# =#NULL! into =#ERR511!. A user formula such as =1/0 does not match.
_ERROR_LITERAL_RE = re.compile(r"=#[A-Z0-9/]+[!?]?|#N/A|=NA\(\)")

# How getFormulaArray writes a VALUE cell (always English General, lcl_GetInputString in
# sc/source/ui/unoobj/cellsuno.cxx). Text that looks like a number gets a leading apostrophe there.
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:E[+-]\d+)?", re.ASCII)


def _matches_stored_value(formula: str, rec: SpillCell) -> bool:
    """True when a cell whose input text is *formula* still holds the value *rec* wrote."""
    if rec.kind == "e":
        return formula == ""
    if rec.kind == "n":
        # Input text and saved files keep 15 significant digits (at most 5e-15 relative error).
        return (
            isinstance(rec.value, (int, float))
            and _NUMBER_RE.fullmatch(formula) is not None
            and math.isclose(float(formula), float(rec.value), rel_tol=1e-14)
        )
    if rec.kind == "s":
        # Calc stores every line break as \n, and quotes number-like text of up to 308 characters
        # (ImpSvNumberInputScan::IsNumberFormat, svl/source/numbers/zforfind.cxx:3848), so a shorter
        # unquoted number is one the user typed.
        text = str(rec.value or "").replace("\r\n", "\n").replace("\r", "\n")
        return formula == f"'{text}" or (
            formula == text and (len(text) > 308 or _NUMBER_RE.fullmatch(text) is None)
        )
    return rec.kind == "err" and _ERROR_LITERAL_RE.fullmatch(formula) is not None


def _unchanged_cells(sheet: Any, cells: list[SpillCell]) -> set[tuple[int, int]]:
    """(row, col) of every cell in *cells* that still holds the value the spill wrote there.

    Reads the input text in one call: a =PY() recalculation is running, and reading values would
    calculate a user formula typed over the spill, which may depend on the running cell.
    """
    if not cells:
        return set()
    top = min(c.row for c in cells)
    left = min(c.col for c in cells)
    try:
        formulas = sheet.getCellRangeByPosition(
            left, top, max(c.col for c in cells), max(c.row for c in cells)
        ).getFormulaArray()
    except Exception:
        # The record can name cells past the edge of a file saved with a smaller sheet.
        log.debug("_unchanged_cells: could not read the spilled range", exc_info=True)
        return set()
    return {
        (c.row, c.col) for c in cells if _matches_stored_value(str(formulas[c.row - top][c.col - left]), c)
    }


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
                                    new_cells = [
                                        SpillCell(c.row + d_row, c.col + d_col, c.kind, c.value)
                                        for c in record.cells
                                    ]
                                    if len(_unchanged_cells(sheet, new_cells)) == len(new_cells):
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
                            for row, col in _unchanged_cells(sheet, removed_rec.cells):
                                if (row, col) != (frow, fcol):
                                    try:
                                        sheet.getCellByPosition(col, row).clearContents(SPILL_CLEAR_FLAGS)
                                    except Exception:
                                        log.debug("cleanup_orphans: failed to clear cell (%d, %d)", row, col, exc_info=True)
                    for old_k, new_k, new_rec in to_remap:
                        state.spills.pop(old_k, None)
                        state.spills[new_k] = new_rec
                    save_spill_registry_for_doc(doc, state)
    except Exception:
        log.exception("Error in cleanup_orphans")


def load_spill_registry_for_doc(doc: Any, state: WorkbookState) -> None:
    """Load the document's spill registry from its UserDefinedProperties."""
    try:
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
                                    cells.append(SpillCell(row=r, col=col, kind=kind, value=c.get("v")))
                            state.spills[(sheet_name, frow, fcol)] = SpillRecord(code=code, cells=cells)
            except Exception:
                log.warning("spill registry skipped malformed entry %r", key, exc_info=True)
                continue
    except Exception:
        log.exception("Failed to load spill registry from document property")


def save_spill_registry_for_doc(doc: Any, state: WorkbookState) -> None:
    """Save the document's spill registry to its UserDefinedProperties."""
    try:
        if not state.key:
            return
        doc_spills: dict[str, Any] = {}
        for (sheet_name, frow, fcol), record in state.spills.items():
            cells_data = []
            for c in record.cells:
                item: dict[str, Any] = {"r": c.row, "c": c.col, "t": c.kind}
                if c.value is not None:
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


def _classify_spill_grid(grid: list[list[Any]], num_cols: int, null_dt: datetime.date) -> list[list[SpillValue]]:
    return [
        [coerce_spill_value(row[c] if c < len(row) else None, null_dt) for c in range(num_cols)]
        for row in grid
    ]


def _write_spill_values(sheet: Any, formula_row: int, formula_col: int, values: list[list[SpillValue]]) -> None:
    num_rows = len(values)
    num_cols = len(values[0])
    if num_cols > 1:
        first_row_range = sheet.getCellRangeByPosition(formula_col + 1, formula_row, formula_col + num_cols - 1, formula_row)
        first_row_range.setDataArray((tuple(v.calc_value for v in values[0][1:]),))

    if num_rows > 1:
        remaining_range = sheet.getCellRangeByPosition(formula_col, formula_row + 1, formula_col + num_cols - 1, formula_row + num_rows - 1)
        remaining_range.setDataArray(tuple(tuple(v.calc_value for v in row) for row in values[1:]))

    # Write error cells in coalesced blocks: per-cell setFormula took 7.2 s for 10,000 cells
    # over the UNO bridge vs. 0.07 s for one setFormulaArray.
    decisions: list[list[Any]] = [
        [
            ("apply", v.error_code)
            if (r_offset, c_offset) != (0, 0) and v.kind == "err" and v.error_code is not None and v.error_code != ERR_NA
            else None
            for c_offset, v in enumerate(row)
        ]
        for r_offset, row in enumerate(values)
    ]
    for r0, r1, c0, c1, code in coalesce_temporal_apply_rects(decisions):
        formula = f"=#ERR{code}!"
        rows = r1 - r0 + 1
        cols = c1 - c0 + 1
        err_range = sheet.getCellRangeByPosition(formula_col + c0, formula_row + r0, formula_col + c1, formula_row + r1)
        err_range.setFormulaArray(((formula,) * cols,) * rows)


def _apply_spill_temporal_formats(
    doc: Any, sheet: Any, formula_row: int, formula_col: int, values: list[list[SpillValue]]
) -> None:
    if not any(v.temporal_category is not None for row in values for v in row):
        return
    try:
        formats = doc.getNumberFormats()
        locale = resolve_document_locale(doc)
        standard_keys = standard_temporal_format_keys(formats, locale)

        categories = NumberFormatCategories(formats)
        decisions: list[list[Any]] = []

        for r_idx, row in enumerate(values):
            row_dec: list[Any] = []
            for c_idx, v in enumerate(row):
                if v.temporal_category is not None:
                    cell = sheet.getCellByPosition(formula_col + c_idx, formula_row + r_idx)
                    dest_cat = categories.category(int(cell.getPropertyValue("NumberFormat")))
                    if should_preserve_temporal_format(v.temporal_category, float(v.calc_value), dest_cat):
                        row_dec.append(("preserve", None))
                    else:
                        apply_k = standard_keys.get(v.temporal_category, standard_keys["date"])
                        row_dec.append(("apply", int(apply_k)))
                elif v.kind == "e":
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
    """Clear old spilled cells and write new values deferred after rechecking collisions.

    After rechecking that targets are still free, this clears old cells, classifies the grid into ``SpillValue`` records,
    writes neighbour values/errors and updates the spill registry, then applies
    standard temporal number formats across coalesced blocks.

    The caller passes the sheet it located, the code it located it by, and ``WorkbookState.key``.
    The state key, the sheet looked up by name, the origin formula, and target collisions are
    checked again here on purpose: 0.1 s has passed since ``prepare_auto_spill``, and the sheet
    may have been renamed, the formula edited, or a target cell filled in between.
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

            with sheet_modify_scope():
                previous_record = state.spills.get(reg_key)
                num_rows = len(grid)
                num_cols = max(len(row) for row in grid) if num_rows > 0 else 0
                owned = _unchanged_cells(sheet, previous_record.cells) if previous_record is not None else set()
                if _spill_is_blocked(doc, code, sheet, sheet_name, formula_row, formula_col, num_rows, num_cols, owned):
                    return
                if previous_record is not None:
                    for r, c in previous_record.spills:
                        if (r, c) != (formula_row, formula_col):
                            try:
                                cell = sheet.getCellByPosition(c, r)
                                # A cell the user changed is theirs; one they emptied only keeps our number format.
                                if (r, c) in owned or cell.getType() == EMPTY:
                                    cell.clearContents(SPILL_CLEAR_FLAGS)
                            except Exception:
                                log.debug("perform_deferred_spill: failed to clear cell (%d, %d)", r, c, exc_info=True)

                if num_rows == 0 or num_cols == 0:
                    state.spills[reg_key] = SpillRecord(code=code, cells=[])
                    save_spill_registry_for_doc(doc, state)
                    return

                settings = doc.getNumberFormatSettings()
                nd = settings.getPropertyValue("NullDate")
                null_dt = datetime.date(nd.Year, nd.Month, nd.Day)

                values = _classify_spill_grid(grid, num_cols, null_dt)
                new_cells = [
                    SpillCell(row=formula_row + r, col=formula_col + c, kind=v.kind, value=v.registry_value)
                    for r, row in enumerate(values)
                    for c, v in enumerate(row)
                    if (r, c) != (0, 0)
                ]
                _write_spill_values(sheet, formula_row, formula_col, values)
                state.spills[reg_key] = SpillRecord(code=code, cells=new_cells)
                save_spill_registry_for_doc(doc, state)
                _apply_spill_temporal_formats(doc, sheet, formula_row, formula_col, values)

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


def _record_spill_blocked(
    doc: Any,
    code: str,
    sheet_name: str,
    row: int,
    col: int,
    num_rows: int,
    num_cols: int,
    blocker: tuple[int, int] | None,
) -> None:
    """Record why a =PY spill was blocked in the diagnostics store."""
    if blocker is not None:
        needed = qualify_a1(
            sheet_name,
            format_range(col, row, col + num_cols - 1, row + num_rows - 1, collapse_single=True),
        )
        msg = (
            f"#SPILL!: the {num_rows} x {num_cols} result needs {needed}, "
            f"but {format_address(blocker[0], blocker[1])} is not empty. "
            "Nothing beside the formula was written; clear that cell or move the formula."
        )
    else:
        origin_ref = qualify_a1(sheet_name, format_address(col, row))
        msg = (
            f"#SPILL!: the {num_rows} x {num_cols} result from {origin_ref} "
            "runs past the edge of the sheet. Move the formula up or left."
        )
    best_effort(
        lambda: get_diagnostics_store().record(
            workbook_key=diagnostics_workbook_key(doc),
            code=code,
            status="error",
            message=msg,
        ),
        "record blocked spill",
    )


def _spill_is_blocked(
    doc: Any,
    code: str,
    sheet: Any,
    sheet_name: str,
    row: int,
    col: int,
    num_rows: int,
    num_cols: int,
    owned: set[tuple[int, int]],
) -> bool:
    """Return True and record diagnostics if any target cell is out of bounds or occupied outside ``owned``."""
    max_cols = int(sheet.getColumns().getCount())
    max_rows = int(sheet.getRows().getCount())

    for r_idx in range(num_rows):
        for c_idx in range(num_cols):
            if r_idx == 0 and c_idx == 0:
                continue
            target_r = row + r_idx
            target_c = col + c_idx
            if target_r >= max_rows or target_c >= max_cols:
                log.debug("Spill: collision: target coordinate %r is out of bounds", (target_r, target_c))
                _record_spill_blocked(
                    doc=doc,
                    code=code,
                    sheet_name=sheet_name,
                    row=row,
                    col=col,
                    num_rows=num_rows,
                    num_cols=num_cols,
                    blocker=None,
                )
                return True
            if (target_r, target_c) in owned:
                continue
            cell = sheet.getCellByPosition(target_c, target_r)
            cell_type = cell.getType()
            if cell_type != EMPTY:
                log.debug("Spill: collision: cell at %r (type=%s, formula=%r) is not empty", (target_r, target_c), cell_type, cell.getFormula())
                _record_spill_blocked(
                    doc=doc,
                    code=code,
                    sheet_name=sheet_name,
                    row=row,
                    col=col,
                    num_rows=num_rows,
                    num_cols=num_cols,
                    blocker=(target_c, target_r),
                )
                return True
    return False


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
    callback). Returns ``"#SPILL!"`` on collision (recording the reason in the
    diagnostics store, which the LibrePy sidebar and ``write_range`` read),
    ``PreparedSpill`` when the neighbor write should proceed, or ``None`` when
    ``state.key`` is empty.
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
        ensure_sheet_modify_listener(ctx, doc, sheet)
    except Exception:
        log.exception("Failed to register modify listener on sheet")

    num_rows = len(grid_to_spill)
    num_cols = max(len(row) for row in grid_to_spill) if num_rows > 0 else 0
    reg_key = (sheet_name, formula_row, formula_col)
    previous_record = state.spills.get(reg_key)
    owned = _unchanged_cells(sheet, previous_record.cells) if previous_record is not None else set()

    log.debug("Spill: owned cells for cell %r: %r", reg_key, owned)

    if _spill_is_blocked(doc, code, sheet, sheet_name, formula_row, formula_col, num_rows, num_cols, owned):
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
