# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv symbolic compute — runs in user venv worker."""

from __future__ import annotations

import logging
from typing import Any

# Local copy of small pure value from the host facade. The worker must not import
# from plugin.scripting.* (those modules pull in host-only code and are not guaranteed
# to exist or be compatible in the user's configured venv interpreter).
HELPER_NAMES = frozenset(
    {
        "solve_equation",
        "symbolic_simplify",
        "integrate",
        "differentiate",
        "latex_to_math_object",
    }
)

log = logging.getLogger(__name__)

_PARSE_TRANSFORMS: Any | None = None


def _error_result(code: str, message: str, *, helper: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "error", "code": code, "message": message}
    if helper:
        out["helper"] = helper
    return out


def _ok_result(helper: str, *, latex: str, text: str = "", **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "ok",
        "helper": helper,
        "latex": latex,
        "text": text or latex,
        "writer_cleanup_hints": [],
        **extra,
    }
    return out


def _require_sympy(helper: str) -> Any | None:
    try:
        import sympy as sp
        return sp
    except ImportError:
        return None


def _parse_transformations() -> tuple[Any, ...]:
    global _PARSE_TRANSFORMS
    if _PARSE_TRANSFORMS is None:
        from sympy.parsing.sympy_parser import (
            implicit_multiplication_application,
            standard_transformations,
        )
        _PARSE_TRANSFORMS = standard_transformations + (implicit_multiplication_application,)
    return _PARSE_TRANSFORMS


def _parse_expression(expr: str, *, helper: str) -> Any:
    sp = _require_sympy(helper)
    if sp is None:
        raise ValueError("MISSING_PACKAGE")
    from sympy.parsing.sympy_parser import parse_expr

    text = str(expr or "").strip()
    if not text:
        raise ValueError("empty expression")
    try:
        return parse_expr(text, transformations=_parse_transformations())
    except Exception as exc:
        raise ValueError(f"Could not parse expression: {exc}") from exc


def _parse_variable(name: str, *, helper: str) -> Any:
    sp = _require_sympy(helper)
    if sp is None:
        raise ValueError("MISSING_PACKAGE")
    var = str(name or "x").strip() or "x"
    return sp.Symbol(var)


def _to_latex(sp: Any, value: Any) -> str:
    return str(sp.latex(value))


def _missing_package(helper: str) -> dict[str, Any]:
    return _error_result(
        "MISSING_PACKAGE",
        f"sympy is required for {helper}.",
        helper=helper,
    )


def symbolic_simplify(*, expression: str) -> dict[str, Any]:
    helper = "symbolic_simplify"
    sp = _require_sympy(helper)
    if sp is None:
        return _missing_package(helper)
    try:
        expr = _parse_expression(expression, helper=helper)
        simplified = sp.simplify(expr)
    except ValueError as exc:
        if str(exc) == "MISSING_PACKAGE":
            return _missing_package(helper)
        return _error_result("PARSE_ERROR", str(exc), helper=helper)
    except Exception as exc:
        return _error_result("SYMBOLIC_ERROR", str(exc), helper=helper)
    latex = _to_latex(sp, simplified)
    return _ok_result(helper, latex=latex, text=str(simplified))


def differentiate(*, expression: str, variable: str = "x") -> dict[str, Any]:
    helper = "differentiate"
    sp = _require_sympy(helper)
    if sp is None:
        return _missing_package(helper)
    try:
        expr = _parse_expression(expression, helper=helper)
        sym = _parse_variable(variable, helper=helper)
        result = sp.diff(expr, sym)
    except ValueError as exc:
        if str(exc) == "MISSING_PACKAGE":
            return _missing_package(helper)
        return _error_result("PARSE_ERROR", str(exc), helper=helper)
    except Exception as exc:
        return _error_result("SYMBOLIC_ERROR", str(exc), helper=helper)
    latex = _to_latex(sp, result)
    return _ok_result(helper, latex=latex, text=str(result), variable=variable)


def integrate_helper(*, expression: str, variable: str = "x", lower: str | None = None, upper: str | None = None) -> dict[str, Any]:
    helper = "integrate"
    sp = _require_sympy(helper)
    if sp is None:
        return _missing_package(helper)
    try:
        expr = _parse_expression(expression, helper=helper)
        sym = _parse_variable(variable, helper=helper)
        if lower is not None and upper is not None:
            a = _parse_expression(lower, helper=helper)
            b = _parse_expression(upper, helper=helper)
            result = sp.integrate(expr, (sym, a, b))
        else:
            result = sp.integrate(expr, sym)
    except ValueError as exc:
        if str(exc) == "MISSING_PACKAGE":
            return _missing_package(helper)
        return _error_result("PARSE_ERROR", str(exc), helper=helper)
    except Exception as exc:
        return _error_result("SYMBOLIC_ERROR", str(exc), helper=helper)
    latex = _to_latex(sp, result)
    return _ok_result(helper, latex=latex, text=str(result), variable=variable)


def solve_equation(*, equation: str, variable: str = "x") -> dict[str, Any]:
    helper = "solve_equation"
    sp = _require_sympy(helper)
    if sp is None:
        return _missing_package(helper)
    try:
        sym = _parse_variable(variable, helper=helper)
        text = str(equation or "").strip()
        if not text:
            return _error_result("MISSING_PARAM", "equation is required", helper=helper)
        if "=" in text:
            lhs_s, rhs_s = text.split("=", 1)
            lhs = _parse_expression(lhs_s, helper=helper)
            rhs = _parse_expression(rhs_s, helper=helper)
            eq = sp.Eq(lhs, rhs)
            solutions = sp.solve(eq, sym)
        else:
            expr = _parse_expression(text, helper=helper)
            solutions = sp.solve(expr, sym)
    except ValueError as exc:
        if str(exc) == "MISSING_PACKAGE":
            return _missing_package(helper)
        return _error_result("PARSE_ERROR", str(exc), helper=helper)
    except Exception as exc:
        return _error_result("SYMBOLIC_ERROR", str(exc), helper=helper)

    if not isinstance(solutions, list):
        solutions = [solutions]
    latex_parts = [_to_latex(sp, sol) for sol in solutions]
    latex = ", ".join(latex_parts) if latex_parts else ""
    text = ", ".join(str(s) for s in solutions)
    return _ok_result(
        helper,
        latex=latex,
        text=text,
        solutions=[str(s) for s in solutions],
        variable=variable,
    )


def latex_to_math_object(*, latex: str) -> dict[str, Any]:
    helper = "latex_to_math_object"
    sp = _require_sympy(helper)
    if sp is None:
        return _missing_package(helper)
    trimmed = str(latex or "").strip()
    if not trimmed:
        return _error_result("MISSING_PARAM", "latex is required", helper=helper)
    # Validate by attempting a lightweight parse when the input looks like plain SymPy syntax.
    if "=" not in trimmed and "\\" not in trimmed:
        try:
            expr = _parse_expression(trimmed, helper=helper)
            trimmed = _to_latex(sp, expr)
        except ValueError:
            pass
    return _ok_result(helper, latex=trimmed, text=trimmed)


def _dispatch_helper(name: str, params: dict[str, Any]) -> dict[str, Any]:
    if name == "symbolic_simplify":
        return symbolic_simplify(expression=str(params.get("expression") or ""))
    if name == "differentiate":
        return differentiate(
            expression=str(params.get("expression") or ""),
            variable=str(params.get("variable") or "x"),
        )
    if name == "integrate":
        return integrate_helper(
            expression=str(params.get("expression") or ""),
            variable=str(params.get("variable") or "x"),
            lower=params.get("lower"),
            upper=params.get("upper"),
        )
    if name == "solve_equation":
        return solve_equation(
            equation=str(params.get("equation") or ""),
            variable=str(params.get("variable") or "x"),
        )
    if name == "latex_to_math_object":
        return latex_to_math_object(latex=str(params.get("latex") or params.get("expression") or ""))
    return _error_result("UNKNOWN_HELPER", f"Unknown helper {name!r}", helper=name)


def run_symbolic(
    spec: dict[str, Any] | str,
    data: Any = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spec-driven dispatcher for trusted symbolic helpers."""
    del data, context  # reserved for future numeric substitution from sheet data
    if isinstance(spec, str):
        spec_dict: dict[str, Any] = {"helper": spec}
    elif isinstance(spec, dict):
        spec_dict = spec
    else:
        return _error_result("INVALID_SPEC", "spec must be a dict or helper name")

    helper = str(spec_dict.get("helper") or "").strip()
    if not helper:
        return _error_result("MISSING_HELPER", "helper is required")
    if helper not in HELPER_NAMES:
        return _error_result("UNKNOWN_HELPER", f"Unknown helper {helper!r}", helper=helper)

    params = spec_dict.get("params")
    if params is None:
        params = {k: v for k, v in spec_dict.items() if k != "helper"}
    if not isinstance(params, dict):
        params = {}
    return _dispatch_helper(helper, params)
