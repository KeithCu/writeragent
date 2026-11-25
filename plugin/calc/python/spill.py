# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc spill array tracking, collision detection, and orphan cleanup.

Each spill is stored as a single rectangle (rows, cols) from the formula cell,
and moved formulas are confirmed by the sheet's used-area end delta.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import json
import logging
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
    CELL_FLAG_FORMULA,
    FormulaOrigin,
    collect_matching_formula_origins,
    is_matching_py_formula,
    iter_range_formula_texts,
    locate_formula_cell_in_doc,
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
SPILL_CONTENT_FLAGS = 1 | 2 | 4 | 16
SPILL_CLEAR_FLAGS = SPILL_CONTENT_FLAGS | 32

# A spilled error cell reads back as =#ERR504!, =#NUM! and so on, a void cell as #N/A, and any error
# as =NA() after .xls. Any bare error literal counts as ours: it holds no user data, and XLSX turns
# =#NULL! into =#ERR511!. A user formula such as =1/0 does not match.
_ERROR_LITERAL_RE = re.compile(r"=#[A-Z0-9/]+[!?]?|#N/A|=NA\(\)")


@dataclass
class SpillRecord:
    """One =PY() spill: the code it was located by and the rectangle it wrote, measured from
    the formula cell. rows and cols count the formula cell, so 1 x 1 means nothing beside it.
    values is what the cells beside the formula held when this session last found the formula at
    its origin, and it is never saved. None means it was not found there (a record stale when
    the file opened) or nothing is remembered; such a record clears nothing when its formula is
    gone.
    """

    code: str
    rows: int
    cols: int
    values: tuple[Any, ...] | None = None


def _used_end(sheet: Any) -> tuple[int, int]:
    """Return the sheet's used-area end (EndRow, EndColumn).

    Insert/delete of rows or columns before a block shifts both the formula and the
    used-area end by the same delta, whereas a one-cell cut/paste or sort does not.
    """
    cursor = sheet.createCursor()
    cursor.gotoEndOfUsedArea(False)
    addr = cursor.getRangeAddress()
    return (int(addr.EndRow), int(addr.EndColumn))


def _arm_sheet(ctx: Any, doc: Any, state: WorkbookState, sheet: Any, sheet_name: str) -> None:
    ensure_sheet_modify_listener(ctx, doc, sheet)
    if sheet_name not in state.used_ends:
        state.used_ends[sheet_name] = _used_end(sheet)


def _advance_used_end(
    state: WorkbookState, sheet: Any, sheet_name: str, before: tuple[int, int]
) -> None:
    """Shift state.used_ends[sheet_name] by what our own write changed, preserving any pending structural delta."""
    after = _used_end(sheet)
    prev = state.used_ends.get(sheet_name)
    if prev is None:
        state.used_ends[sheet_name] = after
    else:
        state.used_ends[sheet_name] = (prev[0] + after[0] - before[0], prev[1] + after[1] - before[1])


def _user_formula_in_rect(sheet: Any, row: int, col: int, rows: int, cols: int) -> tuple[int, int] | None:
    """Return (col, row) of the first user formula cell in the rectangle other than the origin.

    Read formula text, never values or queryFormulaCells: prepare_auto_spill runs while
    =PY() is being interpreted, so interpreting a user formula in the rectangle that depends
    on the origin would hit CircularReference (yielding an error result) or trigger iteration.
    """
    if rows * cols <= 1:
        return None
    try:
        rect = sheet.getCellRangeByPosition(col, row, col + cols - 1, row + rows - 1)
    except Exception:
        # The record can name cells past the edge of a file saved with a smaller sheet.
        log.debug("_user_formula_in_rect: could not read the spilled range", exc_info=True)
        return None
    for addr in rect.queryContentCells(CELL_FLAG_FORMULA).getRangeAddresses() or ():
        sub_range = sheet.getCellRangeByPosition(addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow)
        for r, c, formula in iter_range_formula_texts(sheet, sub_range):
            if (r, c) == (row, col):
                continue
            if _ERROR_LITERAL_RE.fullmatch(formula) is None:
                return (c, r)
    return None


def _neighbour_coords(row: int, col: int, rows: int, cols: int) -> list[tuple[int, int, int, int]]:
    """Return (c1, r1, c2, r2) sub-ranges covering the spilled rectangle without the origin cell."""
    sub_coords: list[tuple[int, int, int, int]] = []
    if cols > 1:
        sub_coords.append((col + 1, row, col + cols - 1, row))
    if rows > 1:
        sub_coords.append((col, row + 1, col + cols - 1, row + rows - 1))
    return sub_coords


def _rect_values(sheet: Any, row: int, col: int, rows: int, cols: int) -> tuple[Any, ...] | None:
    """Snapshot current cell values beside the origin. Call only outside =PY() interpretation."""
    # The origin is excluded so reading it never triggers interpretation of the =PY() cell.
    coords = _neighbour_coords(row, col, rows, cols)
    return uno_read(
        lambda: tuple(sheet.getCellRangeByPosition(*c).getDataArray() for c in coords),
        None,
    )


def _rect_unchanged(sheet: Any, row: int, col: int, record: SpillRecord) -> bool:
    """True when every non-empty neighbour still holds the remembered value."""
    if record.values is None:
        return False
    current = _rect_values(sheet, row, col, record.rows, record.cols)
    if current is None or len(current) != len(record.values):
        return False
    # An emptied cell counts as unchanged so deleting the formula with part of its block still
    # clears the rest; a paste or Delete Cells that replaced the origin changes neighbours in the
    # same event (ScRangeToSequence::FillMixedArray returns "" for an empty cell).
    for cur_sub, old_sub in zip(current, record.values, strict=True):
        for cur_row, old_row in zip(cur_sub, old_sub, strict=True):
            for new_val, old_val in zip(cur_row, old_row, strict=True):
                if new_val != "" and new_val != old_val:
                    return False
    return True


def _clear_rect(sheet: Any, row: int, col: int, rows: int, cols: int) -> bool:
    """Clear spilled cells in the rectangle, leaving the origin formula cell untouched.

    Returns True only when every targeted sub-range is empty afterwards (a protected
    sheet ignores ``clearContents`` without raising, ``ScCellRangesBase::clearContents``).
    """
    cleared = True
    for c1, r1, c2, r2 in _neighbour_coords(row, col, rows, cols):
        try:
            target = sheet.getCellRangeByPosition(c1, r1, c2, r2)
            target.clearContents(SPILL_CLEAR_FLAGS)
            if target.queryContentCells(SPILL_CONTENT_FLAGS).getRangeAddresses():
                cleared = False
        except Exception:
            # The record can name cells past the edge of a file saved with a smaller sheet.
            log.debug("_clear_rect: failed to clear spilled range", exc_info=True)
            cleared = False
    return cleared


def _classify_orphan_record(
    unchanged: bool,
    origin_empty: bool,
    formula_str: str,
    matches: list[tuple[Any, int, int]],
    frow: int,
    fcol: int,
    delta: tuple[int, int] | None,
) -> tuple[str, tuple[int, int] | None]:
    """Decide how cleanup_orphans handles a record whose origin formula no longer matches.

    Returns (action, new_coords) where action is one of:
    - "remap": exactly one match on the sheet and the used-area end shifted by the same delta
      (insert/delete of rows or columns before the block).
    - "keep": origin still holds a =PY formula, old code is absent elsewhere, the used-area
      end grew without shrinking (delta[0] >= 0, delta[1] >= 0, delta != (0, 0)), and the
      neighbour rectangle is unchanged, which covers an in-place code edit in the same
      debounce window as added cells; row/column deletes give a negative component and drop
      so a shifted-in =PY formula never inherits the old rectangle.
    - "drop_and_clear": zero matches on the sheet, the neighbour rectangle is unchanged
      (remembered this session and no non-empty neighbour changed), used-area end is unchanged
      (delta == (0, 0)), and the origin is now empty or a different =PY formula (not overwritten
      with a constant or non-PY formula).
    - "drop": everything else (cut/paste, sort, duplicate formula, stale at open, table pasted
      over the block, Delete Cells pulling other cells into the origin, constant over origin, or
      row deleted through the formula); drop the record and clear nothing.
    """
    if len(matches) == 1:
        _cell, new_row, new_col = matches[0]
        if delta == (new_row - frow, new_col - fcol):
            return ("remap", (new_row, new_col))
        return ("drop", None)
    if len(matches) > 1:
        return ("drop", None)
    origin_is_py = is_py_formula_text(formula_str)
    grew = origin_is_py and delta is not None and delta[0] >= 0 and delta[1] >= 0 and delta != (0, 0)
    emptied_or_edited = delta == (0, 0) and (origin_empty or origin_is_py)
    if (grew or emptied_or_edited) and unchanged:
        return ("keep" if grew else "drop_and_clear", None)
    return ("drop", None)


def _used_end_delta(state: WorkbookState, sheet: Any, sheet_name: str) -> tuple[int, int] | None:
    prev = state.used_ends.get(sheet_name)
    if prev is None:
        return None
    cur = _used_end(sheet)
    return (cur[0] - prev[0], cur[1] - prev[1])


def cleanup_orphans(
    state: WorkbookState,
    sheet: Any,
    *,
    doc: Any,
    sheet_name: str,
) -> None:
    """Orphaned-spill cleanup: remap moved formula cells or clear deleted spill rectangles."""
    try:
        if not main.is_current():
            return

        with sheet_modify_scope():
            delta = _used_end_delta(state, sheet, sheet_name)
            to_remove: list[tuple[tuple[str, int, int], bool]] = []
            to_remap: list[tuple[tuple[str, int, int], tuple[str, int, int]]] = []
            for (s_name, frow, fcol), record in list(state.spills.items()):
                if s_name != sheet_name:
                    continue
                try:
                    if _origin_matches_record(sheet, frow, fcol, record):
                        record.values = _rect_values(sheet, frow, fcol, record.rows, record.cols)
                        continue

                    cell = sheet.getCellByPosition(fcol, frow)
                    formula = cell.getFormula()
                    formula_str = str(formula) if formula else ""
                    matches = collect_matching_formula_origins(sheet, record.code, doc=doc) if record.code else []
                    action, new_coords = _classify_orphan_record(
                        _rect_unchanged(sheet, frow, fcol, record),
                        cell.getType() == EMPTY,
                        formula_str,
                        matches,
                        frow,
                        fcol,
                        delta,
                    )
                    if action == "remap" and new_coords is not None:
                        to_remap.append(((sheet_name, frow, fcol), (sheet_name, new_coords[0], new_coords[1])))
                    elif action in ("drop", "drop_and_clear"):
                        to_remove.append(((sheet_name, frow, fcol), action == "drop_and_clear"))
                except Exception:
                    log.debug("Failed to inspect formula cell %r", (sheet_name, frow, fcol), exc_info=True)

            if to_remove or to_remap:
                with undo_lock(doc):
                    for key, clear in to_remove:
                        removed_rec = state.spills.pop(key, None)
                        if removed_rec is not None and clear:
                            if _user_formula_in_rect(sheet, key[1], key[2], removed_rec.rows, removed_rec.cols) is None:
                                _clear_rect(sheet, key[1], key[2], removed_rec.rows, removed_rec.cols)
                    moved = [
                        (new_k, state.spills.pop(old_k))
                        for old_k, new_k in to_remap
                        if old_k in state.spills
                    ]
                    for new_k, rec in moved:
                        state.spills[new_k] = rec
                    save_spill_registry_for_doc(doc, state)

            state.used_ends[sheet_name] = _used_end(sheet)
    except Exception:
        log.exception("Error in cleanup_orphans")


def load_spill_registry_for_doc(doc: Any, state: WorkbookState) -> None:
    """Load the registry from UserDefinedProperties once per workbook state; later calls do nothing."""
    if state.loaded:
        return
    state.loaded = True
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
                            cells = value.get("cells")
                            if cells:
                                # Registries written before rectangles listed every spilled cell;
                                # the rectangle is their bounding box with the formula cell.
                                rows = max(int(c["r"]) for c in cells) - frow + 1
                                cols = max(int(c["c"]) for c in cells) - fcol + 1
                            else:
                                rows = int(value.get("rows") or 1)
                                cols = int(value.get("cols") or 1)
                            reg_key = (sheet_name, frow, fcol)
                            if reg_key not in state.spills:
                                # Keep any in-memory record written this session (it is newer).
                                state.spills[reg_key] = SpillRecord(
                                    code=code,
                                    rows=max(rows, 1),
                                    cols=max(cols, 1),
                                )
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
            doc_spills[f"{sheet_name}:{frow},{fcol}"] = {
                "code": record.code,
                "rows": record.rows,
                "cols": record.cols,
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


def _spill_token(sheet_name: str, row: int, col: int) -> str:
    return f"spill:{sheet_name}:{row}:{col}"


def _live_origin_sheet(
    doc: Any,
    sheet: Any,
    sheet_name: str,
    formula_row: int,
    formula_col: int,
    code: str,
) -> tuple[Any, str] | None:
    """Resolve the current sheet (following renames) and verify the origin still matches ``code``."""
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
        return None
    if live_sheet_name and live_sheet_name != sheet_name:
        try:
            migrate_sheet_rename(doc, sheet_name, live_sheet_name)
        except Exception:
            log.debug("_live_origin_sheet: rename migration failed", exc_info=True)
        sheet_name = live_sheet_name

    try:
        origin_cell = sheet.getCellByPosition(formula_col, formula_row)
        if not is_matching_py_formula(origin_cell.getFormula(), code):
            return None
    except Exception:
        log.debug("_live_origin_sheet: origin formula check failed", exc_info=True)
        return None
    return (sheet, sheet_name)


def _clear_blocked_spill(
    state: WorkbookState,
    doc: Any,
    sheet: Any,
    sheet_name: str,
    row: int,
    col: int,
) -> None:
    """Clear owned neighbor cells when a spill is blocked and shrink its record to 1 x 1.

    Excel empties a blocked spill's range; a user formula inside keeps the cells and
    the record. Owned cells skip the collision check, so shrinking prevents values typed
    into the emptied cells later from being overwritten silently. Ownership is kept if
    clearing fails or content remains (for example on a protected sheet).
    Caller must hold ``undo_lock`` and ``sheet_modify_scope``.
    """
    key = (sheet_name, row, col)
    rec = state.spills.get(key)
    if rec is None or rec.rows * rec.cols <= 1:
        return
    # A shifted-in =PY formula must not clear shifted user data under another formula's old record
    # before cleanup_orphans runs.
    if not _origin_matches_record(sheet, row, col, rec):
        return
    if _user_formula_in_rect(sheet, row, col, rec.rows, rec.cols) is not None:
        return
    before = _used_end(sheet)
    if _clear_rect(sheet, row, col, rec.rows, rec.cols):
        state.spills[key] = SpillRecord(code=rec.code, rows=1, cols=1)
        save_spill_registry_for_doc(doc, state)
    _advance_used_end(state, sheet, sheet_name, before)


def _release_blocked_spill(
    sheet_name: str,
    formula_row: int,
    formula_col: int,
    *,
    doc: Any,
    sheet: Any,
    code: str,
    key: str,
) -> None:
    """Deferred UI-thread release for a spill that was blocked during interpretation."""
    try:
        if not main.is_current():
            return
        state = WorkbookState.for_doc(doc)
        if state is None:
            return
        with undo_lock(doc):
            if not state.scheduled_key_matches(key):
                return
            live = _live_origin_sheet(doc, sheet, sheet_name, formula_row, formula_col, code)
            if live is None:
                return
            live_sheet, live_name = live
            with sheet_modify_scope():
                _clear_blocked_spill(state, doc, live_sheet, live_name, formula_row, formula_col)
    except Exception:
        log.exception("Error in _release_blocked_spill")


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

    Any moved or non-matching record the modify pass has not handled yet is remapped, cleared, or
    dropped first, so the recheck uses the right rectangle.
    After rechecking that targets are still free, this clears old cells, classifies the grid into ``SpillValue`` records,
    writes neighbour values/errors and updates the spill registry, then applies
    standard temporal number formats across coalesced blocks. If the recheck finds a
    collision, owned neighbor cells are cleared and the record shrinks to 1 x 1.

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

            live = _live_origin_sheet(doc, sheet, sheet_name, formula_row, formula_col, code)
            if live is None:
                return
            sheet, sheet_name = live

            reg_key = (sheet_name, formula_row, formula_col)

            with sheet_modify_scope():
                # The modify pass may still be waiting: remap any moved record and clear or drop any
                # record whose formula is no longer at this origin before this write decides what it owns.
                previous_record = state.spills.get(reg_key)
                foreign = previous_record is not None and not _origin_matches_record(
                    sheet, formula_row, formula_col, previous_record
                )
                if foreign or _moved_record(state, sheet, sheet_name, formula_row, formula_col) is not None:
                    cleanup_orphans(state, sheet, doc=doc, sheet_name=sheet_name)
                before = _used_end(sheet)
                previous_record = state.spills.get(reg_key)
                num_rows = len(grid)
                num_cols = max(len(row) for row in grid) if num_rows > 0 else 0
                owned = (previous_record.rows, previous_record.cols) if previous_record is not None else (0, 0)
                if _spill_is_blocked(doc, code, sheet, sheet_name, formula_row, formula_col, num_rows, num_cols, owned):
                    _clear_blocked_spill(state, doc, sheet, sheet_name, formula_row, formula_col)
                    return
                if previous_record is not None:
                    _clear_rect(sheet, formula_row, formula_col, previous_record.rows, previous_record.cols)

                if num_rows == 0 or num_cols == 0:
                    state.spills[reg_key] = SpillRecord(code=code, rows=1, cols=1)
                    save_spill_registry_for_doc(doc, state)
                    _advance_used_end(state, sheet, sheet_name, before)
                    return

                settings = doc.getNumberFormatSettings()
                nd = settings.getPropertyValue("NullDate")
                null_dt = datetime.date(nd.Year, nd.Month, nd.Day)

                values = _classify_spill_grid(grid, num_cols, null_dt)
                _write_spill_values(sheet, formula_row, formula_col, values)
                state.spills[reg_key] = SpillRecord(
                    code=code,
                    rows=num_rows,
                    cols=num_cols,
                    values=_rect_values(sheet, formula_row, formula_col, num_rows, num_cols),
                )
                save_spill_registry_for_doc(doc, state)
                _apply_spill_temporal_formats(doc, sheet, formula_row, formula_col, values)
                _advance_used_end(state, sheet, sheet_name, before)

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
    *,
    in_previous_rect: bool = False,
) -> None:
    """Record why a =PY spill was blocked in the diagnostics store."""
    if blocker is not None and in_previous_rect:
        msg = (
            f"#SPILL!: {format_address(blocker[0], blocker[1])} inside the previous spill "
            "contains a formula; clear or move it so the spill can overwrite the range."
        )
    elif blocker is not None:
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
    owned: tuple[int, int],
) -> bool:
    """Return True and record diagnostics if any target cell is out of bounds, occupied outside ``owned``, or holds a user formula."""
    owned_rows, owned_cols = owned
    if owned_rows * owned_cols > 1:
        blocker = _user_formula_in_rect(sheet, row, col, owned_rows, owned_cols)
        if blocker is not None:
            _record_spill_blocked(
                doc=doc,
                code=code,
                sheet_name=sheet_name,
                row=row,
                col=col,
                num_rows=num_rows,
                num_cols=num_cols,
                blocker=blocker,
                in_previous_rect=True,
            )
            return True

    if num_rows * num_cols <= 1:
        return False

    if row + num_rows > int(sheet.getRows().getCount()) or col + num_cols > int(sheet.getColumns().getCount()):
        _record_spill_blocked(doc, code, sheet_name, row, col, num_rows, num_cols, None)
        return True

    # The growth is two strips around the part already owned (at least the formula cell): right of
    # it, then below it. They follow row-major order, so the first strip with content holds the
    # first blocker. queryContentCells reads cell types only (ScCellRangesBase::queryContentCells,
    # sc/source/ui/unoobj/cellsuno.cxx); reading values here would interpret formulas that may
    # depend on the =PY() cell being recalculated.
    kept_rows = min(max(owned_rows, 1), num_rows)
    kept_cols = min(max(owned_cols, 1), num_cols)
    strips: list[tuple[int, int, int, int]] = []
    if kept_cols < num_cols:
        strips.append((col + kept_cols, row, col + num_cols - 1, row + kept_rows - 1))
    if kept_rows < num_rows:
        strips.append((col, row + kept_rows, col + num_cols - 1, row + num_rows - 1))
    for strip in strips:
        occupied = sheet.getCellRangeByPosition(*strip).queryContentCells(SPILL_CONTENT_FLAGS).getRangeAddresses()
        if occupied:
            blocker_row, blocker_col = min((int(a.StartRow), int(a.StartColumn)) for a in occupied)
            log.debug("Spill: collision at %r", (blocker_row, blocker_col))
            _record_spill_blocked(doc, code, sheet_name, row, col, num_rows, num_cols, (blocker_col, blocker_row))
            return True
    return False


def _origin_matches_record(sheet: Any, frow: int, fcol: int, record: SpillRecord) -> bool:
    formula = str(sheet.getCellByPosition(fcol, frow).getFormula() or "")
    return is_matching_py_formula(formula, record.code) if record.code else is_py_formula_text(formula)


def _moved_record(state: WorkbookState, sheet: Any, sheet_name: str, row: int, col: int) -> SpillRecord | None:
    """Return this cell's record when rows or columns before it were inserted or deleted and
    cleanup_orphans has not remapped it yet; None when the cell already has its own record.

    Inserting a row inside a data range recalculates =PY() at once, 0.1 s before the modify pass
    remaps. The record is the one the used-area delta back from this cell (the witness
    _classify_orphan_record uses). Reads only, because it also runs during interpretation.
    """
    record = state.spills.get((sheet_name, row, col))
    if record is not None and _origin_matches_record(sheet, row, col, record):
        return None
    delta = _used_end_delta(state, sheet, sheet_name)
    if delta is None or delta == (0, 0):
        return None
    moved = state.spills.get((sheet_name, row - delta[0], col - delta[1]))
    # cleanup_orphans never remaps a record without code.
    if moved is None or not moved.code or not _origin_matches_record(sheet, row, col, moved):
        return None
    return moved


def arm_spill_registry(ctx: Any, doc: Any, state: WorkbookState) -> None:
    """Load the document's spill registry and arm modify listeners on sheets with records (UI thread)."""
    load_spill_registry_for_doc(doc, state)
    if not state.spills:
        return
    sheets = doc.getSheets()
    armed: dict[str, Any] = {}
    for (sheet_name, frow, fcol), record in state.spills.items():
        if sheet_name not in armed:
            sheet = uno_read(lambda: sheets.getByName(sheet_name), None)
            armed[sheet_name] = sheet
            if sheet is not None:
                _arm_sheet(ctx, doc, state, sheet, sheet_name)
        else:
            sheet = armed[sheet_name]
        if sheet is not None and uno_read(lambda: _origin_matches_record(sheet, frow, fcol, record), False):
            record.values = _rect_values(sheet, frow, fcol, record.rows, record.cols)


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
    diagnostics store, which the LibrePy sidebar and ``write_range`` read, and
    scheduling a deferred clear of owned neighbor cells), ``PreparedSpill`` when
    the neighbor write should proceed, or ``None`` when ``state.key`` is empty.
    When rows or columns before a formula were inserted or deleted, it uses its unmoved record's
    rectangle; the deferred write remaps and saves.
    """
    if not state.key:
        return None
    sheet = origin.sheet
    formula_row = origin.row
    formula_col = origin.col
    sheet_name = origin.sheet_name
    log.debug("Spill: checking formula cell at %r on sheet %r for code %r", (formula_row, formula_col), sheet_name, code)

    load_spill_registry_for_doc(doc, state)

    try:
        _arm_sheet(ctx, doc, state, sheet, sheet_name)
    except Exception:
        log.exception("Failed to register modify listener on sheet")

    num_rows = len(grid_to_spill)
    num_cols = max(len(row) for row in grid_to_spill) if num_rows > 0 else 0
    reg_key = (sheet_name, formula_row, formula_col)
    previous_record = _moved_record(state, sheet, sheet_name, formula_row, formula_col) or state.spills.get(reg_key)
    owned = (previous_record.rows, previous_record.cols) if previous_record is not None else (0, 0)

    log.debug("Spill: owned rect for cell %r: %r", reg_key, owned)

    if _spill_is_blocked(doc, code, sheet, sheet_name, formula_row, formula_col, num_rows, num_cols, owned):
        # Cells cannot be written during interpretation; scheduling on the shared token
        # replaces any pending write for this origin (even when no record exists yet) and
        # lets a later fitting recalculation replace or cancel the release.
        key = state.key
        state.debounce.schedule(
            _spill_token(sheet_name, formula_row, formula_col),
            0.1,
            lambda: _release_blocked_spill(
                sheet_name,
                formula_row,
                formula_col,
                doc=doc,
                sheet=sheet,
                code=code,
                key=key,
            ),
        )
        return "#SPILL!"
    return PreparedSpill(sheet_name, formula_row, formula_col, sheet=sheet, key=state.key, code=code)


def queue_deferred_spill_write(state: WorkbookState, doc: Any, prepared: PreparedSpill, grid_to_spill: list[list[Any]]) -> None:
    """Post neighbor writes to the UI thread after the usual 0.1s settle."""
    token = _spill_token(prepared.sheet_name, prepared.formula_row, prepared.formula_col)

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
    late to change the add-in return; on collision, the cell keeps its own value
    while ``prepare_auto_spill`` still schedules clearing old neighbor cells.
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

        # Cancel any pending main-path release or older write for this origin before writing
        # synchronously so an earlier release cannot fire later and wipe the fresh spill.
        state.debounce.cancel(_spill_token(prepared.sheet_name, prepared.formula_row, prepared.formula_col))
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
