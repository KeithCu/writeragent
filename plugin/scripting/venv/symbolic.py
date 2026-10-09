# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv symbolic compute — runs in user venv worker."""

from __future__ import annotations

import functools
import logging
import re
from typing import Any, Callable

from plugin.scripting.calc_functions_common import SYMBOLIC_HELPER_NAMES as HELPER_NAMES
from plugin.scripting.venv.coerce import (
    error_result as _error_result,
    missing_package_error as _missing_package_error,
    ok_result as _ok_result,
    parse_trusted_spec as _parse_trusted_spec,
)

log = logging.getLogger(__name__)

_MAX_SYMBOLIC_EXPR_CHARS = 10_000


class _SymbolicError(Exception):
    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _require_sympy() -> Any | None:
    try:
        import sympy as sp

        return sp
    except ImportError:
        return None


@functools.cache
def _parse_transformations() -> tuple[Any, ...]:
    # x^2 is power, not XOR, and a multi-letter name stays one symbol.
    # convert_xor plus implicit_multiplication and implicit_application;
    # split_symbols would break names such as price.
    from sympy.parsing.sympy_parser import (
        convert_xor,
        implicit_application,
        implicit_multiplication,
        standard_transformations,
    )

    return standard_transformations + (convert_xor, implicit_multiplication, implicit_application)


def _sympy_name_dict(sp: Any) -> dict[str, Any]:
    """Names a formula may use. Not the process globals, so parse_expr cannot import."""
    names: dict[str, Any] = {}
    # Names a formula may call. ln is sp.log. A name missing here is split
    # into letters or fails to parse.
    for name in (
        "Abs",
        "Add",
        "And",
        "E",
        "Eq",
        "Float",
        "Ge",
        "Gt",
        "I",
        "Integer",
        "Le",
        "Lt",
        "Mul",
        "Ne",
        "Not",
        "Or",
        "Pow",
        "Rational",
        "Symbol",
        "binomial",
        "cos",
        "exp",
        "factorial",
        "log",
        "oo",
        "pi",
        "sin",
        "sqrt",
        "tan",
        "asin",
        "acos",
        "atan",
        "atan2",
        "sinh",
        "cosh",
        "tanh",
        "asinh",
        "acosh",
        "atanh",
        "sec",
        "csc",
        "cot",
        "floor",
        "ceiling",
        "Min",
        "Max",
        "gamma",
        "erf",
        "Mod",
        "sign",
        "re",
        "im",
        "Piecewise",
        "conjugate",
    ):
        obj = getattr(sp, name, None)
        if obj is not None:
            names[name] = obj
    names["ln"] = getattr(sp, "log", None)
    return names


def _parse_expression(sp: Any, expr: str, *, variable: Any | None = None) -> Any:
    text = str(expr or "").strip()
    if not text:
        raise ValueError("empty expression")
    if len(text) > _MAX_SYMBOLIC_EXPR_CHARS:
        raise ValueError(f"expression longer than {_MAX_SYMBOLIC_EXPR_CHARS} characters")
    from sympy.core.function import AppliedUndef
    from sympy.parsing.sympy_parser import parse_expr

    local_dict: dict[str, Any] = {}
    if variable is not None:
        var_name = str(variable)
        local_dict[var_name] = sp.Symbol(var_name)

    try:
        parsed = parse_expr(
            text,
            local_dict=local_dict,
            global_dict=_sympy_name_dict(sp),
            transformations=_parse_transformations(),
            evaluate=False,
        )
    except Exception as exc:
        raise ValueError(f"Could not parse expression: {exc}") from exc

    # parse_expr turns an unknown call into AppliedUndef and would return ok.
    # Reject those atoms so the caller sees PARSE_ERROR.
    if parsed.atoms(AppliedUndef):
        undef = ", ".join(str(a.func) for a in parsed.atoms(AppliedUndef))
        raise ValueError(f"Undefined function in expression: {undef}")

    return parsed


def _parse_variable(sp: Any, name: str) -> Any:
    var = str(name or "x").strip() or "x"
    return sp.Symbol(var)


def _to_latex(sp: Any, value: Any) -> str:
    return str(sp.latex(value))


def _symbolic_helper(func: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """One exception-to-result wrapper for symbolic helpers."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        helper = func.__name__
        sp = _require_sympy()
        if sp is None:
            return _missing_package_error(helper, "sympy")
        try:
            return func(*args, **kwargs)
        except _SymbolicError as exc:
            return _error_result(exc.code, exc.message, helper=helper)
        except ValueError as exc:
            return _error_result("PARSE_ERROR", str(exc), helper=helper)
        except Exception as exc:
            return _error_result("SYMBOLIC_ERROR", str(exc), helper=helper)

    return wrapper


@_symbolic_helper
def symbolic_simplify(*, expression: str) -> dict[str, Any]:
    sp = _require_sympy()
    assert sp is not None
    expr = _parse_expression(sp, expression)
    simplified = sp.simplify(expr)
    latex = _to_latex(sp, simplified)
    return _ok_result("symbolic_simplify", latex=latex, text=str(simplified), writer_cleanup_hints=[])


@_symbolic_helper
def differentiate(*, expression: str, variable: str = "x") -> dict[str, Any]:
    sp = _require_sympy()
    assert sp is not None
    sym = _parse_variable(sp, variable)
    expr = _parse_expression(sp, expression, variable=sym)
    result = sp.diff(expr, sym)
    latex = _to_latex(sp, result)
    # Echo str(sym), the stripped name. The raw parameter can still contain spaces.
    return _ok_result("differentiate", latex=latex, text=str(result), variable=str(sym), writer_cleanup_hints=[])


@_symbolic_helper
def integrate(
    *,
    expression: str,
    variable: str = "x",
    lower: str | None = None,
    upper: str | None = None,
) -> dict[str, Any]:
    sp = _require_sympy()
    assert sp is not None
    sym = _parse_variable(sp, variable)
    expr = _parse_expression(sp, expression, variable=sym)
    has_lower = lower is not None and str(lower).strip() != ""
    has_upper = upper is not None and str(upper).strip() != ""
    if has_lower != has_upper:
        # Exactly one bound is MISSING_PARAM. Both absent is indefinite;
        # both present is definite.
        raise _SymbolicError("MISSING_PARAM", "Both lower and upper bounds must be provided for definite integration.")

    if has_lower and has_upper:
        a = _parse_expression(sp, str(lower), variable=sym)
        b = _parse_expression(sp, str(upper), variable=sym)
        result = sp.integrate(expr, (sym, a, b))
    else:
        result = sp.integrate(expr, sym)

    latex = _to_latex(sp, result)
    return _ok_result("integrate", latex=latex, text=str(result), variable=str(sym), writer_cleanup_hints=[])


integrate_helper = integrate


@_symbolic_helper
def solve_equation(*, equation: str, variable: str = "x") -> dict[str, Any]:
    sp = _require_sympy()
    assert sp is not None
    sym = _parse_variable(sp, variable)
    text = str(equation or "").strip()
    if not text:
        raise _SymbolicError("MISSING_PARAM", "equation is required")
    # Inequalities and '==' are INVALID_PARAMS. split("=", 1) would cut
    # '<=' and '==' into pieces parse_expr cannot accept.
    if re.search(r"<=|>=|!=|==|<|>", text):
        raise _SymbolicError("INVALID_PARAMS", "Inequalities and '==' are not supported in solve_equation; use '=' or expression equal to zero.")
    if "=" in text:
        parts = text.split("=")
        if len(parts) > 2:
            raise _SymbolicError("INVALID_PARAMS", "Equation cannot contain multiple '=' signs.")
        lhs = _parse_expression(sp, parts[0], variable=sym)
        rhs = _parse_expression(sp, parts[1], variable=sym)
        eq = sp.Eq(lhs, rhs)
        solutions = sp.solve(eq, sym)
    else:
        expr = _parse_expression(sp, text, variable=sym)
        solutions = sp.solve(expr, sym)

    if not isinstance(solutions, list):
        solutions = [solutions]
    latex_parts = [_to_latex(sp, sol) for sol in solutions]
    latex = ", ".join(latex_parts) if latex_parts else ""
    text_sol = ", ".join(str(s) for s in solutions)
    return _ok_result(
        "solve_equation",
        latex=latex,
        text=text_sol,
        solutions=[str(s) for s in solutions],
        variable=str(sym),
        writer_cleanup_hints=[],
    )


@_symbolic_helper
def latex_to_math_object(*, latex: str) -> dict[str, Any]:
    trimmed = str(latex or "").strip()
    if not trimmed:
        raise _SymbolicError("MISSING_PARAM", "latex is required")
    # '{', '}', '^', and '_' are LaTeX, same as '=' and '\\'. x^{2} or x_1
    # sent through parse_expr is mangled.
    if not any(cue in trimmed for cue in ("=", "\\", "{", "}", "^", "_")):
        sp = _require_sympy()
        assert sp is not None
        expr = _parse_expression(sp, trimmed)
        trimmed = _to_latex(sp, expr)
    return _ok_result("latex_to_math_object", latex=trimmed, text=trimmed, writer_cleanup_hints=[])


_HELPERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "symbolic_simplify": lambda p: symbolic_simplify(expression=str(p.get("expression") or "")),
    "differentiate": lambda p: differentiate(
        expression=str(p.get("expression") or ""),
        variable=str(p.get("variable") or "x"),
    ),
    "integrate": lambda p: integrate(
        expression=str(p.get("expression") or ""),
        variable=str(p.get("variable") or "x"),
        lower=p.get("lower"),
        upper=p.get("upper"),
    ),
    "solve_equation": lambda p: solve_equation(
        equation=str(p.get("equation") or ""),
        variable=str(p.get("variable") or "x"),
    ),
    "latex_to_math_object": lambda p: latex_to_math_object(
        latex=str(p.get("latex") or p.get("expression") or ""),
    ),
}


def _dispatch_helper(name: str, params: dict[str, Any]) -> dict[str, Any]:
    handler = _HELPERS.get(name)
    if handler is None:
        return _error_result("UNKNOWN_HELPER", f"Unknown helper {name!r}", helper=name)
    return handler(params)


def run_symbolic(
    spec: dict[str, Any] | str,
    data: Any = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spec-driven dispatcher for trusted symbolic helpers."""
    del data  # reserved for future numeric substitution from sheet data
    parsed = _parse_trusted_spec(spec, helper_names=HELPER_NAMES, context=context)
    if isinstance(parsed, dict):
        return parsed
    helper, params, _headers, _header_row, ctx, _spec = parsed

    result = _dispatch_helper(helper, params)
    if isinstance(result, dict) and result.get("status") == "ok" and ctx:
        result["context"] = {k: v for k, v in ctx.items() if k in ("sheet_name", "range_a1", "task_hint")}
    return result
