# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Parse-only Calc/Excel formula AST (vendored xlcalculator slice)."""

from __future__ import annotations

from plugin.contrib.calc_formula_parser.ast_nodes import (
    ASTNode,
    FunctionNode,
    OperandNode,
    OperatorNode,
    RangeNode,
)
from plugin.contrib.calc_formula_parser.parser import FormulaParser

_PARSER = FormulaParser()


def parse_formula(formula: str, *, named_ranges: dict[str, str] | None = None) -> ASTNode:
    """Parse *formula* (with leading ``=``) into an AST."""
    return _PARSER.parse(formula, named_ranges=named_ranges)


__all__ = [
    "ASTNode",
    "FormulaParser",
    "FunctionNode",
    "OperandNode",
    "OperatorNode",
    "RangeNode",
    "parse_formula",
]
