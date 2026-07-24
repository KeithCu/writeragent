# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Extract and normalize existing ``=PY()`` / ``=PYTHON()`` formulas."""

from __future__ import annotations

import re

from plugin.calc.python.formula_edit import (
    format_data_binding_display,
    parse_data_binding_text,
    normalize_formula_string,
    parse_python_formula,
    rebuild_python_formula_with_data,
)
from plugin.calc.spreadsheet_import.models import PyCellExtract, SheetModel

# LibreOffice stores registered add-ins as fully qualified names in getFormula().
_ADDIN_PY_PREFIX_RE = re.compile(
    r"^=\s*ORG\.EXTENSION\.(?:WRITERAGENT|LIBREPY)\.PYTHONFUNCTION\.(?:PYTHON|PY)\s*\(",
    re.IGNORECASE,
)


def canonicalize_py_formula_for_parse(formula: str) -> str:
    """Map LO add-in formula text to ``=PYTHON(…)`` for ``parse_python_formula``."""
    raw = normalize_formula_string(formula)
    match = _ADDIN_PY_PREFIX_RE.match(raw)
    if match:
        return "=PYTHON(" + raw[match.end() :]
    return raw


def py_formula_semantics(formula: str) -> tuple[str, list[str]] | None:
    """Return ``(code, data_args)`` when *formula* is a strict PY/PYTHON call."""
    parts = parse_python_formula(canonicalize_py_formula_for_parse(formula))
    if parts is None:
        return None
    data_args = parse_data_binding_text(format_data_binding_display(parts.data_suffix))
    return parts.code, data_args


def is_py_formula_text(formula: str) -> bool:
    """True when *formula* is PY/PYTHON, including LO fully qualified add-in form."""
    from plugin.calc.python.formula_edit import cell_looks_python_like

    return cell_looks_python_like(canonicalize_py_formula_for_parse(formula))


def normalize_py_formula(formula: str) -> str | None:
    """Rebuild *formula* as canonical ``=PY("…"; ranges…)`` with semicolon separators."""
    semantics = py_formula_semantics(formula)
    if semantics is None:
        return None
    code, data_args = semantics
    return rebuild_python_formula_with_data(code, data_args, parts=None)


def extract_py_cells(model: SheetModel) -> list[PyCellExtract]:
    """Return normalized PY extracts for every parseable ``py_formula`` cell."""
    extracts: list[PyCellExtract] = []
    for addr in sorted(model.cells):
        cell = model.cells[addr]
        if cell.type != "py_formula" or not cell.formula:
            continue
        normalized = normalize_py_formula(cell.formula)
        if normalized is None:
            continue
        semantics = py_formula_semantics(normalized)
        assert semantics is not None
        code, data_args = semantics
        extracts.append(
            PyCellExtract(
                address=addr,
                original_formula=cell.formula,
                normalized_formula=normalized,
                code=code,
                data_args=data_args,
                changed=cell.formula != normalized,
            ),
        )
    return extracts
