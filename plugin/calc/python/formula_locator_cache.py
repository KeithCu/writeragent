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

from plugin.calc.python.formula_edit import parse_python_formula, py_formula_has_unquoted_code_ref
from plugin.framework.errors import uno_probe

CELL_FLAG_FORMULA = 16  # com.sun.star.sheet.CellFlags.FORMULA

_Location = tuple[Any, Any, tuple[int, int]]
_LocateMemo = dict[tuple[int, str], _Location | None]

# One memo per add-in execution (bind_formula_locate_memo); a None value means no unique origin.
_LOCATE_MEMO: contextvars.ContextVar[_LocateMemo | None] = contextvars.ContextVar("_LOCATE_MEMO", default=None)


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
    if extracted is None:
        return False
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
        if sheet_prefix and sheet.getName() != sheet_prefix:
            if doc is None:
                return None
            sheets = doc.getSheets()
            target_sheet = uno_probe(lambda: sheets.getByName(sheet_prefix), None)
            if target_sheet is None:
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
        if py_formula_has_unquoted_code_ref(formula):
            parts = parse_python_formula(formula)
            if parts is not None and parts.code:
                ref_cell = _resolve_ref_cell(sheet, doc, parts.code)
                if ref_cell is not None:
                    cell_text = uno_probe(ref_cell.getString, "") if hasattr(ref_cell, "getString") else ""
                    return _code_strings_match(cell_text, code_str)
    return False


def iter_range_formula_texts(sheet: Any, cell_range: Any, *, skip_cell_errors: bool = False) -> Iterator[tuple[int, int, str]]:
    """Yield (row, col, formula_text). getFormulaArray() runs before the first yield.
    Fallback getFormula() errors propagate unless skip_cell_errors."""
    addr = cell_range.getRangeAddress()
    formula_matrix = uno_probe(cell_range.getFormulaArray, None) if hasattr(cell_range, "getFormulaArray") else None
    if isinstance(formula_matrix, (list, tuple)) and len(formula_matrix) > 0:
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


def _array_block_origin(sheet: Any, col: int, row: int) -> tuple[int, int] | None:
    """Return top-left (row, col) if cell is part of a multi-cell array formula block.

    Matches the cursor check in plugin/calc/manipulator.py _array_block.
    collapseToCurrentArray() on an ordinary cell collapses to that cell
    (End == Start); the getArrayFormula() check is what stops a fill-down
    from being treated as an array formula.
    """
    def _probe() -> tuple[int, int] | None:
        cursor = sheet.createCursorByRange(sheet.getCellRangeByPosition(col, row, col, row))
        cursor.collapseToCurrentArray()
        addr = cursor.getRangeAddress()
        if addr.StartColumn == addr.EndColumn and addr.StartRow == addr.EndRow:
            return None
        rng = sheet.getCellRangeByPosition(addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow)
        if not rng.getArrayFormula():
            return None
        return (addr.StartRow, addr.StartColumn)

    return uno_probe(_probe, None)


def search_sheet_for_formula(sheet: Any, code_str: str, doc: Any | None = None) -> tuple[Any, int, int] | None:
    """Query formula cells on sheet. Returns a match only when exactly one origin matches.

    Duplicate ``=PY("same")`` cells are indistinguishable (XAddIn has no calling cell);
    first-match would spill into the wrong formula. Matrix/array formulas are one origin
    (top-left of the formula range).
    """
    matches = _collect_matching_formula_origins(sheet, code_str, doc=doc)
    if len(matches) == 1:
        return matches[0]
    return None


def _collect_matching_formula_origins(sheet: Any, code_str: str, doc: Any | None = None) -> list[tuple[Any, int, int]]:
    """All matching formula origins on *sheet* (one per queryContentCells range)."""
    found: list[tuple[Any, int, int]] = []
    formula_cells = sheet.queryContentCells(CELL_FLAG_FORMULA)
    if formula_cells is None:
        return found
    count = formula_cells.getCount() if hasattr(formula_cells, "getCount") else 0
    for i in range(count):
        cell_range = formula_cells.getByIndex(i)
        hits: list[tuple[int, int]] = []
        for r, c, formula_str in iter_range_formula_texts(sheet, cell_range):
            if is_matching_py_formula(formula_str, code_str, sheet=sheet, doc=doc):
                hits.append((r, c))
        if len(hits) <= 1:
            found.extend((sheet.getCellByPosition(c, r), r, c) for r, c in hits)
            continue
        origins = list(dict.fromkeys(_array_block_origin(sheet, c, r) or (r, c) for r, c in hits))
        found.extend((sheet.getCellByPosition(c, r), r, c) for r, c in origins)
    return found


def locate_formula_cell_in_doc(doc: Any, code_str: str) -> _Location | None:
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

    collected: list[_Location] = []
    seen: set[tuple[str, int, int]] = set()
    sheets = doc.getSheets() if hasattr(doc, "getSheets") else None
    if sheets is not None:
        count = sheets.getCount() if hasattr(sheets, "getCount") else 0
        for i in range(count):
            sheet = sheets.getByIndex(i)
            sheet_name = sheet.getName() if hasattr(sheet, "getName") else ""
            for cell, r, c in _collect_matching_formula_origins(sheet, code_str, doc=doc):
                key = (sheet_name, r, c)
                if key in seen:
                    continue
                seen.add(key)
                collected.append((sheet, cell, (r, c)))
                if len(collected) > 1:
                    break
            if len(collected) > 1:
                break

    res = collected[0] if len(collected) == 1 else None
    if memo is not None and cache_key is not None:
        memo[cache_key] = res
    return res
