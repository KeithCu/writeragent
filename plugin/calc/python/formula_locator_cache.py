# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Formula cell location for Calc =PY() formulas with array-formula origin detection."""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
import re
from typing import Any, Iterator

from plugin.framework.errors import uno_probe

CELL_FLAG_FORMULA = 16  # com.sun.star.sheet.CellFlags.FORMULA

_LOCATE_MEMO: contextvars.ContextVar[dict[tuple[int, str], tuple[Any, Any, tuple[int, int]] | None] | None] = contextvars.ContextVar("_LOCATE_MEMO", default=None)


@contextmanager
def bind_formula_locate_memo() -> Iterator[None]:
    """Bind a fresh per-call formula location memo dictionary."""
    token = _LOCATE_MEMO.set({})
    try:
        yield
    finally:
        _LOCATE_MEMO.reset(token)

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
                    target_sheet = uno_probe(lambda: sheets.getByName(sheet_prefix), None)
                    if target_sheet is None:
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
        from plugin.calc.python.formula_edit import parse_python_formula, py_formula_has_unquoted_code_ref

        if py_formula_has_unquoted_code_ref(formula):
            parts = parse_python_formula(formula)
            if parts is not None and parts.code:
                ref_cell = _resolve_ref_cell(sheet, doc, parts.code)
                if ref_cell is not None:
                    cell_text = uno_probe(ref_cell.getString, "") if hasattr(ref_cell, "getString") else ""
                    return _code_strings_match(cell_text, code_str)
    return False


def iter_range_formula_texts(sheet: Any, cell_range: Any, *, skip_cell_errors: bool = False) -> Iterator[tuple[int, int, str]]:
    """Yield (row, col, formula_text). getFormulas() runs before the first yield.
    Fallback getFormula() errors propagate unless skip_cell_errors."""
    addr = cell_range.getRangeAddress()
    formula_matrix = cell_range.getFormulas() if hasattr(cell_range, "getFormulas") else None

    def _gen() -> Iterator[tuple[int, int, str]]:
        if formula_matrix is not None and len(formula_matrix) > 0:
            for r_idx, row_formulas in enumerate(formula_matrix):
                row = addr.StartRow + r_idx
                for c_idx, formula in enumerate(row_formulas):
                    yield (row, addr.StartColumn + c_idx, str(formula or ""))
        else:
            for row in range(addr.StartRow, addr.EndRow + 1):
                for col in range(addr.StartColumn, addr.EndColumn + 1):
                    try:
                        cell = sheet.getCellByPosition(col, row)
                        formula = str(cell.getFormula() or "")
                    except Exception:
                        if skip_cell_errors:
                            continue
                        raise
                    yield (row, col, formula)

    return _gen()


def _array_block_origin(sheet: Any, col: int, row: int) -> tuple[int, int] | None:
    """Return top-left (row, col) if cell is part of a multi-cell array formula block.

    Matches the cursor check in plugin/calc/manipulator.py _array_block.
    collapseToCurrentArray() on an ordinary cell collapses to that cell
    (End == Start); the getArrayFormula() check is what stops a fill-down
    from being treated as an array formula.
    """
    def _probe() -> tuple[int, int] | None:
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

    return uno_probe(_probe, None)


def _matching_cells_in_range(sheet: Any, cell_range: Any, code_str: str, doc: Any | None = None) -> list[tuple[Any, int, int]]:
    """Matching formula cells in *cell_range*. A multi-cell array formula of the same code is one origin."""
    addr = cell_range.getRangeAddress()
    raw_hits: list[tuple[Any, int, int]] = []

    formula_array: Any = uno_probe(cell_range.getFormulaArray, None) if hasattr(cell_range, "getFormulaArray") else None

    def _candidates() -> Iterator[tuple[str, int, int]]:
        if formula_array is not None and isinstance(formula_array, (list, tuple)):
            for r_offset, row_formulas in enumerate(formula_array):
                r = addr.StartRow + r_offset
                for c_offset, formula in enumerate(row_formulas):
                    yield (str(formula or ""), r, addr.StartColumn + c_offset)
        else:
            for r in range(addr.StartRow, addr.EndRow + 1):
                for c in range(addr.StartColumn, addr.EndColumn + 1):
                    cell = sheet.getCellByPosition(c, r)
                    yield (str(cell.getFormula() or ""), r, c)

    for formula_str, r, c in _candidates():
        if not formula_str:
            continue
        upper = formula_str.upper()
        if "PY" not in upper:
            continue
        if is_matching_py_formula(formula_str, code_str, sheet=sheet, doc=doc):
            cell = sheet.getCellByPosition(c, r)
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
    formula_cells = sheet.queryContentCells(CELL_FLAG_FORMULA)
    if formula_cells is None:
        return found
    count = formula_cells.getCount() if hasattr(formula_cells, "getCount") else 0
    for i in range(count):
        cell_range = formula_cells.getByIndex(i)
        found.extend(_matching_cells_in_range(sheet, cell_range, code_str, doc=doc))
    return found


def locate_formula_cell_in_doc(ctx: Any, doc: Any, code_str: str) -> tuple[Any, Any, tuple[int, int]] | None:
    """Find unique (sheet, cell, (row, col)) for this Python formula in *doc*.

    Returns None when zero or two+ origins match: XAddIn has no calling cell, so
    spilling or caching from the first hit writes the wrong formula.
    """
    if doc is None:
        return None

    memo = _LOCATE_MEMO.get()
    cache_key = (id(doc), code_str) if memo is not None else None
    if memo is not None and cache_key in memo:
        return memo[cache_key]

    collected: list[tuple[Any, Any, tuple[int, int]]] = []
    seen: set[tuple[str, int, int]] = set()

    def _add(sheet: Any, cell: Any, row: int, col: int) -> None:
        name = sheet.getName() if hasattr(sheet, "getName") else ""
        key = (name, row, col)
        if key in seen:
            return
        seen.add(key)
        collected.append((sheet, cell, (row, col)))

    res: tuple[Any, Any, tuple[int, int]] | None = None
    sheets = doc.getSheets() if hasattr(doc, "getSheets") else None
    if sheets is not None:
        count = sheets.getCount() if hasattr(sheets, "getCount") else 0
        for i in range(count):
            sheet = sheets.getByIndex(i)
            for cell, r, c in collect_matching_formula_origins(sheet, code_str, doc=doc):
                _add(sheet, cell, r, c)
                if len(collected) > 1:
                    if memo is not None and cache_key is not None:
                        memo[cache_key] = None
                    return None

    if len(collected) == 1:
        res = collected[0]
    if memo is not None and cache_key is not None:
        memo[cache_key] = res
    return res
