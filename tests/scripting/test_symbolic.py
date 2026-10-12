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


@pytest.mark.parametrize(
    "expression, value, expected",
    [
        pytest.param("atan(x)", "1/(x**2 + 1)", "\\frac{1}{x^{2} + 1}", id="test_differentiate_atan"),
        pytest.param("ln(x)", "1/x", "\\frac{1}{x}", id="test_differentiate_ln"),
    ],
)
def test_differentiate_atan(expression, value, expected):
    from plugin.scripting.venv.symbolic import differentiate

    res = differentiate(expression=expression, variable="x")
    assert res["status"] == "ok"
    assert value in res["text"] or res["latex"] == expected

@pytest.mark.parametrize(
    "expression, expected, value, expected_2",
    [
        pytest.param("x^2", "ok", "text", "2*x", id="test_differentiate_xor_power"),
        pytest.param("custom_fn(x)", "error", "code", "PARSE_ERROR", id="test_undefined_function_is_parse_error"),
    ],
)
def test_differentiate_xor_power(expression, expected, value, expected_2):
    from plugin.scripting.venv.symbolic import differentiate

    res = differentiate(expression=expression, variable="x")
    assert res["status"] == expected
    assert res[value] == expected_2


def test_differentiate_multi_letter_variable():
    from plugin.scripting.venv.symbolic import differentiate

    res_rate = differentiate(expression="rate*x", variable="rate")
    assert res_rate["status"] == "ok"
    assert res_rate["text"] == "x"

    res_x = differentiate(expression="rate*x", variable="x")
    assert res_x["status"] == "ok"
    assert res_x["text"] == "rate"


def test_latex_to_math_object_preserves_latex_cues():
    from plugin.scripting.venv.symbolic import latex_to_math_object

    res_pow = latex_to_math_object(latex="x^{2}")
    assert res_pow["status"] == "ok"
    assert res_pow["latex"] == "x^{2}"

    res_sub = latex_to_math_object(latex="x_1 + y_2")
    assert res_sub["status"] == "ok"
    assert res_sub["latex"] == "x_1 + y_2"


def test_integrate_one_sided_bounds_error():
    from plugin.scripting.venv.symbolic import integrate

    res_lower = integrate(expression="x", variable="x", lower="0")
    assert res_lower["status"] == "error"
    assert res_lower["code"] == "MISSING_PARAM"

    res_upper = integrate(expression="x", variable="x", upper="1")
    assert res_upper["status"] == "error"
    assert res_upper["code"] == "MISSING_PARAM"


def test_integrate_two_sided_bounds():
    from plugin.scripting.venv.symbolic import integrate

    res = integrate(expression="x", variable="x", lower="0", upper="1")
    assert res["status"] == "ok"
    assert res["text"] == "1/2"


def test_solve_equation_rejects_inequalities_and_double_equals():
    from plugin.scripting.venv.symbolic import solve_equation

    for eq in ("x <= 2", "x >= 2", "x != 2", "x == 2", "x < 2", "x > 2"):
        res = solve_equation(equation=eq, variable="x")
        assert res["status"] == "error"
        assert res["code"] == "INVALID_PARAMS"

    res_multi = solve_equation(equation="x = 1 = 2", variable="x")
    assert res_multi["status"] == "error"
    assert res_multi["code"] == "INVALID_PARAMS"


def test_variable_returned_stripped():
    from plugin.scripting.venv.symbolic import differentiate, integrate, solve_equation

    res_diff = differentiate(expression="x", variable=" x ")
    assert res_diff["variable"] == "x"

    res_int = integrate(expression="x", variable=" x ")
    assert res_int["variable"] == "x"

    res_sol = solve_equation(equation="x = 1", variable=" x ")
    assert res_sol["variable"] == "x"

def test_is_symbolic_result():
    from plugin.scripting.symbolic import is_symbolic_result

    assert is_symbolic_result({"status": "ok", "helper": "differentiate"}) is True
    assert is_symbolic_result({"status": "error", "code": "SYMBOLIC_ERROR"}) is True
    assert is_symbolic_result({"status": "ok", "helper": "unknown_helper"}) is False
    assert is_symbolic_result({"status": "ok", "latex": "x^2"}) is False
    assert is_symbolic_result({"status": "error", "code": "OTHER_ERROR"}) is False


def test_differentiate_euler_e():
    from plugin.scripting.venv.symbolic import differentiate

    res = differentiate(expression="e^x", variable="x")
    assert res["status"] == "ok"
    assert res["text"] == "E**x"

    # A variable named e overrides Euler's number in the name dict.
    res_var = differentiate(expression="e*x", variable="e")
    assert res_var["status"] == "ok"
    assert res_var["text"] == "x"
    assert res_var["variable"] == "e"


def test_attribute_access_is_parse_error_without_eval():
    from plugin.scripting.venv.symbolic import differentiate, symbolic_simplify

    payloads = (
        "(1).__class__",
        "(1).__class__.__bases__[0].__subclasses__()",
        "x.real",
    )
    for payload in payloads:
        with patch("sympy.parsing.sympy_parser.parse_expr") as parse_expr:
            res = differentiate(expression=payload, variable="x")
        assert res["status"] == "error"
        assert res["code"] == "PARSE_ERROR"
        parse_expr.assert_not_called()

    decimal = symbolic_simplify(expression="1.5")
    assert decimal["status"] == "ok"
    implicit = symbolic_simplify(expression="x.5")
    assert implicit["status"] == "ok"
    assert "x" in implicit["text"]


def test_numeric_zero_is_not_empty():
    from plugin.scripting.venv.symbolic import symbolic_simplify

    direct = symbolic_simplify(expression=0)
    assert direct["status"] == "ok"
    assert direct["text"] == "0"

    via_run = run_symbolic(
        {"helper": "symbolic_simplify", "params": {"expression": 0}},
        None,
        {},
    )
    assert via_run["status"] == "ok"
    assert via_run["text"] == "0"


def test_sympy_valueerror_is_symbolic_error():
    import sympy as sp

    from plugin.scripting.venv.symbolic import symbolic_simplify

    with patch.object(sp, "simplify", side_effect=ValueError("internal boom")):
        res = symbolic_simplify(expression="x")
    assert res["status"] == "error"
    assert res["code"] == "SYMBOLIC_ERROR"
    assert "internal boom" in res["message"]


def test_helpers_match_helper_names():
    from plugin.scripting.calc_functions_common import SYMBOLIC_HELPER_NAMES
    from plugin.scripting.venv.symbolic import _HELPERS

    assert set(_HELPERS) == set(SYMBOLIC_HELPER_NAMES)


def test_factorial_equation_needs_a_space_before_equals():
    from plugin.scripting.venv.symbolic import solve_equation

    # `!=` is one token. factorial_notation only rewrites a bare `!`.
    for equation in ("n!=720", "5!=120"):
        res = solve_equation(equation=equation, variable="n")
        assert res["status"] == "error"
        assert res["code"] == "INVALID_PARAMS"

    spaced = solve_equation(equation="n! = 720", variable="n")
    assert spaced["status"] == "error"
    assert spaced["code"] == "SYMBOLIC_ERROR"


def test_unevaluated_integral_is_no_closed_form():
    from plugin.scripting.venv.symbolic import integrate

    res = integrate(expression="exp(sin(x))", variable="x")
    assert res["status"] == "error"
    assert res["code"] == "NO_CLOSED_FORM"

    si = integrate(expression="sin(x)/x", variable="x")
    assert si["status"] == "ok"
    assert "Si" in si["text"]


def test_solve_no_roots_is_empty_list():
    from plugin.scripting.venv.symbolic import solve_equation

    res = solve_equation(equation="exp(x)", variable="x")
    assert res["status"] == "ok"
    assert res["solutions"] == []


def test_latex_to_math_object_accepts_expression_alias():
    res = run_symbolic(
        {"helper": "latex_to_math_object", "params": {"expression": "sin(x)"}},
        None,
        {},
    )
    assert res["status"] == "ok"
    assert "sin" in res["latex"].lower()

