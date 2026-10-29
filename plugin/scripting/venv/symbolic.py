# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv symbolic compute — runs in the user venv worker.

Host ``run_symbolic`` uses the long trusted worker budget
(``LONG_TRUSTED_WORKER_TIMEOUT_SEC``, 300 seconds) and kills the worker if
sympy does not return. This module does not add a second timer.
"""

from __future__ import annotations

import functools
import inspect
import logging
import re
from typing import Any, Callable

from plugin.scripting.calc_functions_common import SYMBOLIC_HELPER_NAMES as HELPER_NAMES
from plugin.scripting.venv.trusted_dispatch import (
    error_result as _error_result,
    missing_package_error as _missing_package_error,
    ok_result as _ok_result,
    parse_trusted_spec as _parse_trusted_spec,
)

log = logging.getLogger(__name__)

_MAX_SYMBOLIC_EXPR_CHARS = 10_000

# Cues that mean "already LaTeX". ``x_1`` is kept; ``x1`` is parsed.
_LATEX_CUES = ("=", "\\", "{", "}", "^", "_")

_CALL_NAME = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")

_SYMPY_NAMES = (
    "Abs Add And E Eq Float Ge Gt I Integer Le Lt Mul Ne Not Or Pow Rational "
    "Symbol binomial cos exp factorial log oo pi sin sqrt tan "
    "asin acos atan atan2 sinh cosh tanh asinh acosh atanh "
    "sec csc cot floor ceiling Min Max gamma erf Mod sign re im "
    "Piecewise conjugate"
).split()
_KNOWN_CALL_NAMES = frozenset(_SYMPY_NAMES) | {"ln", "e"}


def _undefined_call_names(text: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for match in _CALL_NAME.finditer(text):
        name = match.group(1)
        if name in _KNOWN_CALL_NAMES:
            continue
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


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


def _param_text(value: Any) -> str:
    """Stringify a parameter. ``None`` becomes an empty string.

    ``value or ""`` treated JSON ``0`` and ``False`` as missing, so
    ``expression: 0`` became "empty expression" instead of the number zero.
    """
    if value is None:
        return ""
    return str(value)


def _is_blank_param(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


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


@functools.cache
def _sympy_name_dict(sp: Any) -> dict[str, Any]:
    """Names a formula may use. Not the process globals, so parse_expr cannot import.

    Cached on the sympy module. ``parse_expr`` writes ``local_dict``, not this
    dict. ``__builtins__`` is present so ``eval`` does not inject the real
    builtins when the key is missing. Callers must not mutate the dict.
    """
    names: dict[str, Any] = {"__builtins__": {}}
    # A name missing here is split into letters or fails to parse.
    for name in _SYMPY_NAMES:
        obj = getattr(sp, name, None)
        if obj is not None:
            names[name] = obj
    # ln is sp.log. e is Euler's number (sympy spells it E). getattr(sp, "e")
    # is None, and storing that None would make parse_expr treat e as missing.
    log_fn = getattr(sp, "log", None)
    if log_fn is not None:
        names["ln"] = log_fn
    euler = getattr(sp, "E", None)
    if euler is not None:
        names["e"] = euler
    return names


def _reject_unsafe_expression(text: str) -> None:
    """Reject source that ``parse_expr`` would eval.

    ``parse_expr`` compiles the string and evals it. A restricted global dict
    still runs attribute chains such as ``(1).__class__`` before we can look
    at the result, and ``__subclasses__()`` had already executed on that path.
    Dunder names are not math. A dot is a decimal only when the character
    before it or after it is a digit (``1.5``, ``.5``, ``1.``, ``x.5``).
    """
    if "__" in text:
        raise _SymbolicError("PARSE_ERROR", "Dunder attribute access is not allowed in expressions.")
    for index, char in enumerate(text):
        if char != ".":
            continue
        prev_digit = index > 0 and text[index - 1].isdigit()
        next_digit = index + 1 < len(text) and text[index + 1].isdigit()
        if not prev_digit and not next_digit:
            raise _SymbolicError("PARSE_ERROR", "Attribute access is not allowed in expressions.")


def _parse_expression(sp: Any, expr: Any, *, variable: Any | None = None) -> Any:
    text = _param_text(expr).strip()
    if not text:
        raise _SymbolicError("PARSE_ERROR", "empty expression")
    if len(text) > _MAX_SYMBOLIC_EXPR_CHARS:
        raise _SymbolicError("PARSE_ERROR", f"expression longer than {_MAX_SYMBOLIC_EXPR_CHARS} characters")
    _reject_unsafe_expression(text)
    from sympy.parsing.sympy_parser import parse_expr

    local_dict: dict[str, Any] = {}
    if variable is not None:
        var_name = str(variable)
        # local_dict wins over the global name, so a variable named e stays a
        # symbol instead of Euler's number.
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
        # sympy rewrites an unknown call to Function('name')(...); eval then
        # raises NameError. Which name is undefined is in the source, not in
        # the exception text, so do not match the message wording.
        if isinstance(exc, NameError):
            names = _undefined_call_names(text)
            if names:
                raise _SymbolicError("PARSE_ERROR", f"Undefined function in expression: {', '.join(names)}") from exc
        raise _SymbolicError("PARSE_ERROR", f"Could not parse expression: {exc}") from exc

    # parse_expr returns a Python str, list, tuple, or dict for quotes, brackets,
    # and braces, and returns the Float class for 1.0.func. Those objects have no
    # .atoms. The wrapper reported SYMBOLIC_ERROR with 'str' object has no attribute
    # 'atoms'. They are not expressions, so the failure is PARSE_ERROR.
    if not isinstance(parsed, sp.Basic):
        raise _SymbolicError("PARSE_ERROR", "Not a math expression.")

    return parsed


def _parse_variable(sp: Any, name: Any) -> Any:
    var = _param_text(name).strip() or "x"
    return sp.Symbol(var)


def _public_signature(func: Callable[..., Any]) -> inspect.Signature:
    """Drop ``sp``. Callers and generated templates never pass the module."""
    raw = inspect.signature(func)
    params = [param for name, param in raw.parameters.items() if name != "sp"]
    return raw.replace(parameters=params)


_HELPERS: dict[str, Callable[..., dict[str, Any]]] = {}


def _symbolic_helper(func: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Resolve sympy once and turn helper failures into result dicts.

    The wrapper passes the module in, so helpers do not look it up again and
    do not assert on it. Those asserts disappeared under ``python -O``.
    Parse failures raise ``_SymbolicError`` directly. A ``ValueError`` from
    ``simplify``, ``integrate``, or ``solve`` used to be labeled PARSE_ERROR
    because this wrapper caught every ValueError from the whole body.
    """
    public_sig = _public_signature(func)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        helper = func.__name__
        sp = _require_sympy()
        if sp is None:
            return _missing_package_error(helper, "sympy")
        try:
            return func(sp, *args, **kwargs)
        except _SymbolicError as exc:
            return _error_result(exc.code, exc.message, helper=helper)
        except Exception as exc:
            return _error_result("SYMBOLIC_ERROR", str(exc), helper=helper)

    # inspect.signature follows __wrapped__ unless this is set, and would
    # then require `sp`. Run Python Script templates bind the public kwargs.
    setattr(wrapper, "__signature__", public_sig)
    _HELPERS[func.__name__] = wrapper
    return wrapper


@_symbolic_helper
def symbolic_simplify(sp: Any, *, expression: Any) -> dict[str, Any]:
    """Simplify a symbolic expression."""
    expr = _parse_expression(sp, expression)
    simplified = sp.simplify(expr)
    latex = str(sp.latex(simplified))
    return _ok_result("symbolic_simplify", latex=latex, text=str(simplified), writer_cleanup_hints=[])


@_symbolic_helper
def differentiate(sp: Any, *, expression: Any, variable: Any = "x") -> dict[str, Any]:
    """Differentiate *expression* with respect to *variable* (default ``x``).

    ``e`` is Euler's number. A variable named ``e`` stays that symbol.
    """
    sym = _parse_variable(sp, variable)
    expr = _parse_expression(sp, expression, variable=sym)
    result = sp.diff(expr, sym)
    latex = str(sp.latex(result))
    # Echo str(sym), the stripped name. The raw parameter can still contain spaces.
    return _ok_result("differentiate", latex=latex, text=str(result), variable=str(sym), writer_cleanup_hints=[])


@_symbolic_helper
def integrate(
    sp: Any,
    *,
    expression: Any,
    variable: Any = "x",
    lower: Any = None,
    upper: Any = None,
) -> dict[str, Any]:
    """Integrate an expression. Pass both bounds for a definite integral."""
    sym = _parse_variable(sp, variable)
    expr = _parse_expression(sp, expression, variable=sym)
    has_lower = lower is not None and str(lower).strip() != ""
    has_upper = upper is not None and str(upper).strip() != ""
    if has_lower != has_upper:
        # Exactly one bound is MISSING_PARAM. Both absent is indefinite;
        # both present is definite.
        raise _SymbolicError("MISSING_PARAM", "Both lower and upper bounds must be provided for definite integration.")

    if has_lower:
        a = _parse_expression(sp, str(lower), variable=sym)
        b = _parse_expression(sp, str(upper), variable=sym)
        result = sp.integrate(expr, (sym, a, b))
    else:
        result = sp.integrate(expr, sym)

    # integrate returns Integral(...) when it has no closed form and the
    # helper used to report that object as success.
    if result.has(sp.Integral):
        raise _SymbolicError("NO_CLOSED_FORM", f"No closed form: {result}")

    latex = str(sp.latex(result))
    return _ok_result("integrate", latex=latex, text=str(result), variable=str(sym), writer_cleanup_hints=[])


@_symbolic_helper
def solve_equation(sp: Any, *, equation: Any, variable: Any = "x") -> dict[str, Any]:
    """Solve one equation for *variable*.

    Use ``=`` or an expression equal to zero. Inequalities and ``==`` are
    rejected. ``!=`` is one token, including ``n!=720``, so that is not
    factorial. Factorial equations need a space before ``=`` (``n! = 720``).
    SymPy may still report that it has no algorithm for those.
    """
    sym = _parse_variable(sp, variable)
    text = _param_text(equation).strip()
    if not text:
        raise _SymbolicError("MISSING_PARAM", "equation is required")
    # Inequalities and '==' are INVALID_PARAMS. split("=", 1) would cut
    # '<=' and '==' into pieces parse_expr cannot accept. `!=` stays rejected:
    # the tokenizer fuses it, so factorial_notation never sees a bare `!`.
    if re.search(r"<=|>=|!=|==|<|>", text):
        raise _SymbolicError("INVALID_PARAMS", "Inequalities and '==' are not supported in solve_equation; use '=' or expression equal to zero.")
    if "=" in text:
        parts = text.split("=")
        if len(parts) > 2:
            raise _SymbolicError("INVALID_PARAMS", "Equation cannot contain multiple '=' signs.")
        lhs = _parse_expression(sp, parts[0], variable=sym)
        rhs = _parse_expression(sp, parts[1], variable=sym)
        target = sp.Eq(lhs, rhs)
    else:
        target = _parse_expression(sp, text, variable=sym)

    solutions = sp.solve(target, sym)

    if not isinstance(solutions, list):
        solutions = [solutions]

    # sp.solve returns [] when nothing is a root (exp(x), 1 = 0) and when every
    # complex value works (x = x, 0). The success dict then had latex "", and
    # Writer raised "Symbolic helper returned no LaTeX." solveset on that empty list
    # separates EmptySet (NO_SOLUTION) from any other set, which is returned as
    # one latex value so egress has a formula. A non-empty solve list is still the
    # explicit roots (sin(x) stays 0, pi).
    if not solutions:
        solution_set = sp.solveset(target, sym, domain=sp.S.Complexes)
        if solution_set == sp.S.EmptySet:
            raise _SymbolicError("NO_SOLUTION", "No solution.")
        latex = str(sp.latex(solution_set))
        text_sol = str(solution_set)
        solution_strs = [text_sol]
    else:
        latex = ", ".join(str(sp.latex(sol)) for sol in solutions)
        text_sol = ", ".join(str(s) for s in solutions)
        solution_strs = [str(s) for s in solutions]
    return _ok_result(
        "solve_equation",
        latex=latex,
        text=text_sol,
        solutions=solution_strs,
        variable=str(sym),
        writer_cleanup_hints=[],
    )


@_symbolic_helper
def latex_to_math_object(sp: Any, *, latex: Any) -> dict[str, Any]:
    """Normalize a math string for Writer Math insert.

    A LaTeX cue (``=``, ``\\``, ``{``, ``}``, ``^``, ``_``) keeps the text
    unchanged as both ``latex`` and ``text``. Anything else is parsed and
    re-emitted as LaTeX. ``x_1`` is kept because of ``_``. ``x1`` and ``a b``
    have no cue, so they are parsed (``a b`` becomes a product).
    """
    trimmed = _param_text(latex).strip()
    if not trimmed:
        raise _SymbolicError("MISSING_PARAM", "latex is required")
    if len(trimmed) > _MAX_SYMBOLIC_EXPR_CHARS:
        raise _SymbolicError("PARSE_ERROR", f"latex longer than {_MAX_SYMBOLIC_EXPR_CHARS} characters")
    if not any(cue in trimmed for cue in _LATEX_CUES):
        expr = _parse_expression(sp, trimmed)
        trimmed = str(sp.latex(expr))
    return _ok_result("latex_to_math_object", latex=trimmed, text=trimmed, writer_cleanup_hints=[])


def _dispatch_helper(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Call *name* with the parameters that function accepts.

    The old lambdas repeated ``str(p.get(...) or "x")``. Defaults live on the
    functions. ``None`` for an optional parameter keeps that default, so JSON
    ``0`` is passed through. A missing required string is ``""``, which is
    what those lambdas passed, so the helper's own empty check still runs.
    """
    func = _HELPERS.get(name)
    if func is None:
        return _error_result("UNKNOWN_HELPER", f"Unknown helper {name!r}", helper=name)
    # Public signature only. The wrapper injects sympy and hides `sp`.
    sig = inspect.signature(func)
    kwargs: dict[str, Any] = {}
    for pname, param in sig.parameters.items():
        value = params.get(pname)
        if value is None:
            if param.default is not inspect.Parameter.empty:
                continue
            value = ""
        kwargs[pname] = value
    if name == "latex_to_math_object" and _is_blank_param(kwargs.get("latex")):
        # Callers used to send the formula as `expression`. A blank latex
        # string fell through via `or`. Numeric 0 does not.
        expression = params.get("expression")
        if not _is_blank_param(expression):
            kwargs["latex"] = expression
    return func(**kwargs)


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
    helper, params, _headers, _header_row, ctx = parsed

    result = _dispatch_helper(helper, params)
    if result.get("status") == "ok" and ctx:
        result["context"] = {k: v for k, v in ctx.items() if k in ("sheet_name", "range_a1", "task_hint")}
    return result
