# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv quant compute — runs in user venv worker."""

from __future__ import annotations

import importlib
import logging
from typing import Any

from plugin.scripting.venv.coerce import CoerceResult, coerce_to_dataframe

# Local copy of small pure value from the host facade. The worker must not import
# from plugin.scripting.* (those modules pull in host-only code and are not guaranteed
# to exist or be compatible in the user's configured venv interpreter).
HELPER_NAMES = (
    "fetch_historical_data",
    "technical_analysis",
    "portfolio_tearsheet",
    "efficient_frontier",
)

log = logging.getLogger(__name__)


def _error_result(code: str, message: str, *, helper: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "error", "code": code, "message": message}
    if helper:
        out["helper"] = helper
    return out


def _missing_package_error(helper: str, package: str) -> dict[str, Any]:
    return _error_result(
        "MISSING_PACKAGE",
        f"{package} is required for {helper}.",
        helper=helper,
    )


def _resolve_df(data: Any, *, headers: bool = True, header_row: int = 0, sheet_hint: str | None = None) -> CoerceResult:
    if isinstance(data, CoerceResult):
        return data
    if hasattr(data, "columns") and hasattr(data, "index"):
        df = data.copy()
        meta: dict[str, Any] = {
            "n_rows": int(len(df)),
            "n_cols": int(len(df.columns)),
            "numeric_cols": [str(c) for c in df.select_dtypes(include="number").columns],
        }
        if sheet_hint:
            meta["sheet_hint"] = sheet_hint
        return CoerceResult(df=df, metadata=meta)
    return coerce_to_dataframe(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)


def fetch_historical_data(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        return _missing_package_error("fetch_historical_data", "yfinance")
    
    tickers = params.get("tickers", [])
    if isinstance(tickers, str):
        tickers = [tickers]
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    interval = params.get("interval", "1d")
    
    if not tickers:
        return _error_result("INVALID_PARAMS", "tickers parameter is required.")
        
    try:
        data = yf.download(tickers, start=start_date, end=end_date, interval=interval)
        data = data.reset_index()
        # Convert datetime to string for JSON serialization
        if 'Date' in data.columns:
            data['Date'] = data['Date'].astype(str)
        if 'Datetime' in data.columns:
            data['Datetime'] = data['Datetime'].astype(str)
            
        columns = list(data.columns)
        records = data.values.tolist()
        
        return {
            "status": "success",
            "helper": "fetch_historical_data",
            "table": {
                "columns": columns,
                "rows": records
            }
        }
    except Exception as e:
        log.exception("Error in fetch_historical_data")
        return _error_result("EXECUTION_ERROR", str(e), helper="fetch_historical_data")


def technical_analysis(params: dict[str, Any], data: Any, context: dict[str, Any]) -> dict[str, Any]:
    try:
        importlib.import_module("pandas_ta")
    except ImportError:
        return _missing_package_error("technical_analysis", "pandas-ta")
        
    res = _resolve_df(data)
    df = res.df
    indicators = params.get("indicators", ["macd", "rsi", "bbands"])
    
    try:
        # Assuming df has typical columns like Close, High, Low
        close_col = next((c for c in df.columns if c.lower() == 'close'), None)
        if close_col:
            for ind in indicators:
                if ind.lower() == 'macd':
                    df.ta.macd(close=close_col, append=True)
                elif ind.lower() == 'rsi':
                    df.ta.rsi(close=close_col, append=True)
                elif ind.lower() == 'bbands':
                    df.ta.bbands(close=close_col, append=True)
        else:
            return _error_result("MISSING_COLUMN", "Could not find 'Close' column for technical analysis.")
            
        # Convert datetime again if needed
        for col in df.select_dtypes(include=['datetime64']).columns:
            df[col] = df[col].astype(str)
            
        return {
            "status": "success",
            "helper": "technical_analysis",
            "table": {
                "columns": list(df.columns),
                "rows": df.values.tolist()
            }
        }
    except Exception as e:
        log.exception("Error in technical_analysis")
        return _error_result("EXECUTION_ERROR", str(e), helper="technical_analysis")


def portfolio_tearsheet(params: dict[str, Any], data: Any, context: dict[str, Any]) -> dict[str, Any]:
    try:
        import quantstats as qs  # type: ignore
    except ImportError:
        return _missing_package_error("portfolio_tearsheet", "quantstats")
        
    res = _resolve_df(data)
    df = res.df
    
    try:
        if df.shape[1] > 1:
            prices = df.iloc[:, 1]
            returns = prices.pct_change().dropna()
        else:
            returns = df.iloc[:, 0].dropna()
            
        metrics = qs.reports.metrics(returns, display=False)
        metrics_dict = metrics.to_dict()
        
        return {
            "status": "success",
            "helper": "portfolio_tearsheet",
            "metrics": metrics_dict
        }
    except Exception as e:
        log.exception("Error in portfolio_tearsheet")
        return _error_result("EXECUTION_ERROR", str(e), helper="portfolio_tearsheet")


def efficient_frontier(params: dict[str, Any], data: Any, context: dict[str, Any]) -> dict[str, Any]:
    try:
        from pypfopt.expected_returns import mean_historical_return  # type: ignore
        from pypfopt.risk_models import CovarianceShrinkage  # type: ignore
        from pypfopt.efficient_frontier import EfficientFrontier  # type: ignore
    except ImportError:
        return _missing_package_error("efficient_frontier", "PyPortfolioOpt")
        
    res = _resolve_df(data)
    df = res.df
    
    try:
        if 'Date' in df.columns or 'date' in df.columns:
            date_col = 'Date' if 'Date' in df.columns else 'date'
            df = df.set_index(date_col)
            
        import pandas as pd
        df = df.apply(pd.to_numeric, errors='coerce').dropna()
        
        mu = mean_historical_return(df)
        S = CovarianceShrinkage(df).ledoit_wolf()
        
        ef = EfficientFrontier(mu, S)
        ef.max_sharpe()
        cleaned_weights = ef.clean_weights()
        
        return {
            "status": "success",
            "helper": "efficient_frontier",
            "weights": cleaned_weights
        }
    except Exception as e:
        log.exception("Error in efficient_frontier")
        return _error_result("EXECUTION_ERROR", str(e), helper="efficient_frontier")


def run_quant(
    helper: str,
    params: dict[str, Any],
    data: Any = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = context or {}
    
    if helper not in HELPER_NAMES:
        return _error_result("UNKNOWN_HELPER", f"Unknown quant helper '{helper}'.", helper=helper)
        
    if helper == "fetch_historical_data":
        return fetch_historical_data(params, context)
    elif helper == "technical_analysis":
        return technical_analysis(params, data, context)
    elif helper == "portfolio_tearsheet":
        return portfolio_tearsheet(params, data, context)
    elif helper == "efficient_frontier":
        return efficient_frontier(params, data, context)
        
    return _error_result("UNIMPLEMENTED", f"Helper {helper} not fully implemented.", helper=helper)
