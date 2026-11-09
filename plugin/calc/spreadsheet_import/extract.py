# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Extract and normalize existing ``=PY()`` / ``=PYTHON()`` formulas."""

from __future__ import annotations

from plugin.calc.python.formula_edit import PyFormula


def normalize_py_formula(formula: str) -> str | None:
    """Rebuild *formula* as canonical ``=PY("…"; ranges…)`` with semicolon separators."""
    parsed = PyFormula.parse(formula)
    if parsed is None:
        return None
    return PyFormula.compose(parsed.code, parsed.data_args())
