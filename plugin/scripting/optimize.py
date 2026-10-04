# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Optimize helper templates, host RPC, and Calc egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.optimize`` via ``__getattr__``.
"""

from __future__ import annotations

from typing import Any

from plugin.scripting._lazy_venv import install_lazy_dir, make_getattr
from plugin.scripting.client import run_optimize as client_run_optimize
from plugin.scripting.helper_domain import (
    header_prefix,
    is_status_helper_result,
    run_trusted_calc_data_helper,
)

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
install_lazy_dir(globals(), _OPTIMIZE_VENV_EXPORTS)


# --- Templates ---

from plugin.scripting.helper_domain import DomainFacadeConfig, make_template_api


_API = make_template_api(
    DomainFacadeConfig(
        tag="optimize",
        helper_names=HELPER_NAMES,
        default_params=_DEFAULT_PARAMS,
        descriptions=_HELPER_DESCRIPTIONS,
        import_module="writeragent.scripting.optimize",
        run_name="run_optimize",
        style="run_import",
        data_expr="data",
        leading_data=True,
        extra_comment_lines=("# Set the data range in the toolbar (or select cells), then Run.",),
    )
)

parse_optimize_script_header = _API.parse_header
get_optimize_script_templates = _API.get_templates


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
    return run_trusted_calc_data_helper(
        uno_ctx,
        doc,
        helper=helper,
        params=params,
        data_range=data_range,
        data=data,
        headers=headers,
        task_hint=task_hint,
        helper_names=HELPER_NAMES,
        error_code="OPTIMIZE_ERROR",
        empty_data_message="No data to optimize",
        client_run=client_run_optimize,
    )


# --- Egress ---

def is_optimize_result(value: Any) -> bool:
    """True when *value* matches the compact optimize helper result contract."""
    return is_status_helper_result(value, HELPER_NAMES, frozenset({"OPTIMIZE_ERROR"}))


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
    sheet_name: str | None = None,
    start_col: int | None = None,
    start_row: int | None = None,
) -> int:
    """Write formatted optimization output at *sheet_name*/*start_col*/*start_row* (or the selection).

    *sheet_name* is the prefix from ``parse_output_anchor``. The shared
    tabular writer qualifies the anchor so ``CalcBridge.resolve`` opens
    that sheet. A missing name stays on the active sheet (Run Python Script
    selection, or an unqualified ``output_range``).
    """
    from plugin.calc.tabular_egress import insert_tabular_result_into_calc

    grid = format_optimize_for_calc(result)
    return insert_tabular_result_into_calc(
        doc,
        uno_ctx,
        grid,
        sheet_name=sheet_name,
        start_col=start_col,
        start_row=start_row,
    )
