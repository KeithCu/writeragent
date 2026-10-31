# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Emit ``=PY()`` formulas and build converted output models."""

from __future__ import annotations

import logging
import re
from typing import Any

from plugin.framework.errors import best_effort
from plugin.calc.address_utils import parse_address
from plugin.calc.python.formula_edit import rebuild_python_formula_with_data
from plugin.calc.spreadsheet_import.models import (
    CellRecord,
    ConversionReport,
    OutputCell,
    OutputSheetModel,
    SheetModel,
    TodoCell,
    output_cell_from_record,
)
from plugin.calc.spreadsheet_import.range_clip import clip_workbook_data_ranges
from plugin.calc.spreadsheet_import.translate import translate_formula
from plugin.calc.spreadsheet_import.vectorize import (
    detect_vectorized_columns,
    translation_has_cross_sheet_ranges,
    vectorize_range,
)


from plugin.contrib.calc_formula_parser import FunctionNode, parse_formula
from plugin.calc.spreadsheet_import.preprocess import normalize_lo_formula_for_parse

log = logging.getLogger(__name__)


def _has_function_node(node: Any) -> bool:
    try:
        return any(isinstance(n, FunctionNode) for n in node)
    except Exception:
        # Walking a very deep AST can hit the recursion limit; say "has a function"
        # so the cell goes to the guarded translate path and is reported EMIT_ERROR.
        return True


def emit_py_formula(code: str, data_ranges: list[str], *, sheet_bounds: dict[str, tuple[int, int]] | None = None, current_sheet: str | None = None) -> str:
    """Build canonical ``=PY("…"; ranges…)``."""
    clipped = clip_workbook_data_ranges(data_ranges, sheet_bounds=sheet_bounds, current_sheet=current_sheet)
    return rebuild_python_formula_with_data(code, clipped)


def build_converted_output_model(model: SheetModel, *, vectorize: bool = False, sheet_bounds: dict[str, tuple[int, int]] | None = None) -> tuple[OutputSheetModel, ConversionReport]:
    """Translate formula cells to ``=PY()``; preserve constants and normalized PY."""
    report = ConversionReport()
    circular = {addr for group in model.circular_groups for addr in group}

    vector_groups = detect_vectorized_columns(model) if vectorize else {}
    vector_handled_cells: dict[str, str] = {}

    for first_addr, group in vector_groups.items():
        group_formulas: dict[str, str] = {}
        try:
            first_record = model.cells[first_addr]
            if not first_record.formula:
                continue
            ast = best_effort(lambda: parse_formula(normalize_lo_formula_for_parse(first_record.formula)), "vector parse formula")
            if ast is None or not _has_function_node(ast):
                continue
            translation = translate_formula(first_record.formula, cell_addr=first_addr)
            if translation.ok and translation.code and translation.data_ranges is not None and not translation_has_cross_sheet_ranges(translation.data_ranges) and "calc.fmt(" not in translation.code:
                last_addr = group[-1]
                row_delta = parse_address(last_addr)[1] - parse_address(first_addr)[1]
                vectorized_data_ranges = [vectorize_range(a1_range, row_delta) for a1_range in translation.data_ranges]

                # Convert bare data references to np.asarray(data) to support element-wise operations
                code = translation.code
                if len(translation.data_ranges) == 1:
                    code = re.sub(r"(?<!np\.asarray\()\bdata\b", "np.asarray(data)", code)
                else:
                    for idx in range(len(translation.data_ranges)):
                        code = re.sub(rf"(?<!np\.asarray\()\bdata\[{idx}\]\b", f"np.asarray(data[{idx}])", code)

                # Strip scalar coercion since array formulas return vectors, not scalars
                if code.startswith("float(") and code.endswith(")"):
                    code = code[6:-1]

                for idx, addr in enumerate(group):
                    formula = emit_py_formula(code, vectorized_data_ranges + [str(idx)], sheet_bounds=sheet_bounds, current_sheet=model.sheet_name)
                    group_formulas[addr] = formula
        except Exception:
            log.exception("build_converted_output_model: vector group at %s failed", first_addr)
            continue
        vector_handled_cells.update(group_formulas)
        report.converted.extend(group_formulas)

    cells: dict[str, OutputCell] = {}

    def _pass_through(addr: str, record: CellRecord, reason: str | None) -> None:
        report.pass_through.append(addr)
        if reason is not None:
            report.skipped.append(TodoCell(address=addr, reason=reason))
        cells[addr] = output_cell_from_record(record)

    for addr in sorted(model.cells):
        if addr in vector_handled_cells:
            cells[addr] = OutputCell(address=addr, value=None, formula=vector_handled_cells[addr], number_format=None)
            continue

        record = model.cells[addr]
        if record.type == "py_formula":
            cells[addr] = output_cell_from_record(record)
            continue
        if record.type == "prompt":
            _pass_through(addr, record, "PROMPT")
            continue
        if record.type == "array_formula":
            _pass_through(addr, record, "ARRAY_FORMULA")
            continue
        if addr in circular:
            _pass_through(addr, record, "CIRCULAR_REF")
            continue
        # prompt/array/py are not formula|error; they must be handled above.
        # circular_groups contain only formula-like addresses (graph.py).
        if record.type in ("empty", "constant") or record.type not in ("formula", "error") or not record.formula:
            cells[addr] = output_cell_from_record(record)
            continue

        ast = best_effort(lambda: parse_formula(normalize_lo_formula_for_parse(record.formula)), "parse formula")
        if ast is not None and not _has_function_node(ast):
            _pass_through(addr, record, None)
            continue

        try:
            translation = translate_formula(record.formula, cell_addr=addr)
            log.debug("build_converted_output_model translate cell %s formula=%r translation.ok=%s reason=%s code=%r", addr, record.formula, translation.ok, translation.reason, translation.code)
            if translation.ok and translation.code and translation.data_ranges is not None:
                cells[addr] = OutputCell(address=addr, value=None, formula=emit_py_formula(translation.code, translation.data_ranges, sheet_bounds=sheet_bounds, current_sheet=model.sheet_name), number_format=None)
                report.converted.append(addr)
            else:
                _pass_through(addr, record, translation.reason or "UNSUPPORTED_FUNCTION")
        except Exception:
            # Bugfix: one pathological formula aborted the whole sheet. parse/_emit
            # recurse the AST, so a formula deeper than Python's recursion limit
            # raises RecursionError out of translate_formula (it catches only
            # ValueError/TypeError/IndexError/TranslationError). Keep the original
            # formula and report this cell instead.
            log.exception("build_converted_output_model: cell %s failed", addr)
            _pass_through(addr, record, "EMIT_ERROR")

    output = OutputSheetModel(sheet_name=model.sheet_name, used_range=model.used_range, cells=cells)
    return output, report
