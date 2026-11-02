"""Contracts sidecar for plugin.calc.python.formula_edit."""

from __future__ import annotations

from typing import Any
import deal

from plugin.calc.python import formula_edit as _prod
from plugin.calc.python.formula_edit import PythonFormulaParts
from tests.contracts.bounds import (
    DEAL_MAX_SHAPE_DIM,
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    UNDER_CROSSHAIR,
    ascii_bounded,
    inverse_ensure,
    str_bounded,
)

_RANGE_ADDR_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.!:'$_ \"")
_DEAL_ASCII_STR = ascii_bounded if UNDER_CROSSHAIR else str_bounded


def _deal_range_addr_ok(s: object) -> bool:
    return isinstance(s, str) and len(s) <= DEAL_MAX_TOKEN and all(c in _RANGE_ADDR_CHARS for c in s)


def _deal_data_args_ok(data_args: object) -> bool:
    return (
        isinstance(data_args, list)
        and len(data_args) <= DEAL_MAX_SHAPE_DIM
        and all(_deal_range_addr_ok(x) for x in data_args)
    )


def _quoted_parse_result_ok(s: str, start: int, result: tuple[str, int] | None) -> bool:
    if result is None:
        return True
    code, end = result
    return isinstance(code, str) and isinstance(end, int) and 0 <= start < end <= len(s)


def _parts_result_ok(result: Any) -> bool:
    if result is None:
        return True
    prefix = result.prefix
    code = result.code
    data_suffix = result.data_suffix
    return (
        isinstance(prefix, str)
        and prefix.endswith("(")
        and isinstance(code, str)
        and isinstance(data_suffix, str)
        and data_suffix.endswith(")")
    )


_parse_quoted_string = deal.pre(
    lambda s, start: str_bounded(s, DEAL_MAX_SOURCE) and isinstance(start, int) and 0 <= start <= len(s)
)(
    deal.post(lambda result: result is None or (isinstance(result, tuple) and len(result) == 2))(
        inverse_ensure(lambda s, start, result: _quoted_parse_result_ok(s, start, result))(
            _prod._parse_quoted_string
        )
    )
)

_parse_unquoted_code_arg = deal.pre(
    lambda inner_body: _DEAL_ASCII_STR(inner_body, DEAL_MAX_SOURCE)
)(
    deal.post(
        lambda result: result is None or (isinstance(result, str) and not result.startswith('"'))
    )(_prod._parse_unquoted_code_arg)
)

normalize_formula_string = deal.pre(
    lambda formula: str_bounded(formula, DEAL_MAX_SOURCE)
)(
    deal.post(lambda result: isinstance(result, str))(
        _prod.normalize_formula_string
    )
)

parse_python_formula = deal.pre(
    lambda formula: str_bounded(formula, DEAL_MAX_SOURCE)
)(
    deal.post(lambda result: result is None or isinstance(result, PythonFormulaParts))(
        inverse_ensure(lambda formula, result: _parts_result_ok(result))(
            _prod.parse_python_formula
        )
    )
)

escape_code_for_formula = deal.pre(
    lambda code: str_bounded(code, DEAL_MAX_SOURCE + 256)
)(
    deal.post(lambda result: isinstance(result, str))(
        deal.ensure(lambda code, result: result == (code or "").replace('"', '""'))(
            _prod.escape_code_for_formula
        )
    )
)

rebuild_python_formula = deal.pre(
    lambda parts, new_code: isinstance(parts, PythonFormulaParts)
    and str_bounded(new_code, DEAL_MAX_SOURCE + 256)
)(
    deal.post(lambda result: isinstance(result, str))(
        deal.ensure(
            lambda parts, new_code, result: parts.data_suffix in result
            and result.startswith(f'{parts.prefix}"')
        )(_prod.rebuild_python_formula)
    )
)

format_data_binding_display = deal.pre(
    lambda data_suffix: ascii_bounded(data_suffix, DEAL_MAX_SOURCE)
)(
    deal.post(lambda result: isinstance(result, str))(
        deal.ensure(
            lambda data_suffix, result: not result
            or (not result.startswith(";") and not result.startswith(",") and not result.endswith(")"))
        )(_prod.format_data_binding_display)
    )
)

parse_data_binding_text = deal.pre(
    lambda text: ascii_bounded(text, DEAL_MAX_SOURCE)
)(
    deal.post(lambda result: isinstance(result, list) and all(isinstance(x, str) for x in result))(
        _prod.parse_data_binding_text
    )
)

format_data_binding_text = deal.pre(
    lambda *args, **kwargs: bool(args) and _deal_data_args_ok(args[0])
)(
    deal.post(lambda result: isinstance(result, str))(
        _prod.format_data_binding_text
    )
)

_is_ascii_letter = deal.pre(
    lambda c: isinstance(c, str) and len(c) == 1 and c.isascii()
)(_prod._is_ascii_letter)

_is_a1_cell_prefix = deal.pre(
    lambda s: _DEAL_ASCII_STR(s, DEAL_MAX_TOKEN)
)(_prod._is_a1_cell_prefix)

_is_sheet_identifier = deal.pre(
    lambda s: _DEAL_ASCII_STR(s, DEAL_MAX_TOKEN)
)(_prod._is_sheet_identifier)

format_py_data_range = deal.pre(
    lambda range_addr: _deal_range_addr_ok(range_addr)
)(
    deal.post(lambda result: isinstance(result, str))(
        _prod.format_py_data_range
    )
)

format_excel_data_range = deal.pre(
    lambda range_addr: _deal_range_addr_ok(range_addr)
)(
    deal.post(lambda result: isinstance(result, str))(
        _prod.format_excel_data_range
    )
)

build_data_suffix = deal.pre(
    lambda data_args, *_unused, **__: _deal_data_args_ok(data_args)
)(
    deal.ensure(
        lambda *args, result=None, **kwargs: isinstance(result, str) and result.endswith(")")
    )(_prod.build_data_suffix)
)

rebuild_python_formula_with_data = deal.pre(
    lambda code, data_args, *_unused, **__: str_bounded(code, DEAL_MAX_SOURCE + 256)
    and _deal_data_args_ok(data_args)
)(
    deal.ensure(
        lambda *args, result=None, **kwargs: isinstance(result, str) and result.endswith(")")
    )(_prod.rebuild_python_formula_with_data)
)

PYTEST = [
    "_parse_quoted_string",
    "_parse_unquoted_code_arg",
    "normalize_formula_string",
    "parse_python_formula",
    "escape_code_for_formula",
    "rebuild_python_formula",
    "format_data_binding_display",
    "parse_data_binding_text",
    "format_data_binding_text",
    "_is_ascii_letter",
    "_is_a1_cell_prefix",
    "_is_sheet_identifier",
    "format_py_data_range",
    "format_excel_data_range",
    "build_data_suffix",
    "rebuild_python_formula_with_data",
]

TARGETS = [
    f"{__name__}._parse_quoted_string",
    f"{__name__}._parse_unquoted_code_arg",
    f"{__name__}.normalize_formula_string",
    f"{__name__}.parse_python_formula",
    f"{__name__}._is_ascii_letter",
    f"{__name__}._is_a1_cell_prefix",
    f"{__name__}._is_sheet_identifier",
    f"{__name__}.format_py_data_range",
    f"{__name__}.format_excel_data_range",
]
