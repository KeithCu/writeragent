# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted symbolic math helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from plugin.scripting.symbolic import run_symbolic

pytest.importorskip("sympy")


def test_run_symbolic_simplify():
    result = run_symbolic(
        {"helper": "symbolic_simplify", "params": {"expression": "(x + 1)**2 - x**2 - 2*x"}},
        None,
        {},
    )
    assert result["status"] == "ok"
    assert result["helper"] == "symbolic_simplify"
    assert result["text"] == "1"


def test_run_symbolic_solve_equation():
    result = run_symbolic(
        {"helper": "solve_equation", "params": {"equation": "x**2 - 4", "variable": "x"}},
        None,
        {},
    )
    assert result["status"] == "ok"
    assert len(result.get("solutions", [])) == 2


def test_run_symbolic_integrate():
    result = run_symbolic(
        {"helper": "integrate", "params": {"expression": "x", "variable": "x"}},
        None,
        {},
    )
    assert result["status"] == "ok"
    assert "x" in result["latex"].lower()


def test_run_symbolic_missing_package():
    with patch("plugin.scripting.venv.symbolic._require_sympy", return_value=None):
        result = run_symbolic({"helper": "symbolic_simplify", "params": {"expression": "x"}}, None, {})
    assert result["status"] == "error"
    assert result["code"] == "MISSING_PACKAGE"


def test_run_symbolic_parse_error():
    result = run_symbolic({"helper": "symbolic_simplify", "params": {"expression": "((("}}, None, {})
    assert result["status"] == "error"
    assert result["code"] == "PARSE_ERROR"


def test_overlong_expression_is_parse_error_without_evaluating():
    from plugin.scripting.venv.symbolic import _MAX_SYMBOLIC_EXPR_CHARS

    expr = "x+" * _MAX_SYMBOLIC_EXPR_CHARS
    result = run_symbolic({"helper": "symbolic_simplify", "params": {"expression": expr}}, None, {})
    assert result["status"] == "error"
    assert result["code"] == "PARSE_ERROR"
    assert "longer than" in result["message"]


def test_latex_to_math_object_rejects_unparsed_text():
    from plugin.scripting.venv.symbolic import latex_to_math_object

    result = latex_to_math_object(latex="(((")
    assert result["status"] == "error"
    assert result["code"] == "PARSE_ERROR"


def test_insert_symbolic_result_into_calc() -> None:
    from unittest.mock import MagicMock, patch
    from plugin.scripting.symbolic import insert_symbolic_result_into_calc

    doc = MagicMock()
    ctx = MagicMock()
    result = {"status": "ok", "tables": [{"columns": ["Expr"], "rows": [["x**2"]]}]}

    with patch("plugin.calc.tabular_egress.insert_tabular_result_into_calc", return_value=1) as mock_insert:
        res = insert_symbolic_result_into_calc(doc, ctx, result)
        assert res == 1
        mock_insert.assert_called_once()

