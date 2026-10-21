# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Extract and normalize existing ``=PY()`` / ``=PYTHON()`` formulas."""

from __future__ import annotations

from plugin.calc.python.cell_discovery import canonicalize_py_formula_for_parse
from plugin.calc.python.formula_edit import format_data_binding_display, parse_data_binding_text, parse_python_formula, rebuild_python_formula_with_data
from plugin.calc.spreadsheet_import.models import PyCellExtract, SheetModel


def py_formula_semantics(formula: str) -> tuple[str, list[str]] | None:
    """Return ``(code, data_args)`` when *formula* is a strict PY/PYTHON call."""
    parts = parse_python_formula(canonicalize_py_formula_for_parse(formula))
    if parts is None:
        return None
    data_args = parse_data_binding_text(format_data_binding_display(parts.data_suffix))
    return parts.code, data_args


def _normalized_py(formula: str) -> tuple[str, str, list[str]] | None:
    semantics = py_formula_semantics(formula)
    if semantics is None:
        return None
    code, data_args = semantics
    normalized = rebuild_python_formula_with_data(code, data_args, parts=None)
    return normalized, code, data_args


def normalize_py_formula(formula: str) -> str | None:
    """Rebuild *formula* as canonical ``=PY("…"; ranges…)`` with semicolon separators."""
    norm = _normalized_py(formula)
    return norm[0] if norm is not None else None


def extract_py_cells(model: SheetModel) -> list[PyCellExtract]:
    """Return normalized PY extracts for every parseable ``py_formula`` cell."""
    extracts: list[PyCellExtract] = []
    for addr in sorted(model.cells):
        cell = model.cells[addr]
        if cell.type != "py_formula" or not cell.formula:
            continue
        norm = _normalized_py(cell.formula)
        if norm is None:
            continue
        normalized, code, data_args = norm
        extracts.append(PyCellExtract(address=addr, original_formula=cell.formula, normalized_formula=normalized, code=code, data_args=data_args, changed=cell.formula != normalized))
    return extracts
