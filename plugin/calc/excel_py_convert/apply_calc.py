# SPDX-License-Identifier: GPL-3.0-or-later
"""Apply a DAG conversion report to an open Calc document via UNO.

Used when openpyxl is unavailable in the LibreOffice host (LibrePy OXT does not
vendor it). Formulas use Calc ``;`` separators; spill ranges are cleared before
rewrite so cached array results do not block the new ``=PY``.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from plugin.calc.python.formula_edit import rebuild_python_formula_with_data

if TYPE_CHECKING:
    from plugin.calc.excel_py_convert.models import ConversionReport, ConvertedCell

log = logging.getLogger(__name__)

_RE_A1 = re.compile(r"^([A-Za-z]+)(\d+)$")


def _col_letters_to_index(col: str) -> int:
    n = 0
    for ch in col.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def _index_to_col_letters(n: int) -> str:
    letters = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(65 + rem))
    return "".join(reversed(letters))


def _iter_a1_span(ref: str) -> list[str]:
    """Expand ``A1:B2`` (no sheet) into cell coordinates; single cell → [cell]."""
    raw = (ref or "").replace("$", "")
    if "!" in raw:
        raw = raw.split("!", 1)[1]
    if ":" not in raw:
        return [raw] if _RE_A1.match(raw) else []
    left, right = raw.split(":", 1)
    m1, m2 = _RE_A1.match(left), _RE_A1.match(right)
    if not m1 or not m2:
        return []
    c1, r1 = _col_letters_to_index(m1.group(1)), int(m1.group(2))
    c2, r2 = _col_letters_to_index(m2.group(1)), int(m2.group(2))
    if c1 > c2:
        c1, c2 = c2, c1
    if r1 > r2:
        r1, r2 = r2, r1
    out: list[str] = []
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            out.append(f"{_index_to_col_letters(c)}{r}")
    return out


def _calc_formula_for_cell(cell: ConvertedCell) -> str:
    args = list(cell.data_args) + list(cell.ordering_args)
    return rebuild_python_formula_with_data(cell.converted_code, args, separator=";")


def _resolve_sheet(doc: Any, sheet_name: str) -> Any | None:
    sheets = doc.getSheets()
    if sheets.hasByName(sheet_name):
        return sheets.getByName(sheet_name)
    # Case-insensitive fallback (Excel titles vs Calc import quirks).
    for i in range(sheets.getCount()):
        sh = sheets.getByIndex(i)
        if str(sh.getName()).lower() == sheet_name.lower():
            return sh
    return None


def _clear_spill_uno(sheet: Any, anchor: str, array_ref: str) -> None:
    if not array_ref:
        return
    cells = _iter_a1_span(array_ref)
    if len(cells) <= 1:
        return
    for coord in cells:
        if coord == anchor:
            continue
        try:
            cell = sheet.getCellRangeByName(coord)
            cell.setFormula("")
            if hasattr(cell, "setString"):
                cell.setString("")
        except Exception:
            continue


def apply_dag_formulas_to_calc_doc(doc: Any, report: ConversionReport) -> list[str]:
    """Write successfully converted cells onto *doc*. Returns a list of error strings."""
    errors: list[str] = []
    for cell in report.cells:
        if not cell.converted or not cell.converted_code:
            continue
        sheet = _resolve_sheet(doc, cell.sheet)
        if sheet is None:
            errors.append(f"unmapped sheet {cell.sheet!r} for cell {cell.cell}")
            continue
        try:
            if cell.array_ref:
                _clear_spill_uno(sheet, cell.cell, cell.array_ref)
            uno_cell = sheet.getCellRangeByName(cell.cell)
            uno_cell.setFormula(_calc_formula_for_cell(cell))
        except Exception as exc:
            errors.append(f"{cell.sheet}!{cell.cell}: {exc}")
    if not errors:
        try:
            if hasattr(doc, "calculateAll"):
                doc.calculateAll()
        except Exception:
            log.debug("calculateAll after Excel PY apply failed", exc_info=True)
    return errors
