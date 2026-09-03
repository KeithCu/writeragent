# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Discover ``=PY()`` / ``=PYTHON()`` formula cells on a Calc sheet.

Used by the LibrePy Python sidebar (cell list + click-to-navigate). Handles short
formulas, WriterAgent / LibrePy fully qualified add-in forms, and Collabora
``GETPY`` OriginalNames (prefix-rewritten for parse; open-rewrite evaluates them).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from plugin.calc.address_utils import index_to_column
from plugin.calc.python.formula_edit import (
    cell_looks_python_like,
    extract_python_code_loose,
    normalize_formula_string,
    parse_python_formula,
)

log = logging.getLogger(__name__)

# LibreOffice stores registered add-ins as fully qualified names in getFormula().
# LibrePy still registers as writeragent.PythonFunction (WA/LibrePy file compat).
# Collabora Online stores pythoncompute GETPY — canonicalize via prefix rewrite.
_ADDIN_PY_PREFIX_RE = re.compile(
    r"^=\s*ORG\.EXTENSION\.(?:WRITERAGENT|LIBREPY)\.PYTHONFUNCTION\.(?:PYTHON|PY)\s*\(",
    re.IGNORECASE,
)

# CellFlags.FORMULA = 16
_CELL_FLAG_FORMULA = 16
_MAX_PYTHON_CELLS_FOUND = 100
_MAX_CELLS_TO_SCAN = 50000


@dataclass(frozen=True)
class PythonCellInfo:
    """One discovered Python formula cell."""

    sheet: str
    row: int  # 0-based
    column: int  # 0-based
    address: str  # e.g. "Sheet1.A1"
    code: str
    formula: str


@dataclass(frozen=True)
class PythonSheetDiscovery:
    """Row-major PY cells on one sheet, plus whether the scan stopped early.

    Phase 1 treated ``len(cells) >= 100`` as cap-hit and over-skipped an exact
    100. ``truncated`` is True only when the 100-cell find cap or the 50k
    scan cap stopped discovery before the sheet was fully scanned — a
    completed scan of exactly 100 is ``truncated=False``.
    """

    cells: list[PythonCellInfo]
    truncated: bool


def canonicalize_py_formula_for_parse(formula: str) -> str:
    """Map LO add-in formula text to ``=PYTHON(…)`` for ``parse_python_formula``."""
    from plugin.calc.python.collabora_formula import rewrite_collabora_addin_prefix

    raw = rewrite_collabora_addin_prefix(normalize_formula_string(formula))
    match = _ADDIN_PY_PREFIX_RE.match(raw)
    if match:
        return "=PYTHON(" + raw[match.end() :]
    return raw


def is_py_formula_text(formula: str) -> bool:
    """True when *formula* is PY/PYTHON, including fully qualified add-in form."""
    return cell_looks_python_like(canonicalize_py_formula_for_parse(formula))


def extract_code_from_formula(formula: str) -> str:
    """Best-effort Python source from a PY/PYTHON formula."""
    canonical = canonicalize_py_formula_for_parse(formula)
    parts = parse_python_formula(canonical)
    if parts is not None:
        return parts.code
    return extract_python_code_loose(canonical) or ""


def _cell_address(sheet_name: str, row: int, column: int) -> str:
    return f"{sheet_name}.{index_to_column(column)}{row + 1}"


def list_python_cells_on_sheet(sheet: Any, *, sheet_name: str | None = None) -> list[PythonCellInfo]:
    """Return Python formula cells on *sheet*, sorted by row then column.

    Cap / completeness live on :func:`discover_python_cells_on_sheet` (``truncated``).
    """
    return discover_python_cells_on_sheet(sheet, sheet_name=sheet_name).cells


def discover_python_cells_on_sheet(
    sheet: Any, *, sheet_name: str | None = None
) -> PythonSheetDiscovery:
    """Discover PY cells and report whether the 100 / 50k caps truncated the list.

    After 100 PY cells we keep scanning for one more. Finding #101 (or hitting
    the 50k scan cap) sets ``truncated=True``. An exact 100 that finishes the
    formula-cell walk is complete.
    """
    if sheet is None:
        return PythonSheetDiscovery([], False)
    name = sheet_name
    if not name:
        try:
            name = str(sheet.getName() or "")
        except Exception:
            name = ""
    if not name:
        name = "Sheet"

    found: list[PythonCellInfo] = []
    truncated = False
    try:
        formula_cells = sheet.queryContentCells(_CELL_FLAG_FORMULA)
    except Exception:
        log.debug("list_python_cells_on_sheet: queryContentCells failed", exc_info=True)
        return PythonSheetDiscovery([], False)

    if formula_cells is None:
        return PythonSheetDiscovery([], False)

    try:
        count = int(formula_cells.getCount())
    except Exception:
        return PythonSheetDiscovery([], False)

    scanned_count = 0

    def _consider(formula: str, row: int, col: int) -> bool:
        """Add a PY cell, or mark truncated on the 101st. False means stop."""
        nonlocal truncated
        if not formula or not is_py_formula_text(str(formula)):
            return True
        if len(found) >= _MAX_PYTHON_CELLS_FOUND:
            # 101st PY cell: the returned list of 100 is incomplete.
            truncated = True
            return False
        code = extract_code_from_formula(str(formula))
        found.append(
            PythonCellInfo(
                sheet=name,
                row=row,
                column=col,
                address=_cell_address(name, row, col),
                code=code,
                formula=str(formula),
            )
        )
        return True

    for i in range(count):
        if truncated or scanned_count >= _MAX_CELLS_TO_SCAN:
            if scanned_count >= _MAX_CELLS_TO_SCAN:
                truncated = True
            break
        try:
            cell_range = formula_cells.getByIndex(i)
            addr = cell_range.getRangeAddress()
            formula_matrix = cell_range.getFormulas() if hasattr(cell_range, "getFormulas") else None
        except Exception:
            continue

        if formula_matrix is not None and len(formula_matrix) > 0:
            stop = False
            for r_idx, row_formulas in enumerate(formula_matrix):
                row = addr.StartRow + r_idx
                for c_idx, formula in enumerate(row_formulas):
                    scanned_count += 1
                    if scanned_count > _MAX_CELLS_TO_SCAN:
                        truncated = True
                        stop = True
                        break
                    col = addr.StartColumn + c_idx
                    if not _consider(str(formula), row, col):
                        stop = True
                        break
                if stop:
                    break
        else:
            stop = False
            for row in range(addr.StartRow, addr.EndRow + 1):
                if truncated or scanned_count >= _MAX_CELLS_TO_SCAN:
                    if scanned_count >= _MAX_CELLS_TO_SCAN:
                        truncated = True
                    stop = True
                    break
                for col in range(addr.StartColumn, addr.EndColumn + 1):
                    scanned_count += 1
                    if scanned_count > _MAX_CELLS_TO_SCAN:
                        truncated = True
                        stop = True
                        break
                    try:
                        cell = sheet.getCellByPosition(col, row)
                        formula = str(cell.getFormula() or "")
                    except Exception:
                        continue
                    if not _consider(formula, row, col):
                        stop = True
                        break
                if stop:
                    break
        if truncated:
            break

    found.sort(key=lambda c: (c.row, c.column))
    return PythonSheetDiscovery(found, truncated)


def list_python_cells_in_doc(doc: Any, *, active_sheet_only: bool = True) -> list[PythonCellInfo]:
    """Enumerate Python cells in *doc* (active sheet by default)."""
    if doc is None:
        return []
    try:
        controller = doc.getCurrentController()
        if active_sheet_only and controller is not None:
            sheet = controller.getActiveSheet()
            if sheet is not None:
                return list_python_cells_on_sheet(sheet)
    except Exception:
        log.debug("list_python_cells_in_doc: active sheet path failed", exc_info=True)

    if active_sheet_only:
        return []

    out: list[PythonCellInfo] = []
    try:
        sheets = doc.getSheets()
        for i in range(sheets.getCount()):
            sheet = sheets.getByIndex(i)
            out.extend(list_python_cells_on_sheet(sheet))
    except Exception:
        log.debug("list_python_cells_in_doc: all-sheets path failed", exc_info=True)
    return out
