# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Quant helper templates and host RPC (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.quant`` via ``__getattr__``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from plugin.calc.analysis_runner import calc_tool_context
from plugin.scripting._lazy_venv import make_getattr
from plugin.calc.python.venv import _resolve_python_data
from plugin.doc.document_helpers import is_calc, is_writer
from plugin.scripting.client import run_quant as client_run_quant
from plugin.framework.errors import ToolExecutionError

log = logging.getLogger(__name__)

# --- Constants & Common ---

HELPER_NAMES = (
    "fetch_historical_data",
    "technical_analysis",
    "portfolio_tearsheet",
    "efficient_frontier",
)

QUANT_HEADER_PREFIX = "# writeragent:quant"
_QUANT_HEADER_RE = re.compile(
    r"^\s*#\s*writeragent:quant\s+helper=(\w+)\s+params=(\{.*\})\s*$",
    re.MULTILINE,
)

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "fetch_historical_data": {"tickers": ["AAPL", "MSFT"], "start_date": "2023-01-01", "end_date": "2024-01-01", "interval": "1d"},
    "technical_analysis": {"indicators": ["macd", "rsi", "bbands"]},
    "portfolio_tearsheet": {},
    "efficient_frontier": {},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "fetch_historical_data": "Fetch historical prices via yfinance",
    "technical_analysis": "Calculate MACD, RSI, and Bollinger Bands",
    "portfolio_tearsheet": "Generate portfolio performance metrics via quantstats",
    "efficient_frontier": "Optimize portfolio weights via PyPortfolioOpt",
}

_QUANT_VENV_EXPORTS = frozenset(
    {
        "efficient_frontier",
        "fetch_historical_data",
        "portfolio_tearsheet",
        "run_quant",
        "technical_analysis",
    }
)

__getattr__ = make_getattr("quant", _QUANT_VENV_EXPORTS)


# --- Templates ---

@dataclass
class QuantScriptHeader:
    helper: str
    params: dict[str, Any]


def parse_quant_script_header(code: str) -> QuantScriptHeader | None:
    match = _QUANT_HEADER_RE.search(code)
    if not match:
        return None
    try:
        params = json.loads(match.group(2))
        return QuantScriptHeader(helper=match.group(1), params=params)
    except Exception:
        return None


def get_quant_template(helper: str) -> str | None:
    if helper not in HELPER_NAMES:
        return None
    params = _DEFAULT_PARAMS.get(helper, {})
    params_str = json.dumps(params)
    
    desc = _HELPER_DESCRIPTIONS.get(helper, helper.replace("_", " ").title())
    
    lines = [
        f"{QUANT_HEADER_PREFIX} helper={helper} params={params_str}",
        "#",
        f"# {desc}",
        "# This script delegates to the trusted quant venv module.",
        "# Edit the JSON params above if needed. No other code runs.",
    ]
    
    return "\n".join(lines) + "\n"


# --- Runner ---

def supports_quant_manual(doc: Any) -> bool:
    """True when Run Python Script should expose Quant Helpers for *doc*."""
    if doc is None:
        return False
    try:
        return is_writer(doc) or is_calc(doc)
    except Exception:
        return False


def run_trusted_quant(
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
    """Fetch Calc data and run a trusted quant helper in the user venv."""
    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="QUANT_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="QUANT_ERROR")

    if not is_calc(doc) and not is_writer(doc):
        raise ToolExecutionError("Quant helpers require a Writer or Calc document.", code="QUANT_ERROR")

    dr = str(data_range).strip() if data_range else None
    
    py_data = None
    if dr or data is not None:
        tool_ctx = calc_tool_context(uno_ctx, doc)
        py_data, err = _resolve_python_data(tool_ctx, data_range=dr, data=data)
        if err:
            raise ToolExecutionError(err, code="QUANT_ERROR")
            
    # Some helpers like fetch_historical_data do not need py_data
    if name != "fetch_historical_data" and py_data is None:
        raise ToolExecutionError("Provide data_range or data for this quant helper", code="QUANT_ERROR")

    spec_params = params or {}

    context: dict[str, Any] = {}
    if is_calc(doc):
        try:
            from plugin.calc.bridge import CalcBridge
            context["sheet_name"] = CalcBridge(doc).get_active_sheet().getName()
        except Exception:
            pass
    if task_hint:
        context["task_hint"] = str(task_hint)
    if dr:
        context["range_a1"] = dr

    return client_run_quant(uno_ctx, name, spec_params, py_data, context=context or None)
