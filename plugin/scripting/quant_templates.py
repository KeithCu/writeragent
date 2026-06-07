# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in Run Python Script templates for trusted quant helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from plugin.scripting.quant_common import HELPER_NAMES

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
        f"#",
        f"# {desc}",
        f"# This script delegates to the trusted quant venv module.",
        f"# Edit the JSON params above if needed. No other code runs.",
    ]
    
    return "\n".join(lines) + "\n"
