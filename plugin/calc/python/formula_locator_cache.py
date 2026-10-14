# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Formula cell location for Calc =PY() formulas with array-formula origin detection."""

from __future__ import annotations

import logging
import re
from typing import Any

from plugin.framework.errors import is_disposed_exception

log = logging.getLogger(__name__)

# Handles Calc-escaped double-quotes ("") inside string literals and optional add-in
# OriginalNames (WriterAgent / LibrePy, or Collabora GETPY before open-rewrite).
_PY_FORMULA_CODE_REGEX = re.compile(
    r"^\{?\s*=?\s*"
    r"(?:ORG\.(?:EXTENSION\.[A-Z0-9_.]+|COLLABORAOFFICE\.SHEET\.ADDIN\.PYTHONCOMPUTEFUNCTIONS)\.)?"
    r'(?:GETPYTHON|GETPY|PYTHON|PY)\s*\(\s*"((?:[^"]|"")*)"',
    re.IGNORECASE,
)


def extract_code_from_py_formula(formula: str) -> str | None:
    """Extract and unescape the Python code string argument from a =PY() / =PYTHON() formula."""
    if not formula:
        return None
    match = _PY_FORMULA_CODE_REGEX.match(formula)
    if match:
        raw_code = match.group(1)
        return raw_code.replace('""', '"')
    return None


def _code_strings_match(extracted: str | None, code_str: str) -> bool:
    if extracted is None or not code_str:
        return False
    if extracted == code_str:
        return True
    ext_norm = extracted.replace("\r\n", "\n").strip()
    code_norm = code_str.replace("\r\n", "\n").strip()
    return ext_norm == code_norm


def _resolve_ref_cell(sheet: Any, doc: Any | None, code_ref: str) -> Any | None:
    try:
        from plugin.calc.address_utils import parse_address, split_sheet_prefix

        sheet_prefix, bare = split_sheet_prefix(code_ref.strip())
        bare = bare.replace("$", "").strip()
        if not bare:
            return None
        col, row = parse_address(bare)
        target_sheet = sheet
        if sheet_prefix:
            if hasattr(sheet, "getName") and sheet.getName() == sheet_prefix:
                target_sheet = sheet
            elif doc is not None and hasattr(doc, "getSheets"):
                sheets = doc.getSheets()
                if hasattr(sheets, "hasByName") and sheets.hasByName(sheet_prefix):
                    target_sheet = sheets.getByName(sheet_prefix)
                elif hasattr(sheets, "getByName"):
                    try:
                        target_sheet = sheets.getByName(sheet_prefix)
                    except Exception:
                        return None
                else:
                    return None
            else:
                return None
        return target_sheet.getCellByPosition(col, row)
    except Exception:
        return None


def is_matching_py_formula(formula: str, code_str: str, *, sheet: Any | None = None, doc: Any | None = None) -> bool:
    """True if formula is a =PY() / =PYTHON() call whose code argument equals code_str."""
    if not formula or not code_str:
        return False
    upper = formula.upper()
    if "PY" not in upper:
        return False
    extracted = extract_code_from_py_formula(formula)
    if extracted is not None:
        return _code_strings_match(extracted, code_str)
    if sheet is not None:
        try:
            from plugin.calc.python.formula_edit import parse_python_formula, py_formula_has_unquoted_code_ref

            if py_formula_has_unquoted_code_ref(formula):
                parts = parse_python_formula(formula)
                if parts is not None and parts.code:
                    ref_cell = _resolve_ref_cell(sheet, doc, parts.code)
                    if ref_cell is not None:
                        cell_text = getattr(ref_cell, "getString", lambda: "")()
                        return _code_strings_match(cell_text, code_str)
        except Exception:
            pass
    return False


def _array_block_origin(sheet: Any, col: int, row: int) -> tuple[int, int] | None:
    """Return top-left (row, col) if cell is part of a multi-cell array formula block.

    Matches the cursor check in plugin/calc/manipulator.py _array_block.
    collapseToCurrentArray() on an ordinary cell collapses to that cell
    (End == Start); the getArrayFormula() check is what stops a fill-down
    from being treated as an array formula.
    """
    try:
        if not hasattr(sheet, "createCursorByRange") or not hasattr(sheet, "getCellRangeByPosition"):
            return None
        cursor = sheet.createCursorByRange(sheet.getCellRangeByPosition(col, row, col, row))
        if not hasattr(cursor, "collapseToCurrentArray"):
            return None
        cursor.collapseToCurrentArray()
        addr = cursor.getRangeAddress()
        start_col = getattr(addr, "StartColumn", None)
        end_col = getattr(addr, "EndColumn", None)
        start_row = getattr(addr, "StartRow", None)
        end_row = getattr(addr, "EndRow", None)
        if start_col is None or end_col is None or start_row is None or end_row is None:
            return None
        if start_col == end_col and start_row == end_row:
            return None
        rng = sheet.getCellRangeByPosition(start_col, start_row, end_col, end_row)
        get_arr = getattr(rng, "getArrayFormula", None)
        if not get_arr or not get_arr():
            return None
        return (start_row, start_col)
    except Exception as e:
        if is_disposed_exception(e):
            raise
        return None


def _matching_cells_in_range(sheet: Any, cell_range: Any, code_str: str, doc: Any | None = None) -> list[tuple[Any, int, int]]:
    """Matching formula cells in *cell_range*. A multi-cell array formula of the same code is one origin."""
    addr = cell_range.getRangeAddress()
    raw_hits: list[tuple[Any, int, int]] = []

    formula_array: Any = None
    if hasattr(cell_range, "getFormulaArray"):
        try:
            formula_array = cell_range.getFormulaArray()
        except Exception:
            formula_array = None

    if formula_array is not None and isinstance(formula_array, (list, tuple)):
        for r_offset, row_formulas in enumerate(formula_array):
            r = addr.StartRow + r_offset
            for c_offset, formula in enumerate(row_formulas):
                c = addr.StartColumn + c_offset
                if not formula:
                    continue
                formula_str = str(formula)
                upper = formula_str.upper()
                if "PY" not in upper:
                    continue
                if is_matching_py_formula(formula_str, code_str, sheet=sheet, doc=doc):
                    cell = sheet.getCellByPosition(c, r)
                    raw_hits.append((cell, r, c))
    else:
        for r in range(addr.StartRow, addr.EndRow + 1):
            for c in range(addr.StartColumn, addr.EndColumn + 1):
                cell = sheet.getCellByPosition(c, r)
                formula = cell.getFormula()
                if not formula:
                    continue
                upper = formula.upper()
                if "PY" not in upper:
                    continue
                if is_matching_py_formula(formula, code_str, sheet=sheet, doc=doc):
                    raw_hits.append((cell, r, c))

    if len(raw_hits) <= 1:
        return raw_hits

    hits: list[tuple[Any, int, int]] = []
    seen_array_origins: set[tuple[int, int]] = set()
    for cell, r, c in raw_hits:
        origin = _array_block_origin(sheet, c, r)
        if origin is not None:
            if origin not in seen_array_origins:
                seen_array_origins.add(origin)
                origin_r, origin_c = origin
                origin_cell = cell if (origin_r == r and origin_c == c) else sheet.getCellByPosition(origin_c, origin_r)
                hits.append((origin_cell, origin_r, origin_c))
        else:
            hits.append((cell, r, c))
    return hits


def search_sheet_for_formula(sheet: Any, code_str: str, doc: Any | None = None) -> tuple[Any, int, int] | None:
    """Query formula cells on sheet. Returns a match only when exactly one origin matches.

    Duplicate ``=PY("same")`` cells are indistinguishable (XAddIn has no calling cell);
    first-match would spill into the wrong formula. Matrix/array formulas are one origin
    (top-left of the formula range).
    """
    matches = collect_matching_formula_origins(sheet, code_str, doc=doc)
    if len(matches) == 1:
        return matches[0]
    return None


def collect_matching_formula_origins(sheet: Any, code_str: str, doc: Any | None = None) -> list[tuple[Any, int, int]]:
    """All matching formula origins on *sheet* (one per queryContentCells range)."""
    found: list[tuple[Any, int, int]] = []
    try:
        # com.sun.star.sheet.CellFlags.FORMULA = 16
        formula_cells = sheet.queryContentCells(16)
        if formula_cells is None:
            return found
        count = formula_cells.getCount() if hasattr(formula_cells, "getCount") else 0
        for i in range(count):
            cell_range = formula_cells.getByIndex(i)
            found.extend(_matching_cells_in_range(sheet, cell_range, code_str, doc=doc))
    except Exception:
        log.debug("search_sheet_for_formula failed on sheet", exc_info=True)
    return found


def locate_formula_cell_in_doc(ctx: Any, doc: Any, code_str: str) -> tuple[Any, Any, tuple[int, int]] | None:
    """Find unique (sheet, cell, (row, col)) for this Python formula in *doc*.

    Returns None when zero or two+ origins match: XAddIn has no calling cell, so
    spilling or caching from the first hit writes the wrong formula.
    """
    if doc is None:
        return None

    collected: list[tuple[Any, Any, tuple[int, int]]] = []
    seen: set[tuple[str, int, int]] = set()

    def _add(sheet: Any, cell: Any, row: int, col: int) -> None:
        try:
            name = sheet.getName() if hasattr(sheet, "getName") else ""
        except Exception:
            name = ""
        key = (name, row, col)
        if key in seen:
            return
        seen.add(key)
        collected.append((sheet, cell, (row, col)))

    try:
        sheets = doc.getSheets() if hasattr(doc, "getSheets") else None
        if sheets is not None:
            count = sheets.getCount() if hasattr(sheets, "getCount") else 0
            for i in range(count):
                sheet = sheets.getByIndex(i)
                for cell, r, c in collect_matching_formula_origins(sheet, code_str, doc=doc):
                    _add(sheet, cell, r, c)
                    if len(collected) > 1:
                        return None
    except Exception:
        log.debug("locate_formula_cell_in_doc failed across sheets", exc_info=True)
        return None

    if len(collected) != 1:
        return None
    return collected[0]


def locate_formula_cell(ctx: Any, sheet: Any, code_str: str) -> tuple[int, int] | None:
    """Find (row, col) containing the Python formula on sheet."""
    try:
        from plugin.framework.thread_guard import on_main_thread

        if not on_main_thread() or not (hasattr(ctx, "ServiceManager") or hasattr(ctx, "getServiceManager")):
            return None
        # The sheet's own spreadsheet, not the front window.
        from plugin.calc.python.sheet_modify import _owning_calc_doc

        doc = _owning_calc_doc(sheet)
        if doc is not None:
            located = locate_formula_cell_in_doc(ctx, doc, code_str)
            if located is not None:
                found_sheet, _, (r, c) = located
                if found_sheet == sheet or (hasattr(found_sheet, "getName") and hasattr(sheet, "getName") and found_sheet.getName() == sheet.getName()):
                    return (r, c)
    except Exception:
        log.debug("locate_formula_cell failed", exc_info=True)

    # Fallback to direct sheet search
    res = search_sheet_for_formula(sheet, code_str)
    if res is not None:
        _, r, c = res
        return (r, c)

    return None
