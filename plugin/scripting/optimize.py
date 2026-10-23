# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Optimize helper templates, host RPC, and Calc egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.optimize`` via ``__getattr__``.
"""

from __future__ import annotations

from typing import Any

from plugin.scripting._lazy_venv import install_lazy_exports
from plugin.scripting.helper_domain import (
    DomainFacadeConfig,
    is_status_helper_result,
    make_template_api,
    run_trusted_calc_data_helper,
)

from plugin.scripting.calc_functions_common import (
    OPTIMIZE_HELPER_NAMES as HELPER_NAMES,
)

HELPER_PARAM_HELP: dict[str, str] = {
    "optimize_portfolio": "Long-only minimum-variance weights. Each weight is between 0 and 1 and the weights sum to 1. returns_col is a column name or a list; when omitted, every numeric column is used except date-named columns and monotonic unique integer columns such as ids, and each skipped column is listed in flags. target_return, when set, is a per-period mean constraint. risk_free_rate (default 0) is used only in the reported Sharpe and does not change the weights. Return and volatility are per period, not annualized. There is no short selling and no max-Sharpe mode.",
    "linear_programming": "c_col, a_cols (one name or a list), b_col, bounds (one [low, high] pair applied to every variable, default [0, null], or one pair per variable), maximize (bool, default false). Each row is a variable and c_col is its objective coefficient. Each name in a_cols is one <= constraint. b_col supplies one bound per constraint column; a single constraint column may repeat that same bound on every row. Only <= constraints are supported. A blank or non-numeric cell drops that variable row and does not fail the solve. c_col, b_col, and every name in a_cols must be distinct; a repeat is INVALID_PARAM.",
    "solve_scheduling_problem": "cost_cols is a column name or a list. When omitted, every numeric column is used except date-named columns and monotonic unique integer columns such as ids, and each skipped column is listed in flags. The helper minimizes total cost. row_index is the dataframe index of the assigned row after blank rows are removed, not a sheet row number. metrics.unassigned_rows counts rows left over when there are more rows than columns. metrics.unassigned_columns counts columns left over when there are more columns than rows. There is no maximize mode.",
}

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "optimize_portfolio": {"returns_col": None, "target_return": None, "risk_free_rate": 0.0},
    "linear_programming": {"c_col": "c", "a_cols": ["a1"], "b_col": "b", "maximize": False},
    "solve_scheduling_problem": {"cost_cols": ["cost1"]},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "optimize_portfolio": "Long-only minimum-variance portfolio weights",
    "linear_programming": "Linear programming solver",
    "solve_scheduling_problem": "Assignment problem solver (e.g., workers to tasks)",
}

_OPTIMIZE_VENV_EXPORTS = HELPER_NAMES | {"run_optimize"}

install_lazy_exports(globals(), "optimize", _OPTIMIZE_VENV_EXPORTS)


# --- Templates ---

_API = make_template_api(
    DomainFacadeConfig(
        tag="optimize",
        helper_names=HELPER_NAMES,
        default_params=_DEFAULT_PARAMS,
        descriptions=_HELPER_DESCRIPTIONS,
        import_module="writeragent.scripting.optimize",
        run_name="run_optimize",
        leading_data=True,
        extra_comment_lines=("# Set the data range in the toolbar (or select cells), then Run.",),
    )
)

parse_optimize_script_header = _API.parse_header
get_optimize_script_templates = _API.get_templates
get_optimize_template = _API.template_for


# --- Runner ---

def run_trusted_optimize(
    uno_ctx: Any,
    doc: Any,
    *,
    helper: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch Calc data and run a trusted optimization helper in the user venv."""
    # What was wrong: importing this facade loaded the host worker inside the venv child.
    # How it happened: Run Python Script templates import writeragent.scripting.optimize,
    # and the module-level client import pulls trusted_rpc, which imports venv_worker.
    # Why this change: the child only needs the lazy venv helpers. The host runner
    # imports the client when run_trusted_optimize is called.
    from plugin.scripting.client import run_optimize as client_run_optimize

    return run_trusted_calc_data_helper(
        uno_ctx,
        doc,
        helper=helper,
        helper_names=HELPER_NAMES,
        error_code="OPTIMIZE_ERROR",
        empty_data_message="No data to optimize",
        client_run=client_run_optimize,
        **kwargs,
    )


# --- Egress ---

def is_optimize_result(value: Any) -> bool:
    """True when *value* matches the compact optimize helper result contract."""
    return is_status_helper_result(value, HELPER_NAMES, frozenset({"OPTIMIZE_ERROR"}))
