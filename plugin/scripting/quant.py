# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Quant helper templates (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.quant`` via ``__getattr__``.
"""

from __future__ import annotations

from typing import Any

from plugin.scripting._lazy_venv import install_lazy_exports
from plugin.scripting.calc_functions_common import QUANT_HELPER_NAMES as HELPER_NAMES
from plugin.scripting.helper_domain import (
    DomainFacadeConfig,
    make_template_api,
)

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    # Runner templates embed params in a Python literal, so "data" was the ticker symbol
    # "data". The sheet variable is the second argument to run_quant, and fetch_historical_data
    # does not read it. AAPL is a real symbol in the default date window.
    "fetch_historical_data": {"tickers": "AAPL", "start_date": "2023-01-01", "end_date": "2024-01-01", "interval": "1d"},
    "technical_analysis": {"indicators": ["macd", "rsi", "bbands"]},
    "portfolio_tearsheet": {},
    "efficient_frontier": {},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "fetch_historical_data": "Fetch historical prices via yfinance",
    "technical_analysis": "Calculate MACD, RSI, and Bollinger Bands",
    "portfolio_tearsheet": "Generate performance metrics from a returns sheet via quantstats",
    "efficient_frontier": "Optimize weights from a returns sheet via PyPortfolioOpt",
}

_QUANT_VENV_EXPORTS = HELPER_NAMES | {"run_quant"}

install_lazy_exports(globals(), "quant", _QUANT_VENV_EXPORTS)


# --- Templates ---

_API = make_template_api(
    DomainFacadeConfig(
        tag="quant",
        helper_names=HELPER_NAMES,
        default_params=_DEFAULT_PARAMS,
        descriptions=_HELPER_DESCRIPTIONS,
        import_module="writeragent.scripting.quant",
        run_name="run_quant",
        invoke="runner",
    )
)

parse_quant_script_header = _API.parse_header
get_quant_script_templates = _API.get_templates


def get_quant_template(helper: str) -> str | None:
    if helper not in HELPER_NAMES:
        return None
    return _API.template_body(helper, dict(_DEFAULT_PARAMS.get(helper, {})))
