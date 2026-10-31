# SPDX-License-Identifier: GPL-3.0-or-later
"""Snapshot DAG-style =PY cells from a live LibreOffice Calc document (UNO) for Excel export."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.calc.address_utils import index_to_column
from plugin.calc.excel_py_convert.script_bank import CODE_SHEET_PREFIX, resolve_bank_cell_reference
from plugin.calc.excel_py_convert.to_excel import convert_dag_cells_to_excel
from plugin.calc.python.cell_discovery import canonicalize_py_formula_for_parse, is_py_formula_text
from plugin.calc.python.formula_edit import CALC_PYTHON_FN, escape_code_for_formula, parse_python_formula
from plugin.framework.errors import uno_probe

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
    ``return_type`` / ``data_args`` (legacy ``ordering_args`` ignored on export).
    """
    from com.sun.star.sheet import CellFlags

    from plugin.calc.excel_py_convert.convert import cell_meta_key, load_dag_meta_from_doc

    meta_map = load_dag_meta_from_doc(doc)
    triples: list[tuple[str, str, str, dict[str, Any]]] = []

    named_sheets: list[tuple[str, Any]] = []
    doc_sheets = doc.getSheets()
    for i in range(doc_sheets.getCount()):
        sh = doc_sheets.getByIndex(i)
        named_sheets.append((str(sh.getName()), sh))

    def _uno_lookup(sheet_name: str, a1: str) -> str | None:
        want = sheet_name.lower()
        bank_sheet = next((sh for name, sh in named_sheets if name.lower() == want), None)
        if bank_sheet is None:
            return None
        val = uno_probe(lambda: str(bank_sheet.getCellRangeByName(a1).getString() or ""), "")
        return val if val and val.strip() else None

    # Higher ceiling than sidebar discovery — export must see every PY cell.
    total_scanned = 0
    scan_limit_reached = False
    unparseable_issues: list[str] = []

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
            if not formula or not is_py_formula_text(str(formula)):
                continue
            a1 = f"{index_to_column(col)}{row + 1}"
            canonical = canonicalize_py_formula_for_parse(str(formula))
            parts = parse_python_formula(canonical)
            if parts is None:
                unparseable_issues.append(f"{sheet_name}!{a1}: =PY formula could not be parsed")
                continue
            resolved = resolve_bank_cell_reference(parts.code, _uno_lookup)
            if resolved is not None and resolved.strip():
                formula_out = f'={CALC_PYTHON_FN}("{escape_code_for_formula(resolved)}"{parts.data_suffix}'
            else:
                formula_out = canonical
            cell_meta: dict[str, Any] = {}
            stored = meta_map.get(cell_meta_key(sheet_name, a1))
            if isinstance(stored, dict):
                cell_meta = dict(stored)
            triples.append((sheet_name, a1, formula_out, cell_meta))
        if scan_limit_reached:
            break

    report = convert_dag_cells_to_excel(triples)
    report.issues.extend(unparseable_issues)
    if scan_limit_reached:
        report.issues.append(f"scan limit reached ({FORMULA_SCAN_CAP} formula cells); export aborted")
    doc_url = uno_probe(lambda: str(doc.getURL() or ""), "")
    if doc_url:
        report.source_path = doc_url
    return report
