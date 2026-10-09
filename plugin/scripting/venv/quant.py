# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv quant compute — runs in user venv worker."""

from __future__ import annotations

import importlib
import logging
from typing import Any

from plugin.scripting.calc_functions_common import QUANT_HELPER_NAMES as HELPER_NAMES
from plugin.scripting.venv.coerce import (
    error_result as _error_result,
    is_missing_value as _is_missing_value,
    missing_package_error as _missing_package_error,
    ok_result as _ok_result,
    parse_trusted_spec as _parse_trusted_spec,
    resolve_df as _resolve_df,
)

log = logging.getLogger(__name__)


def _flatten_tickers(raw_tickers: Any) -> list[str]:
    """Normalize raw ticker inputs into a clean list of non-empty strings."""
    if isinstance(raw_tickers, str):
        return [raw_tickers.strip()] if raw_tickers.strip() else []
    if hasattr(raw_tickers, "values") and isinstance(raw_tickers.values, list):
        return [
            str(c).strip()
            for row in raw_tickers.values
            if isinstance(row, (list, tuple))
            for c in row
            if not _is_missing_value(c) and str(c).strip()
        ]
    if isinstance(raw_tickers, (list, tuple)):
        clean_list: list[str] = []
        for item in raw_tickers:
            if isinstance(item, (list, tuple)):
                for sub in item:
                    if not _is_missing_value(sub) and str(sub).strip():
                        clean_list.append(str(sub).strip())
            elif not _is_missing_value(item) and str(item).strip():
                clean_list.append(str(item).strip())
        return clean_list
    return []


def _returns_frame_from_df(df: Any) -> tuple[Any, Any]:
    """Extract date column if present and coerce remaining columns to numeric returns."""
    import pandas as pd

    date_col = next(
        (c for c in df.columns if str(c).strip().lower() in ("date", "datetime", "timestamp")),
        None,
    )
    dates = None
    if date_col is not None:
        dates = pd.to_datetime(df[date_col], errors="coerce")
        df = df.drop(columns=[date_col])

    numeric_df = df.apply(pd.to_numeric, errors="coerce")
    numeric_df = numeric_df.dropna(how="all")
    return numeric_df, dates


def fetch_historical_data(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        return _missing_package_error("fetch_historical_data", "yfinance")

    tickers = _flatten_tickers(params.get("tickers", []))
    if not tickers:
        return _error_result("INVALID_PARAMS", "tickers parameter is required.", helper="fetch_historical_data")

    start_date = params.get("start_date")
    end_date = params.get("end_date")
    interval = params.get("interval", "1d")

    try:
        # progress=False: yfinance writes a progress bar to stdout and that
        # breaks IPC framing. An empty frame is NO_DATA, not ok.
        data = yf.download(
            tickers,
            start=start_date,
            end=end_date,
            interval=interval,
            progress=False,
            multi_level_index=False,
        )
        if data is None or getattr(data, "empty", True):
            return _error_result("NO_DATA", f"No data returned for ticker(s): {', '.join(tickers)}.", helper="fetch_historical_data")

        data = data.reset_index()
        # Convert datetime to string for JSON serialization
        if "Date" in data.columns:
            data["Date"] = data["Date"].astype(str)
        if "Datetime" in data.columns:
            data["Datetime"] = data["Datetime"].astype(str)

        columns = [
            str(c[0]) if isinstance(c, tuple) and len(c) > 0 else str(c)
            for c in data.columns
        ]
        records = data.values.tolist()

        return _ok_result(
            "fetch_historical_data",
            table={
                "columns": columns,
                "rows": records,
            },
        )
    except Exception as e:
        log.exception("Error in fetch_historical_data")
        return _error_result("EXECUTION_ERROR", str(e), helper="fetch_historical_data")


def technical_analysis(
    params: dict[str, Any],
    data: Any,
    context: dict[str, Any],
    *,
    headers: bool = True,
    header_row: int = 0,
) -> dict[str, Any]:
    try:
        importlib.import_module("pandas_ta")
    except ImportError:
        return _missing_package_error("technical_analysis", "pandas-ta")

    raw_indicators = params.get("indicators", ["macd", "rsi", "bbands"])
    # A string is one indicator name, not a sequence of characters.
    # An unknown name is INVALID_PARAMS.
    if isinstance(raw_indicators, str):
        indicators = [raw_indicators]
    elif isinstance(raw_indicators, (list, tuple)):
        indicators = list(raw_indicators)
    else:
        indicators = ["macd", "rsi", "bbands"]

    _SUPPORTED_INDICATORS = {"macd", "rsi", "bbands"}
    unknown = [str(ind) for ind in indicators if str(ind).strip().lower() not in _SUPPORTED_INDICATORS]
    if unknown:
        return _error_result(
            "INVALID_PARAMS",
            f"Unknown indicator(s): {', '.join(unknown)}. Supported: macd, rsi, bbands.",
            helper="technical_analysis",
        )

    # Coerce inside the try so a bad grid is a result error, not a worker crash.
    try:
        res = _resolve_df(data, headers=headers, header_row=header_row)
        df = res.df

        # headers=False leaves integer column labels. str() before lower().
        close_col = next((c for c in df.columns if str(c).strip().lower() == "close"), None)
        if close_col:
            import pandas as pd

            df[close_col] = pd.to_numeric(df[close_col], errors="coerce")
            for ind in indicators:
                ind_lower = str(ind).strip().lower()
                if ind_lower == "macd":
                    df.ta.macd(close=close_col, append=True)
                elif ind_lower == "rsi":
                    df.ta.rsi(close=close_col, append=True)
                elif ind_lower == "bbands":
                    df.ta.bbands(close=close_col, append=True)
        else:
            return _error_result(
                "MISSING_COLUMN",
                "Could not find 'Close' column for technical analysis.",
                helper="technical_analysis",
            )

        # Convert datetime again if needed
        for col in df.select_dtypes(include=["datetime64"]).columns:
            df[col] = df[col].astype(str)

        return _ok_result(
            "technical_analysis",
            table={
                "columns": list(df.columns),
                "rows": df.values.tolist(),
            },
        )
    except Exception as e:
        log.exception("Error in technical_analysis")
        return _error_result("EXECUTION_ERROR", str(e), helper="technical_analysis")


def portfolio_tearsheet(
    params: dict[str, Any],
    data: Any,
    context: dict[str, Any],
    *,
    headers: bool = True,
    header_row: int = 0,
) -> dict[str, Any]:
    try:
        import pandas as pd
        import quantstats as qs  # type: ignore
    except ImportError:
        return _missing_package_error("portfolio_tearsheet", "quantstats")

    # Coerce inside the try so a bad grid is a result error, not a worker crash.
    try:
        res = _resolve_df(data, headers=headers, header_row=header_row)
        df = res.df

        if df.empty:
            return _error_result("INVALID_DATA", "Input data is empty.", helper="portfolio_tearsheet")

        numeric_df, dates = _returns_frame_from_df(df)
        if numeric_df.empty or numeric_df.shape[1] == 0:
            return _error_result("INVALID_DATA", "No numeric data columns found for portfolio tearsheet.", helper="portfolio_tearsheet")

        col_param = params.get("column")
        if col_param is not None and str(col_param).strip() != "":
            # An unknown column is INVALID_PARAMS. Averaging every column would
            # hide the typo.
            if col_param not in numeric_df.columns:
                return _error_result("INVALID_PARAMS", f"Specified column {col_param!r} not found in numeric columns.", helper="portfolio_tearsheet")
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

        # Drop NaT rows. A synthetic daily index is reported in the warning.
        synthetic_dates = False
        if dates is not None:
            valid_mask = dates.notna()
            if valid_mask.any():
                returns = returns.loc[valid_mask]
                dates = dates.loc[valid_mask]
                returns.index = dates
            else:
                synthetic_dates = True
                returns.index = pd.date_range("2024-01-01", periods=len(returns), freq="D")
        else:
            synthetic_dates = True
            returns.index = pd.date_range("2024-01-01", periods=len(returns), freq="D")

        metrics = qs.reports.metrics(returns, display=False)
        if hasattr(metrics, "iloc") and metrics.shape[1] >= 1:
            metrics_dict = {str(k): v for k, v in metrics.iloc[:, 0].to_dict().items()}
        elif isinstance(metrics, dict):
            metrics_dict = metrics
        else:
            metrics_dict = metrics.to_dict()

        extra: dict[str, Any] = {}
        if synthetic_dates:
            extra["warning"] = "Calculated using synthetic daily calendar starting 2024-01-01 (no valid date column found)."

        return _ok_result(
            "portfolio_tearsheet",
            metrics=metrics_dict,
            **extra,
        )
    except Exception as e:
        log.exception("Error in portfolio_tearsheet")
        return _error_result("EXECUTION_ERROR", str(e), helper="portfolio_tearsheet")


def efficient_frontier(
    params: dict[str, Any],
    data: Any,
    context: dict[str, Any],
    *,
    headers: bool = True,
    header_row: int = 0,
) -> dict[str, Any]:
    try:
        from pypfopt.efficient_frontier import EfficientFrontier  # type: ignore
        from pypfopt.expected_returns import mean_historical_return  # type: ignore
        from pypfopt.risk_models import CovarianceShrinkage  # type: ignore
    except ImportError:
        return _missing_package_error("efficient_frontier", "PyPortfolioOpt")

    # Coerce inside the try so a bad grid is a result error, not a worker crash.
    try:
        res = _resolve_df(data, headers=headers, header_row=header_row)
        df = res.df

        # Case-insensitive date column, shared with portfolio_tearsheet
        # (Date, datetime, timestamp, and the same names in other cases).
        numeric_df, _dates = _returns_frame_from_df(df)
        if numeric_df.empty or numeric_df.shape[1] < 2:
            return _error_result("INVALID_DATA", "Need at least two numeric assets for efficient frontier.", helper="efficient_frontier")

        mu = mean_historical_return(numeric_df, returns_data=True)
        S = CovarianceShrinkage(numeric_df, returns_data=True).ledoit_wolf()

        # weight_bounds and risk_free_rate are part of the requested frontier.
        rfr = float(params.get("risk_free_rate", 0.02))
        raw_bounds = params.get("weight_bounds")
        if isinstance(raw_bounds, (list, tuple)) and len(raw_bounds) == 2:
            bounds = (
                float(raw_bounds[0]) if raw_bounds[0] is not None else None,
                float(raw_bounds[1]) if raw_bounds[1] is not None else None,
            )
            ef = EfficientFrontier(mu, S, weight_bounds=bounds)
        else:
            ef = EfficientFrontier(mu, S)

        ef.max_sharpe(risk_free_rate=rfr)
        cleaned_weights = ef.clean_weights()

        return _ok_result(
            "efficient_frontier",
            weights=cleaned_weights,
        )
    except Exception as e:
        log.exception("Error in efficient_frontier")
        return _error_result("EXECUTION_ERROR", str(e), helper="efficient_frontier")


def run_quant(
    spec: dict[str, Any] | str,
    data: Any = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spec-driven dispatcher — single trusted entry for host RPC and Run Python Script."""
    parsed = _parse_trusted_spec(spec, helper_names=HELPER_NAMES, context=context)
    if isinstance(parsed, dict):
        return parsed
    helper, params, headers, header_row, ctx, _spec = parsed

    # Every data helper needs headers and header_row. Dropping them makes
    # tearsheet and frontier treat a header row as a price.
    if helper == "fetch_historical_data":
        result = fetch_historical_data(params, ctx)
    elif helper == "technical_analysis":
        result = technical_analysis(params, data, ctx, headers=headers, header_row=header_row)
    elif helper == "portfolio_tearsheet":
        result = portfolio_tearsheet(params, data, ctx, headers=headers, header_row=header_row)
    elif helper == "efficient_frontier":
        result = efficient_frontier(params, data, ctx, headers=headers, header_row=header_row)
    else:
        return _error_result("UNKNOWN_HELPER", f"Unknown helper {helper!r}", helper=helper)

    if isinstance(result, dict) and result.get("status") == "ok" and ctx:
        result["context"] = {k: v for k, v in ctx.items() if k in ("sheet_name", "range_a1", "task_hint")}
    return result
