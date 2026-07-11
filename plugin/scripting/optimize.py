# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Optimize helper templates, host RPC, and Calc egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.optimize`` via ``__getattr__``.
"""

from __future__ import annotations

import logging
from typing import Any

from plugin.calc.analysis_runner import calc_tool_context
from plugin.calc.bridge import CalcBridge
from plugin.scripting._lazy_venv import make_getattr
from plugin.calc.calc_addin_data import _resolve_python_data
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.client import run_optimize as client_run_optimize
from plugin.scripting.helper_domain import (
    HelperScriptMeta,
    header_prefix,
)

log = logging.getLogger(__name__)

# --- Common & Constants ---

from plugin.scripting.calc_functions_common import (
    OPTIMIZE_HELPER_NAMES as HELPER_NAMES,
)

OPTIMIZE_HEADER_PREFIX = header_prefix("optimize")

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "optimize_portfolio": {"returns_col": None, "target_return": None, "risk_free_rate": 0.0},
    "linear_programming": {"c_col": "c", "a_cols": ["a1"], "b_col": "b", "maximize": False},
    "solve_scheduling_problem": {"cost_cols": ["cost1"]},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "optimize_portfolio": "Mean-variance portfolio optimization",
    "linear_programming": "Linear programming solver",
    "solve_scheduling_problem": "Assignment problem solver (e.g., workers to tasks)",
}

_OPTIMIZE_VENV_EXPORTS = frozenset(
    {
        "linear_programming",
        "optimize_portfolio",
        "run_optimize",
        "solve_scheduling_problem",
    }
)

__getattr__ = make_getattr("optimize", _OPTIMIZE_VENV_EXPORTS)


# --- Templates ---

from plugin.scripting.helper_domain import DomainFacadeConfig, make_template_api

OptimizeScriptHeader = HelperScriptMeta

_API = make_template_api(
    DomainFacadeConfig(
        tag="optimize",
        helper_names=HELPER_NAMES,
        default_params=_DEFAULT_PARAMS,
        descriptions=_HELPER_DESCRIPTIONS,
        import_module="writeragent.scripting.optimize",
        run_name="run_optimize",
        style="header_only",
        compact_json=False,
        require_prefix=False,
        on_bad_json="none",
        extra_comment_lines=(
            "# This script delegates to the trusted optimize venv module.",
            "# Edit the JSON params above if needed. No other code runs.",
        ),
    )
)

parse_optimize_script_header = _API.parse_header


def get_optimize_template(helper: str) -> str | None:
    if helper not in HELPER_NAMES:
        return None
    return _API.template_body(helper, dict(_DEFAULT_PARAMS.get(helper, {})))


# --- Runner ---

def run_trusted_optimize(
    uno_ctx: Any,
    doc: Any,
    *,
    helper: str,
    params: dict[str, Any] | None = None,
    data_range: str | None = None,
    data: Any = None,
    headers: bool = True,
    task_hint: str | None = None,
) -> dict[str, Any]:
    """Fetch Calc data and run a trusted optimization helper in the user venv."""
    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="OPTIMIZE_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="OPTIMIZE_ERROR")

    dr = str(data_range).strip() if data_range else None
    if not dr and data is None:
        raise ToolExecutionError("Provide data_range or data", code="OPTIMIZE_ERROR")

    tool_ctx = calc_tool_context(uno_ctx, doc)
    py_data, err = _resolve_python_data(tool_ctx, data_range=dr, data=data)
    if err:
        raise ToolExecutionError(err, code="OPTIMIZE_ERROR")
    if py_data is None:
        raise ToolExecutionError("No data to optimize", code="OPTIMIZE_ERROR")

    spec: dict[str, Any] = {"helper": name, "headers": bool(headers)}
    if isinstance(params, dict) and params:
        spec["params"] = params

    context: dict[str, Any] = {}
    try:
        bridge = CalcBridge(doc)
        context["sheet_name"] = bridge.get_active_sheet().getName()
    except Exception:
        pass
    if task_hint:
        context["task_hint"] = str(task_hint)
    if dr:
        context["range_a1"] = dr

    return client_run_optimize(uno_ctx, spec, py_data, context=context or None)


# --- Egress ---

def is_optimize_result(value: Any) -> bool:
    """True when *value* matches the compact optimize helper result contract."""
    if not isinstance(value, dict):
        return False
    if "status" not in value:
        return False
    helper = value.get("helper")
    if isinstance(helper, str) and helper in HELPER_NAMES:
        return True
    if value.get("status") == "error":
        code = str(value.get("code") or "")
        return code == "OPTIMIZE_ERROR" or "OPTIMIZ" in code
    return False


def format_optimize_for_calc(result: dict[str, Any]) -> list[list[Any]]:
    """Turn an optimize helper result dict into a row-major grid for ``write_formula_range``."""
    from plugin.calc.tabular_egress import format_tabular_helper_for_calc

    return format_tabular_helper_for_calc(
        result,
        domain_label="Optimization",
        default_helper="optimization",
        failed_message="Optimization failed.",
    )


def insert_optimize_result_into_calc(
    doc: Any,
    uno_ctx: Any,
    result: dict[str, Any],
    *,
    start_col: int | None = None,
    start_row: int | None = None,
) -> int:
    """Write formatted optimization output starting at *start_col*/*start_row* (or selection). Returns row count."""
    from plugin.calc.tabular_egress import insert_tabular_result_into_calc

    grid = format_optimize_for_calc(result)
    return insert_tabular_result_into_calc(
        doc,
        uno_ctx,
        grid,
        start_col=start_col,
        start_row=start_row,
    )
