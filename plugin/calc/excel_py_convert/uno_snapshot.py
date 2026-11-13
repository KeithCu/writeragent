# SPDX-License-Identifier: GPL-3.0-or-later
"""Snapshot DAG-style =PY cells from a live LibreOffice Calc document (UNO) for Excel export."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.calc.address_utils import format_address
from plugin.calc.excel_py_convert.convert import cell_meta_key, load_dag_meta_from_doc
from plugin.calc.excel_py_convert.models import DagCellMeta, DagFormulaItem, find_sheet_ci
from plugin.calc.excel_py_convert.script_bank import CODE_SHEET_PREFIX, resolve_bank_cell_reference
from plugin.calc.excel_py_convert.to_excel import convert_dag_cells_to_excel
from plugin.calc.python.formula_edit import CALC_PYTHON_FN, PyFormula, escape_code_for_formula
from plugin.framework.errors import uno_read

if TYPE_CHECKING:
    from plugin.calc.excel_py_convert.models import ConversionReport

log = logging.getLogger(__name__)


FORMULA_SCAN_CHUNK_ROWS = 2048
FORMULA_SCAN_CAP = 2_000_000


def _iter_sheet_formulas(sheet: Any, formula_cells: Any):
    """Yield (row, col, formula). One getFormulas() chunk per FORMULA_SCAN_CHUNK_ROWS."""
    for i in range(int(formula_cells.getCount())):
        addr = formula_cells.getByIndex(i).getRangeAddress()
        start_row, end_row = int(addr.StartRow), int(addr.EndRow)
        start_col, end_col = int(addr.StartColumn), int(addr.EndColumn)
        for r_start in range(start_row, end_row + 1, FORMULA_SCAN_CHUNK_ROWS):
            r_end = min(r_start + FORMULA_SCAN_CHUNK_ROWS - 1, end_row)
            matrix = sheet.getCellRangeByPosition(start_col, r_start, end_col, r_end).getFormulas()
            if not matrix:
                continue
            for r_idx, row_formulas in enumerate(matrix):
                row = r_start + r_idx
                for c_idx, formula in enumerate(row_formulas):
                    yield row, start_col + c_idx, formula


def convert_uno_doc_to_excel(doc: Any) -> ConversionReport:
    """Snapshot DAG ``=PY`` cells from an open Calc doc (UNO) → Excel conversion report.

    Resolves ``py_code_*`` bank cell strings in memory so long scripts export correctly.
    When ``ExcelPyDagMeta`` udprop is present (set on auto-open), uses stored
    ``return_type`` / ``data_args``.
    """
    # UNO constant; resolves only once pyuno is loaded.
    from com.sun.star.sheet import CellFlags

    meta_map = load_dag_meta_from_doc(doc)
    items: list[DagFormulaItem] = []

    named_sheets: list[tuple[str, Any]] = []
    doc_sheets = doc.getSheets()
    for i in range(doc_sheets.getCount()):
        sh = doc_sheets.getByIndex(i)
        named_sheets.append((str(sh.getName()), sh))
    sheets_by_name = dict(named_sheets)

    def _uno_lookup(sheet_name: str, a1: str) -> str | None:
        bank_sheet = find_sheet_ci(sheets_by_name, sheet_name)
        if bank_sheet is None:
            return None
        val = uno_read(lambda: str(bank_sheet.getCellRangeByName(a1).getString() or ""), "")
        return val if val and val.strip() else None

    # Higher ceiling than sidebar discovery — export must see every PY cell.
    total_scanned = 0
    scan_limit_reached = False

    for sheet_name, sheet in named_sheets:
        if sheet_name.startswith(CODE_SHEET_PREFIX):
            continue
        formula_cells = sheet.queryContentCells(CellFlags.FORMULA)
        if formula_cells is None:
            continue
        for row, col, formula in _iter_sheet_formulas(sheet, formula_cells):
            if total_scanned >= FORMULA_SCAN_CAP:
                # Also break the sheet loop. A per-sheet counter would scan later sheets past the cap.
                scan_limit_reached = True
                break
            total_scanned += 1
            if not formula:
                continue
            parsed = PyFormula.parse(str(formula))
            if parsed is None:
                continue
            a1 = format_address(col, row)
            resolved = resolve_bank_cell_reference(parsed.code, _uno_lookup)
            if resolved is not None and resolved.strip():
                formula_out = f'={CALC_PYTHON_FN}("{escape_code_for_formula(resolved)}"{parsed.data_suffix}'
            else:
                formula_out = str(formula)
            stored = meta_map.get(cell_meta_key(sheet_name, a1))
            items.append(DagFormulaItem(sheet_name, a1, formula_out, DagCellMeta.from_dict(stored) if isinstance(stored, dict) else None))
        if scan_limit_reached:
            break

    report = convert_dag_cells_to_excel(items)
    if scan_limit_reached:
        report.issues.append(f"scan limit reached ({FORMULA_SCAN_CAP} formula cells); export aborted")
    doc_url = uno_read(lambda: str(doc.getURL() or ""), "")
    if doc_url:
        report.source_path = doc_url
    return report
