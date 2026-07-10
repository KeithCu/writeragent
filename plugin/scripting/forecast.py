# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Forecast helper templates, host RPC, and Calc egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.forecast`` via ``__getattr__``.
"""

from __future__ import annotations

import logging
from typing import Any

from plugin.calc.analysis_runner import calc_tool_context
from plugin.calc.bridge import CalcBridge
from plugin.scripting._lazy_venv import make_getattr
from plugin.calc.python.venv import _resolve_python_data
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.client import run_forecast as client_run_forecast
from plugin.scripting.helper_domain import (
    HelperScriptMeta,
    header_prefix,
)

log = logging.getLogger(__name__)

from plugin.scripting.calc_functions_common import (
    FORECAST_HELPER_NAMES as HELPER_NAMES,
)

FORECAST_HEADER_PREFIX = header_prefix("forecast")

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "forecast_time_series": {
        "periods": 12,
        "model": "auto",
        "date_col": "Date",
        "value_col": "Value",
    },
    "decompose_time_series": {
        "date_col": "Date",
        "value_col": "Value",
        "model": "additive",
        "period": None,
    },
    "anomaly_detection_time_series": {
        "date_col": "Date",
        "value_col": "Value",
        "period": None,
        "method": "stl_residual",
        "threshold": 3.0,
        "include_all": False,
    },
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "forecast_time_series": "Forward time-series predictions with optional confidence intervals",
    "decompose_time_series": "Trend / seasonal / residual decomposition",
    "anomaly_detection_time_series": "Flag temporal outliers via STL residuals and robust z-scores",
}

_FORECAST_VENV_EXPORTS = frozenset(
    {
        "anomaly_detection_time_series",
        "decompose_time_series",
        "forecast_time_series",
        "run_forecast",
    }
)

__getattr__ = make_getattr("forecast", _FORECAST_VENV_EXPORTS)


# --- Templates ---

from plugin.scripting.helper_domain import DomainFacadeConfig, make_template_api

ForecastScriptHeader = HelperScriptMeta

_API = make_template_api(
    DomainFacadeConfig(
        tag="forecast",
        helper_names=HELPER_NAMES,
        default_params=_DEFAULT_PARAMS,
        descriptions=_HELPER_DESCRIPTIONS,
        import_module="writeragent.scripting.forecast",
        run_name="run_forecast",
        style="header_only",
        compact_json=False,
        require_prefix=False,
        on_bad_json="none",
        extra_comment_lines=(
            "# This script delegates to the trusted forecast venv module.",
            "# Edit the JSON params above if needed. No other code runs.",
        ),
    )
)

parse_forecast_script_header = _API.parse_header


def get_forecast_template(helper: str) -> str | None:
    if helper not in HELPER_NAMES:
        return None
    return _API.template_body(helper, dict(_DEFAULT_PARAMS.get(helper, {})))


def run_trusted_forecast(
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
    """Fetch Calc data and run a trusted forecast helper in the user venv."""
    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="FORECAST_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="FORECAST_ERROR")

    dr = str(data_range).strip() if data_range else None
    if not dr and data is None:
        raise ToolExecutionError("Provide data_range or data", code="FORECAST_ERROR")

    tool_ctx = calc_tool_context(uno_ctx, doc)
    py_data, err = _resolve_python_data(tool_ctx, data_range=dr, data=data)
    if err:
        raise ToolExecutionError(err, code="FORECAST_ERROR")
    if py_data is None:
        raise ToolExecutionError("No data to forecast", code="FORECAST_ERROR")

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

    return client_run_forecast(uno_ctx, spec, py_data, context=context or None)


def is_forecast_result(value: Any) -> bool:
    """True when *value* matches the compact forecast helper result contract."""
    if not isinstance(value, dict):
        return False
    if "status" not in value:
        return False
    helper = value.get("helper")
    if isinstance(helper, str) and helper in HELPER_NAMES:
        return True
    if value.get("status") == "error":
        code = str(value.get("code") or "")
        return code == "FORECAST_ERROR" or "FORECAST" in code
    return False


def format_forecast_for_calc(result: dict[str, Any]) -> list[list[Any]]:
    """Turn a forecast helper result dict into a row-major grid for ``write_formula_range``."""
    from plugin.calc.tabular_egress import format_tabular_helper_for_calc

    return format_tabular_helper_for_calc(
        result,
        domain_label="Forecast",
        default_helper="forecast",
        failed_message="Forecast failed.",
    )


def insert_forecast_result_into_calc(
    doc: Any,
    uno_ctx: Any,
    result: dict[str, Any],
    *,
    start_col: int | None = None,
    start_row: int | None = None,
) -> int:
    """Write formatted forecast output starting at *start_col*/*start_row* (or selection). Returns row count."""
    from plugin.calc.tabular_egress import insert_tabular_result_into_calc

    grid = format_forecast_for_calc(result)
    return insert_tabular_result_into_calc(
        doc,
        uno_ctx,
        grid,
        start_col=start_col,
        start_row=start_row,
    )
