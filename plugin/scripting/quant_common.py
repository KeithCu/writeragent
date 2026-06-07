# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared constants for Quant helpers."""

from __future__ import annotations

HELPER_NAMES = (
    "fetch_historical_data",
    "technical_analysis",
    "portfolio_tearsheet",
    "efficient_frontier",
)

QUANT_VENV_PIP_INSTALL = "pip install yfinance pandas-ta quantstats pyportfolioopt"
