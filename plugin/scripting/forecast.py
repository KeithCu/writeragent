# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Forecast helper templates, host RPC, and Calc egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.forecast`` via ``__getattr__``.
"""

from __future__ import annotations

from typing import Any

from plugin.scripting._lazy_venv import install_lazy_dir, make_getattr
from plugin.scripting.client import run_forecast as client_run_forecast
from plugin.scripting.helper_domain import (
    header_prefix,
    is_status_helper_result,
    run_trusted_calc_data_helper,
)

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
install_lazy_dir(globals(), _FORECAST_VENV_EXPORTS)


# --- Templates ---

from plugin.scripting.helper_domain import DomainFacadeConfig, make_template_api


_API = make_template_api(
    DomainFacadeConfig(
        tag="forecast",
        helper_names=HELPER_NAMES,
        default_params=_DEFAULT_PARAMS,
        descriptions=_HELPER_DESCRIPTIONS,
        import_module="writeragent.scripting.forecast",
        run_name="run_forecast",
        style="run_import",
        data_expr="data",
        leading_data=True,
        extra_comment_lines=("# Set the data range in the toolbar (or select cells), then Run.",),
    )
)

parse_forecast_script_header = _API.parse_header
get_forecast_script_templates = _API.get_templates


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
        error_code="FORECAST_ERROR",
        empty_data_message="No data to forecast",
        client_run=client_run_forecast,
    )


def is_forecast_result(value: Any) -> bool:
    """True when *value* matches the compact forecast helper result contract."""
    return is_status_helper_result(value, HELPER_NAMES, frozenset({"FORECAST_ERROR"}))


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
    sheet_name: str | None = None,
    start_col: int | None = None,
    start_row: int | None = None,
) -> int:
    """Write formatted forecast output at *sheet_name*/*start_col*/*start_row* (or the selection).

    *sheet_name* is the prefix from ``parse_output_anchor``. The shared
    tabular writer qualifies the anchor so ``CalcBridge.resolve`` opens
    that sheet. A missing name stays on the active sheet (Run Python Script
    selection, or an unqualified ``output_range``).
    """
    from plugin.calc.tabular_egress import insert_tabular_result_into_calc

    grid = format_forecast_for_calc(result)
    return insert_tabular_result_into_calc(
        doc,
        uno_ctx,
        grid,
        sheet_name=sheet_name,
        start_col=start_col,
        start_row=start_row,
    )
