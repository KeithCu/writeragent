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


FORMULA_SCAN_CHUNK_ROWS = 2048
FORMULA_SCAN_CAP = 2_000_000


def _collect_data_array_strings(target: Any, addr: Any, cached: dict[str, str]) -> None:
    data = target.getDataArray() if hasattr(target, "getDataArray") else None
    if data is not None:
        for r_offset, row_data in enumerate(data):
            r = addr.StartRow + r_offset
            for c_offset, val in enumerate(row_data):
                s = str(val or "")
                if s.strip():
                    c = addr.StartColumn + c_offset
                    coord = f"{index_to_column(c)}{r + 1}"
                    cached[coord] = s


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
                _collect_data_array_strings(rng, addr, cached)
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
                _collect_data_array_strings(cursor, addr, cached)
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
    total_scanned = 0
    stop_scanning = False
    scan_limit_reached = False
    unparseable_issues: list[str] = []

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
            if total_scanned >= FORMULA_SCAN_CAP:
                # stop_scanning breaks all loops; a per-sheet counter would let later sheets scan past the cap.
                scan_limit_reached = True
                stop_scanning = True
                break
            cell_range = formula_cells.getByIndex(i)
            addr = cell_range.getRangeAddress()
            start_row = int(addr.StartRow)
            end_row = int(addr.EndRow)
            start_col = int(addr.StartColumn)
            end_col = int(addr.EndColumn)
            row_span = end_row - start_row + 1

            if row_span > FORMULA_SCAN_CHUNK_ROWS and hasattr(sheet, "getCellRangeByPosition"):
                chunks: list[tuple[int, Any]] = []
                for r_start in range(start_row, end_row + 1, FORMULA_SCAN_CHUNK_ROWS):
                    r_end = min(r_start + FORMULA_SCAN_CHUNK_ROWS - 1, end_row)
                    sub_range = sheet.getCellRangeByPosition(start_col, r_start, end_col, r_end)
                    if sub_range is not None:
                        chunks.append((r_start, sub_range))
            else:
                chunks = [(start_row, cell_range)]

            for chunk_start_row, target_range in chunks:
                if total_scanned >= FORMULA_SCAN_CAP:
                    scan_limit_reached = True
                    stop_scanning = True
                    break
                formula_matrix = target_range.getFormulas() if hasattr(target_range, "getFormulas") else None
                if formula_matrix is None:
                    continue
                for r_idx, row_formulas in enumerate(formula_matrix):
                    if total_scanned >= FORMULA_SCAN_CAP:
                        scan_limit_reached = True
                        stop_scanning = True
                        break
                    row = chunk_start_row + r_idx
                    for c_idx, formula in enumerate(row_formulas):
                        if total_scanned >= FORMULA_SCAN_CAP:
                            scan_limit_reached = True
                            stop_scanning = True
                            break
                        total_scanned += 1
                        if not formula or not is_py_formula_text(str(formula)):
                            continue
                        col = start_col + c_idx
                        a1 = f"{index_to_column(col)}{row + 1}"
                        canonical = canonicalize_py_formula_for_parse(str(formula))
                        parts = parse_python_formula(canonical)
                        if parts is None:
                            unparseable_issues.append(f"{sheet_name}!{a1}: =PY formula could not be parsed")
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
    report.issues.extend(unparseable_issues)
    if scan_limit_reached:
        report.issues.append(f"scan limit reached ({FORMULA_SCAN_CAP} formula cells); export aborted")
    doc_url = uno_probe(lambda: str(doc.getURL() or ""), "")
    if not doc_url:
        doc_url = getattr(doc, "url", "") or ""
    if doc_url:
        report.source_path = doc_url
    return report
