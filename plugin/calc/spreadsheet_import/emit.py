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
from plugin.calc.python.formula_edit import rebuild_python_formula_with_data
from plugin.calc.spreadsheet_import.extract import extract_py_cells
from plugin.calc.spreadsheet_import.models import (
    CellRecord,
    ConversionReport,
    OutputCell,
    OutputSheetModel,
    SheetModel,
    TodoCell,
    output_cell_from_record,
)
from plugin.calc.spreadsheet_import.translate import translate_formula


from plugin.contrib.calc_formula_parser import FunctionNode, parse_formula
from plugin.calc.spreadsheet_import.preprocess import normalize_lo_formula_for_parse

log = logging.getLogger(__name__)


def _has_function_node(node: Any) -> bool:
    return any(isinstance(n, FunctionNode) for n in node)


def emit_py_formula(code: str, data_ranges: list[str], *, sheet_bounds: dict[str, tuple[int, int]] | None = None, current_sheet: str | None = None) -> str:
    """Build canonical ``=PY("…"; ranges…)``."""
    from plugin.calc.spreadsheet_import.range_clip import clip_workbook_data_ranges

    clipped = clip_workbook_data_ranges(data_ranges, sheet_bounds=sheet_bounds, current_sheet=current_sheet)
    return rebuild_python_formula_with_data(code, clipped)


def _circular_addresses(model: SheetModel) -> set[str]:
    addrs: set[str] = set()
    for group in model.circular_groups:
        addrs.update(group)
    return addrs


def build_converted_output_model(model: SheetModel, *, vectorize: bool = False, sheet_bounds: dict[str, tuple[int, int]] | None = None) -> tuple[OutputSheetModel, ConversionReport]:
    """Translate formula cells to ``=PY()``; preserve constants and normalized PY."""
    from plugin.calc.spreadsheet_import.vectorize import detect_vectorized_columns, to_r1c1, translation_has_cross_sheet_ranges, vectorize_range

    report = ConversionReport()
    circular = _circular_addresses(model)
    py_extracts = extract_py_cells(model)
    py_by_addr = {item.address: item for item in py_extracts}

    vector_groups = detect_vectorized_columns(model) if vectorize else {}
    vector_handled_cells: dict[str, str] = {}

    for first_addr, group in vector_groups.items():
        first_record = model.cells[first_addr]
        if not first_record.formula:
            continue
        ast = best_effort(lambda: parse_formula(normalize_lo_formula_for_parse(first_record.formula)), "vector parse formula")
        if ast is None or not _has_function_node(ast):
            continue
        translation = translate_formula(first_record.formula, cell_addr=first_addr)
        if translation.ok and translation.code and translation.data_ranges is not None and not translation_has_cross_sheet_ranges(translation.data_ranges) and "calc.fmt(" not in translation.code:
            last_addr = group[-1]
            vectorized_data_ranges = []
            for a1_range in translation.data_ranges:
                r1c1_range = to_r1c1(a1_range, first_addr)
                vec_range = vectorize_range(r1c1_range, first_addr, last_addr)
                vectorized_data_ranges.append(vec_range)

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
                vector_handled_cells[addr] = formula
                report.converted.append(addr)

    cells: dict[str, OutputCell] = {}

    def _pass_through(addr: str, record: CellRecord, reason: str | None) -> None:
        report.pass_through.append(addr)
        if reason is not None:
            report.skipped.append(TodoCell(address=addr, reason=reason))
        cells[addr] = output_cell_from_record(record, py_by_addr)

    for addr in sorted(model.cells):
        if addr in vector_handled_cells:
            cells[addr] = OutputCell(address=addr, value=None, formula=vector_handled_cells[addr], number_format=None)
            continue

        record = model.cells[addr]
        if record.type == "empty":
            cells[addr] = OutputCell(address=addr, value=None, formula=None, number_format=None)
            continue
        if record.type == "constant":
            cells[addr] = output_cell_from_record(record, py_by_addr)
            continue
        if record.type == "py_formula":
            report.normalized_py.append(addr)
            cells[addr] = output_cell_from_record(record, py_by_addr)
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
        if record.type not in ("formula", "error") or not record.formula:
            cells[addr] = output_cell_from_record(record, py_by_addr)
            continue

        ast = best_effort(lambda: parse_formula(normalize_lo_formula_for_parse(record.formula)), "parse formula")
        if ast is not None and not _has_function_node(ast):
            _pass_through(addr, record, None)
            continue

        translation = translate_formula(record.formula, cell_addr=addr)
        log.debug("build_converted_output_model translate cell %s formula=%r translation.ok=%s reason=%s code=%r", addr, record.formula, translation.ok, translation.reason, translation.code)
        if translation.ok and translation.code and translation.data_ranges is not None:
            cells[addr] = OutputCell(address=addr, value=None, formula=emit_py_formula(translation.code, translation.data_ranges, sheet_bounds=sheet_bounds, current_sheet=model.sheet_name), number_format=None)
            report.converted.append(addr)
        else:
            _pass_through(addr, record, translation.reason or "UNSUPPORTED_FUNCTION")

    output = OutputSheetModel(sheet_name=model.sheet_name, used_range=model.used_range, cells=cells, py_extracts=py_extracts)
    return output, report
