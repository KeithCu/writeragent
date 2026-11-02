"""Contracts sidecar for plugin.calc.excel_py_convert.to_dag."""

from __future__ import annotations

import deal

from plugin.calc.excel_py_convert import to_dag as _prod
from plugin.calc.excel_py_convert.resolve_refs import ResolvedDep
from tests.contracts.bounds import (
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_PLACEHOLDER_INDEX,
    DEAL_MAX_SOURCE,
    DEAL_MAX_XL_EXPR,
    ascii_bounded,
    inverse_ensure,
    str_bounded,
)


def _deal_excel_src_ok(src: object) -> bool:
    return str_bounded(src, DEAL_MAX_SOURCE)


def _deal_convert_scripts_ok(model: object) -> bool:
    scripts = getattr(model, "scripts", None)
    return isinstance(scripts, list) and len(scripts) <= DEAL_MAX_CMD_ARGS and all(_deal_excel_src_ok(s) for s in scripts)


def _deal_convert_cell_ok(cell: object) -> bool:
    script_index = getattr(cell, "script_index", None)
    deps = getattr(cell, "deps", None)
    return (
        type(script_index) is int
        and isinstance(deps, list)
        and len(deps) <= DEAL_MAX_CMD_ARGS
        and all(isinstance(d, str) and ascii_bounded(d, DEAL_MAX_SOURCE) for d in deps)
    )


_placeholder_to_data_index = deal.pre(
    lambda p_num, *_unused, **__: isinstance(p_num, int)
    and 2 <= p_num <= 2 + DEAL_MAX_PLACEHOLDER_INDEX
)(
    deal.post(
        lambda result: isinstance(result, int) and 0 <= result <= DEAL_MAX_PLACEHOLDER_INDEX
    )(_prod._placeholder_to_data_index)
)

_xl_binding_expr = deal.pre(
    lambda index, *_unused, **__: isinstance(index, int)
    and 0 <= index <= DEAL_MAX_PLACEHOLDER_INDEX
)(
    deal.post(
        lambda result: isinstance(result, str)
        and result.startswith("xl(")
        and result.endswith(")")
        and '"%P' in result
        and len(result) <= DEAL_MAX_XL_EXPR
    )(_prod._xl_binding_expr)
)

_skip_string = deal.pre(
    lambda src, i, *_unused, **__: str_bounded(src, DEAL_MAX_SOURCE)
    and type(i) is int
    and 0 <= i < len(src)
)(_prod._skip_string)

_normalize_excel_placeholders = deal.pre(
    lambda src, *_unused, **__: _deal_excel_src_ok(src)
)(
    inverse_ensure(lambda *args, result="", **kwargs: len(result) == len(args[0]))(
        _prod._normalize_excel_placeholders
    )
)

_find_xl_calls = deal.pre(lambda code, *_unused, **__: _deal_excel_src_ok(code or ""))(
    _prod._find_xl_calls
)

_prefer_excel_dep_token = deal.pre(
    lambda current, candidate: ascii_bounded(current or "", DEAL_MAX_SOURCE)
    and ascii_bounded(candidate or "", DEAL_MAX_SOURCE)
)(_prod._prefer_excel_dep_token)

_normalize_bindings = deal.pre(
    lambda resolved, header_modes: (
        type(resolved) is list
        and len(resolved) <= DEAL_MAX_CMD_ARGS
        and all(
            isinstance(r, ResolvedDep)
            and str_bounded(r.note or "", DEAL_MAX_SOURCE)
            and ascii_bounded(r.a1, DEAL_MAX_SOURCE)
            and (r.original is None or ascii_bounded(r.original, DEAL_MAX_SOURCE))
            for r in resolved
        )
        and type(header_modes) is dict
    )
)(_prod._normalize_bindings)

convert_cell_to_dag = deal.pre(
    lambda model, cell, *_unused, **__: _deal_convert_scripts_ok(model)
    and _deal_convert_cell_ok(cell)
)(_prod.convert_cell_to_dag)

convert_model_to_dag = deal.pre(
    lambda model, *_unused, **__: (
        _deal_convert_scripts_ok(model)
        and isinstance(model.cells, list)
        and len(model.cells) <= DEAL_MAX_CMD_ARGS
        and all(_deal_convert_cell_ok(c) for c in model.cells)
    )
)(_prod.convert_model_to_dag)

PYTEST = [
    "_placeholder_to_data_index",
    "_xl_binding_expr",
    "_skip_string",
    "_normalize_excel_placeholders",
    "_find_xl_calls",
    "_prefer_excel_dep_token",
    "_normalize_bindings",
    "convert_cell_to_dag",
    "convert_model_to_dag",
]

TARGETS = [
    f"{__name__}._placeholder_to_data_index",
    f"{__name__}._xl_binding_expr",
    f"{__name__}._skip_string",
    f"{__name__}._find_xl_calls",
    f"{__name__}.convert_model_to_dag",
]
