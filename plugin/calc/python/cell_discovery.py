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

from dataclasses import dataclass
from typing import Any

from plugin.calc.address_utils import format_address, qualify_a1
from plugin.calc.python.collabora_formula import rewrite_collabora_addin_prefix
from plugin.calc.python.formula_edit import (
    extract_python_code_loose,
    normalize_formula_string,
    stored_addin_call_open_end,
)
from plugin.calc.python.formula_locator_cache import CELL_FLAG_FORMULA, iter_range_formula_texts

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
    raw = rewrite_collabora_addin_prefix(normalize_formula_string(formula))
    end = stored_addin_call_open_end(raw)
    if end is None:
        return raw
    return "=PYTHON(" + raw[end:]


def python_code_from_formula(formula: str) -> str | None:
    """Extract Python code from a PY/PYTHON formula, or None if not a PY formula."""
    # Every PY/PYTHON add-in spelling contains "PY"; reject the rest before parsing.
    if "PY" not in (formula or "").upper():
        return None
    return extract_python_code_loose(canonicalize_py_formula_for_parse(formula))


def is_py_formula_text(formula: str) -> bool:
    """True when *formula* is PY/PYTHON, including fully qualified add-in form."""
    return python_code_from_formula(formula) is not None


def discover_python_cells_on_sheet(sheet: Any) -> PythonSheetDiscovery:
    """Discover PY cells and report whether the 100 / 50k caps truncated the list.

    After 100 PY cells we keep scanning for one more. Finding #101 (or hitting
    the 50k scan cap) sets ``truncated=True``. An exact 100 that finishes the
    formula-cell walk is complete.
    """
    if sheet is None:
        return PythonSheetDiscovery([], False)
    name = str(sheet.getName() or "") if hasattr(sheet, "getName") else ""
    if not name:
        name = "Sheet"

    found: list[PythonCellInfo] = []
    truncated = False
    formula_cells = sheet.queryContentCells(CELL_FLAG_FORMULA)
    if formula_cells is None:
        return PythonSheetDiscovery([], False)

    count = int(formula_cells.getCount())

    scanned_count = 0

    def _consider(formula: str, row: int, col: int) -> bool:
        """Add a PY cell, or mark truncated on the 101st. False means stop."""
        nonlocal truncated
        if not formula:
            return True
        code = python_code_from_formula(str(formula))
        if code is None:
            return True
        if len(found) >= _MAX_PYTHON_CELLS_FOUND:
            # 101st PY cell: the returned list of 100 is incomplete.
            truncated = True
            return False
        # Unquoted sheet names (e.g. Data.2024 or names with spaces) broke downstream
        # address parsing and navigation; qualify_a1 quotes the sheet prefix when needed.
        found.append(PythonCellInfo(sheet=name, row=row, column=col, address=qualify_a1(name, format_address(col, row)), code=code, formula=str(formula)))
        return True

    for i in range(count):
        cell_range = formula_cells.getByIndex(i)
        for row, col, formula in iter_range_formula_texts(sheet, cell_range):
            if scanned_count >= _MAX_CELLS_TO_SCAN:
                truncated = True
                break
            scanned_count += 1
            if not _consider(formula, row, col):
                break
        if truncated:
            break

    found.sort(key=lambda c: (c.row, c.column))
    return PythonSheetDiscovery(found, truncated)


def list_python_cells_in_doc(doc: Any, *, active_sheet_only: bool = True) -> list[PythonCellInfo]:
    """Enumerate Python cells in *doc* (active sheet by default)."""
    if doc is None:
        return []
    if active_sheet_only:
        controller = doc.getCurrentController()
        if controller is not None:
            sheet = controller.getActiveSheet()
            if sheet is not None:
                return discover_python_cells_on_sheet(sheet).cells
        return []

    out: list[PythonCellInfo] = []
    sheets = doc.getSheets()
    for i in range(sheets.getCount()):
        sheet = sheets.getByIndex(i)
        out.extend(discover_python_cells_on_sheet(sheet).cells)
    return out
