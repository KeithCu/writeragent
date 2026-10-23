# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Forecast helper templates, host RPC, and Calc egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.forecast`` via ``__getattr__``.
"""

from __future__ import annotations

import functools
from typing import Any

from plugin.scripting._lazy_venv import install_lazy_exports
from plugin.scripting.helper_domain import (
    DomainFacadeConfig,
    insert_helper_result_into_calc,
    is_status_helper_result,
    make_template_api,
    run_trusted_calc_data_helper,
)

from plugin.scripting.calc_functions_common import (
    FORECAST_HELPER_NAMES as HELPER_NAMES,
)

HELPER_PARAM_HELP: dict[str, str] = {
    "forecast_time_series": "periods (int, default 12, minimum 1, capped at 10000), model (auto, holt_winters, arima, or moving_average; default auto), date_col (default Date) and value_col (default Value) must be different columns, seasonal_periods (optional int). Date cells may be ISO strings, Calc serial numbers, or calendar years 1900-2200. model=auto uses Holt-Winters when at least two seasonal cycles are present, otherwise ARIMA(1,1,1), otherwise a flat moving average. seasonal_periods applies to auto and holt_winters. Explicit arima and moving_average ignore it and add a flag.",
    "decompose_time_series": "date_col (default Date) and value_col (default Value) must be different columns, model (additive or multiplicative; default additive), period (optional int greater than 1; inferred from the dates when omitted).",
    "anomaly_detection_time_series": "date_col (default Date) and value_col (default Value) must be different columns, period (optional int greater than 1), method (stl_residual only), threshold (number greater than 0, default 3), include_all (bool, default false).",
}

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

_FORECAST_VENV_EXPORTS = HELPER_NAMES | {"run_forecast"}

install_lazy_exports(globals(), "forecast", _FORECAST_VENV_EXPORTS)


# --- Templates ---

_API = make_template_api(
    DomainFacadeConfig(
        tag="forecast",
        helper_names=HELPER_NAMES,
        default_params=_DEFAULT_PARAMS,
        descriptions=_HELPER_DESCRIPTIONS,
        import_module="writeragent.scripting.forecast",
        run_name="run_forecast",
        leading_data=True,
        extra_comment_lines=("# Set the data range in the toolbar (or select cells), then Run.",),
    )
)

parse_forecast_script_header = _API.parse_header
get_forecast_script_templates = _API.get_templates
get_forecast_template = _API.template_for


def run_trusted_forecast(
    uno_ctx: Any,
    doc: Any,
    *,
    helper: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch Calc data and run a trusted forecast helper in the user venv."""
    # What was wrong: importing this facade loaded the host worker inside the venv child.
    # How it happened: Run Python Script templates import writeragent.scripting.forecast,
    # and the module-level client import pulls trusted_rpc, which imports venv_worker.
    # Why this change: the child only needs the lazy venv helpers. The host runner
    # imports the client when run_trusted_forecast is called.
    from plugin.scripting.client import run_forecast as client_run_forecast

    return run_trusted_calc_data_helper(
        uno_ctx,
        doc,
        helper=helper,
        helper_names=HELPER_NAMES,
        error_code="FORECAST_ERROR",
        empty_data_message="No data to forecast",
        client_run=client_run_forecast,
        **kwargs,
    )


def is_forecast_result(value: Any) -> bool:
    """True when *value* matches the compact forecast helper result contract."""
    return is_status_helper_result(value, HELPER_NAMES, frozenset({"FORECAST_ERROR"}))


insert_forecast_result_into_calc = functools.partial(
    insert_helper_result_into_calc,
    domain_label="Forecast",
    default_helper="forecast",
    failed_message="Forecast failed.",
)
