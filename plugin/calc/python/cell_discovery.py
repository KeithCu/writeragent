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
    name = str(sheet.getName() or "") or "Sheet"
    found: list[PythonCellInfo] = []
    truncated = False
    formula_cells = sheet.queryContentCells(CELL_FLAG_FORMULA)
    if formula_cells is None:
        return PythonSheetDiscovery([], False)

    def _formulas():
        for i in range(int(formula_cells.getCount())):
            yield from iter_range_formula_texts(sheet, formula_cells.getByIndex(i))

    for scanned, (row, col, formula) in enumerate(_formulas()):
        if scanned >= _MAX_CELLS_TO_SCAN:
            truncated = True
            break
        code = python_code_from_formula(formula)
        if code is None:
            continue
        if len(found) >= _MAX_PYTHON_CELLS_FOUND:
            # 101st PY cell: the returned list of 100 is incomplete.
            truncated = True
            break
        # Unquoted sheet names (e.g. Data.2024 or names with spaces) broke downstream
        # address parsing and navigation; qualify_a1 quotes the sheet prefix when needed.
        found.append(PythonCellInfo(sheet=name, row=row, column=col, address=qualify_a1(name, format_address(col, row)), code=code, formula=formula))

    found.sort(key=lambda c: (c.row, c.column))
    return PythonSheetDiscovery(found, truncated)


def list_python_cells_in_doc(doc: Any) -> list[PythonCellInfo]:
    """Enumerate Python cells on the active sheet of *doc*."""
    if doc is None:
        return []
    controller = doc.getCurrentController()
    sheet = controller.getActiveSheet() if controller is not None else None
    if sheet is None:
        return []
    return discover_python_cells_on_sheet(sheet).cells
