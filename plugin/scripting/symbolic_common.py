# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared symbolic math constants — importable without sympy on the LO host."""
from __future__ import annotations

HELPER_NAMES = frozenset(
    {
        "solve_equation",
        "symbolic_simplify",
        "integrate",
        "differentiate",
        "latex_to_math_object",
    }
)

SYMBOLIC_VENV_PIP_INSTALL = "pip install sympy"
