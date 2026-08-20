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

from plugin.scripting.calc_functions_common import QUANT_HELPER_NAMES as HELPER_NAMES

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
            "status": "ok",
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
            "status": "ok",
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
        import pandas as pd
        import quantstats as qs  # type: ignore
    except ImportError:
        return _missing_package_error("portfolio_tearsheet", "quantstats")

    res = _resolve_df(data)
    df = res.df

    if df.empty:
        return _error_result("INVALID_DATA", "Input data is empty.", helper="portfolio_tearsheet")

    date_col = next((c for c in df.columns if str(c).strip().lower() in ("date", "datetime", "timestamp")), None)
    dates = None
    if date_col is not None:
        dates = pd.to_datetime(df[date_col], errors="coerce")
        df = df.drop(columns=[date_col])

    numeric_df = df.apply(pd.to_numeric, errors="coerce")
    numeric_df = numeric_df.dropna(how="all")

    if numeric_df.empty or numeric_df.shape[1] == 0:
        return _error_result("INVALID_DATA", "No numeric data columns found for portfolio tearsheet.", helper="portfolio_tearsheet")

    col_param = params.get("column") if isinstance(params, dict) else None
    if col_param and col_param in numeric_df.columns:
        returns = numeric_df[col_param].dropna()
        if dates is not None:
            dates = dates.loc[returns.index]
    else:
        if numeric_df.shape[1] == 1:
            returns = numeric_df.iloc[:, 0].dropna()
            if dates is not None:
                dates = dates.loc[returns.index]
        else:
            returns = numeric_df.mean(axis=1).dropna()
            if dates is not None:
                dates = dates.loc[returns.index]

    returns = pd.to_numeric(returns, errors="coerce").dropna()
    if returns.empty:
        return _error_result("INVALID_DATA", "No valid numeric returns found.", helper="portfolio_tearsheet")

    if dates is not None and not dates.dropna().empty:
        returns.index = dates
    else:
        returns.index = pd.date_range("2024-01-01", periods=len(returns), freq="D")

    try:
        metrics = qs.reports.metrics(returns, display=False)
        if hasattr(metrics, "iloc") and metrics.shape[1] >= 1:
            metrics_dict = {str(k): v for k, v in metrics.iloc[:, 0].to_dict().items()}
        elif isinstance(metrics, dict):
            metrics_dict = metrics
        else:
            metrics_dict = metrics.to_dict()

        return {
            "status": "ok",
            "helper": "portfolio_tearsheet",
            "metrics": metrics_dict,
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
            "status": "ok",
            "helper": "efficient_frontier",
            "weights": cleaned_weights
        }
    except Exception as e:
        log.exception("Error in efficient_frontier")
        return _error_result("EXECUTION_ERROR", str(e), helper="efficient_frontier")


def run_quant(
    spec: dict[str, Any] | str,
    data: Any = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spec-driven dispatcher — single trusted entry for host RPC and Run Python Script."""
    if isinstance(spec, str):
        spec_dict: dict[str, Any] = {"helper": spec}
    elif isinstance(spec, dict):
        spec_dict = spec
    else:
        return _error_result("INVALID_SPEC", "spec must be a dict or helper name string")

    helper = str(spec_dict.get("helper") or "").strip()
    if not helper:
        return _error_result("MISSING_HELPER", "spec.helper is required")
    if helper not in HELPER_NAMES:
        return _error_result("UNKNOWN_HELPER", f"Unknown quant helper '{helper}'.", helper=helper)

    params: dict[str, Any] = spec_dict["params"] if isinstance(spec_dict.get("params"), dict) else {}
    ctx = context if isinstance(context, dict) else {}

    if helper == "fetch_historical_data":
        return fetch_historical_data(params, ctx)
    if helper == "technical_analysis":
        return technical_analysis(params, data, ctx)
    if helper == "portfolio_tearsheet":
        return portfolio_tearsheet(params, data, ctx)
    if helper == "efficient_frontier":
        return efficient_frontier(params, data, ctx)

    return _error_result("UNIMPLEMENTED", f"Helper {helper} not fully implemented.", helper=helper)
