# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for demo case python_expr AST validity in NumPy domains demo cases."""

import ast

from tests.calc.numpy_domains_demo_cases import (
    all_domain_demo_cases,
    matplotlib_demo_blocks,
)


def test_all_demo_cases_python_expr_valid_ast() -> None:
    cases = all_domain_demo_cases()
    assert cases, "Expected demo cases to be defined"
    for case in cases:
        if case.python_expr:
            try:
                ast.parse(case.python_expr)
            except SyntaxError as exc:
                raise AssertionError(
                    f"Case '{case.id}' python_expr failed to parse as valid Python AST: {exc}\nExpr: {case.python_expr}"
                ) from exc


def test_matplotlib_demo_blocks_python_expr_valid_ast() -> None:
    blocks = matplotlib_demo_blocks()
    assert blocks, "Expected matplotlib demo blocks to be defined"
    for block in blocks:
        if block.python_expr:
            try:
                ast.parse(block.python_expr)
            except SyntaxError as exc:
                raise AssertionError(
                    f"Block '{block.id}' python_expr failed to parse as valid Python AST: {exc}\nExpr: {block.python_expr}"
                ) from exc
