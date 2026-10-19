# SPDX-License-Identifier: GPL-3.0-or-later
"""Snapshot DAG-style =PY cells from a live LibreOffice Calc document (UNO) for Excel export."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.calc.address_utils import index_to_column
from plugin.calc.excel_py_convert.script_bank import CODE_SHEET_PREFIX, resolve_bank_cell_reference
from plugin.calc.excel_py_convert.to_excel import convert_dag_cells_to_excel
from plugin.calc.python.cell_discovery import canonicalize_py_formula_for_parse, is_py_formula_text
from plugin.calc.python.formula_edit import CALC_PYTHON_FN, escape_code_for_excel_formula, parse_python_formula
from plugin.framework.errors import uno_probe

if TYPE_CHECKING:
    from plugin.calc.excel_py_convert.models import ConversionReport

log = logging.getLogger(__name__)


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

    sheets = doc.getSheets()
    sheet_by_name: dict[str, Any] = {}
    for i in range(sheets.getCount()):
        sh = sheets.getByIndex(i)
        sheet_by_name[str(sh.getName())] = sh
    lower_sheet_by_name: dict[str, Any] = {k.lower(): v for k, v in sheet_by_name.items()}

    bank_cache: dict[str, dict[str, str]] = {}

    def _get_bank_strings(bank_sheet: Any, sheet_key: str) -> dict[str, str]:
        if sheet_key in bank_cache:
            return bank_cache[sheet_key]
        cached: dict[str, str] = {}
        string_cells = uno_probe(lambda: bank_sheet.queryContentCells(CellFlags.STRING), None)
        if string_cells is not None and hasattr(string_cells, "getCount"):
            for idx in range(string_cells.getCount()):
                rng = string_cells.getByIndex(idx)
                addr = rng.getRangeAddress()
                data = rng.getDataArray() if hasattr(rng, "getDataArray") else None
                if data is not None:
                    for r_offset, row_data in enumerate(data):
                        r = addr.StartRow + r_offset
                        for c_offset, val in enumerate(row_data):
                            s = str(val or "")
                            if s.strip():
                                c = addr.StartColumn + c_offset
                                coord = f"{index_to_column(c)}{r + 1}"
                                cached[coord] = s
        if not cached:
            cells_attr = getattr(bank_sheet, "_cells", None)
            if isinstance(cells_attr, dict):
                for (c, r), cell in cells_attr.items():
                    s = str(cell.getString() if hasattr(cell, "getString") else "")
                    if s.strip():
                        cached[f"{index_to_column(c)}{r + 1}"] = s
            elif hasattr(bank_sheet, "createCursor"):
                cursor = bank_sheet.createCursor()
                cursor.gotoStartOfUsedArea(False)
                cursor.gotoEndOfUsedArea(True)
                addr = cursor.getRangeAddress()
                data = cursor.getDataArray() if hasattr(cursor, "getDataArray") else None
                if data is not None:
                    for r_offset, row_data in enumerate(data):
                        r = addr.StartRow + r_offset
                        for c_offset, val in enumerate(row_data):
                            s = str(val or "")
                            if s.strip():
                                c = addr.StartColumn + c_offset
                                coord = f"{index_to_column(c)}{r + 1}"
                                cached[coord] = s
        bank_cache[sheet_key] = cached
        return cached

    def _uno_lookup(sheet_name: str, a1: str) -> str | None:
        bank_sheet = sheet_by_name.get(sheet_name)
        if bank_sheet is None:
            bank_sheet = lower_sheet_by_name.get(sheet_name.lower())
        if bank_sheet is None:
            return None
        sheet_key = str(bank_sheet.getName())
        cached = _get_bank_strings(bank_sheet, sheet_key)
        if a1 in cached:
            val = cached[a1]
            return val if val.strip() else None
        val = uno_probe(lambda: str(bank_sheet.getCellRangeByName(a1).getString() or ""), "")
        return val if val and val.strip() else None

    # Higher ceiling than sidebar discovery — export must see every PY cell.
    max_scan = 100_000
    total_scanned = 0
    stop_scanning = False
    scan_limit_reached = False

    for sheet_name, sheet in sheet_by_name.items():
        if stop_scanning:
            break
        if sheet_name.startswith(CODE_SHEET_PREFIX):
            continue
        formula_cells = sheet.queryContentCells(CellFlags.FORMULA)
        if formula_cells is None:
            continue
        count = int(formula_cells.getCount())

        for i in range(count):
            if total_scanned >= max_scan:
                # stop_scanning breaks both loops; a per-sheet counter would let later sheets scan past max_scan.
                scan_limit_reached = True
                stop_scanning = True
                break
            cell_range = formula_cells.getByIndex(i)
            addr = cell_range.getRangeAddress()
            formula_matrix = cell_range.getFormulas() if hasattr(cell_range, "getFormulas") else None
            if formula_matrix is None:
                continue
            for r_idx, row_formulas in enumerate(formula_matrix):
                if total_scanned >= max_scan:
                    scan_limit_reached = True
                    stop_scanning = True
                    break
                row = addr.StartRow + r_idx
                for c_idx, formula in enumerate(row_formulas):
                    total_scanned += 1
                    if not formula or not is_py_formula_text(str(formula)):
                        continue
                    col = addr.StartColumn + c_idx
                    a1 = f"{index_to_column(col)}{row + 1}"
                    canonical = canonicalize_py_formula_for_parse(str(formula))
                    parts = parse_python_formula(canonical)
                    if parts is None:
                        continue
                    resolved = resolve_bank_cell_reference(parts.code, _uno_lookup)
                    if resolved is not None and resolved.strip():
                        formula_out = f'={CALC_PYTHON_FN}("{escape_code_for_excel_formula(resolved)}"{parts.data_suffix}'
                    else:
                        formula_out = canonical
                    cell_meta: dict[str, Any] = {}
                    stored = meta_map.get(cell_meta_key(sheet_name, a1))
                    if isinstance(stored, dict):
                        cell_meta = dict(stored)
                    triples.append((sheet_name, a1, formula_out, cell_meta))

    report = convert_dag_cells_to_excel(triples)
    if scan_limit_reached:
        report.issues.append(f"scan limit reached ({max_scan} formula cells); export aborted")
    doc_url = uno_probe(lambda: str(doc.getURL() or ""), "")
    if not doc_url:
        doc_url = getattr(doc, "url", "") or ""
    if doc_url:
        report.source_path = doc_url
    return report
