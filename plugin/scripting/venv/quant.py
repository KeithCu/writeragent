# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv quant compute — runs in user venv worker."""

from __future__ import annotations

import importlib
import logging
import math
from typing import Any

from plugin.scripting.helper_registry import helper
from plugin.scripting.venv.coerce import (
    date_column,
    flat_label,
    is_missing_value as _is_missing_value,
    parse_date_column,
    resolve_df as _resolve_df,
)
from plugin.scripting.venv.results import error_result
from plugin.scripting.venv.trusted_dispatch import (
    missing_package_error as _missing_package_error,
    ok_result as _ok_result,
    run_domain,
)

log = logging.getLogger(__name__)

_INDICATOR_NAMES = ("macd", "rsi", "bbands")


def _flatten_tickers(raw_tickers: Any) -> list[str]:
    """Normalize raw ticker inputs into a clean list of non-empty strings."""
    if isinstance(raw_tickers, str):
        return [part.strip() for part in raw_tickers.split(",") if part.strip()]

    skip_scalar = False
    rows: list[Any] | tuple[Any, ...]
    if hasattr(raw_tickers, "values") and isinstance(raw_tickers.values, list):
        rows = raw_tickers.values
        skip_scalar = True
    elif isinstance(raw_tickers, (list, tuple)):
        rows = raw_tickers
    else:
        return []

    clean: list[str] = []
    for row in rows:
        if isinstance(row, (list, tuple)):
            cells = row
        elif skip_scalar:
            continue
        else:
            cells = (row,)
        for cell in cells:
            if _is_missing_value(cell):
                continue
            clean.extend(part.strip() for part in str(cell).split(",") if part.strip())
    return clean


def _order_by_date(df: Any) -> Any:
    """Sort ascending on a date column. Sheet order stays when there is none.

    Sort by detected date column ascending so MACD/RSI match row order; newest-first sheets would otherwise reverse indicators. Date text is not rewritten.
    """
    col = date_column(df)
    if col is None:
        return df
    # parse_date_column resolves Calc serials (epoch 1899-12-30) and mixed dates for sorting.
    parsed = parse_date_column(df[col])
    order = parsed.reset_index(drop=True).sort_values(kind="mergesort", na_position="last").index
    return df.iloc[order]


def _stringify_datetimes(df: Any) -> None:
    """Turn datetime64 and datetimetz columns into text before tolist() for pickle egress."""
    for col in df.select_dtypes(include=["datetime64", "datetimetz"]).columns:
        df[col] = df[col].astype(str)


def _returns_frame_from_df(df: Any) -> tuple[Any, Any]:
    """Extract date column if present and coerce remaining columns to numeric returns."""
    import pandas as pd

    date_col = date_column(df)
    dates = None
    if date_col is not None:
        # parse_date_column resolves Calc serials and years into calendar dates.
        dates = parse_date_column(df[date_col])
        df = df.drop(columns=[date_col])

    # Drop datetime/timedelta columns before pd.to_numeric to prevent conversion to int64 nanoseconds.
    extra_time = list(
        df.select_dtypes(include=["datetime64", "datetimetz", "timedelta64"]).columns
    )
    if len(extra_time) > 0:
        df = df.drop(columns=extra_time)

    numeric_df = df.apply(pd.to_numeric, errors="coerce")
    numeric_df = numeric_df.dropna(how="all")
    return numeric_df, dates


@helper("quant")
def fetch_historical_data(params: dict[str, Any]) -> dict[str, Any]:
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        return _missing_package_error("fetch_historical_data", "yfinance")

    tickers = _flatten_tickers(params.get("tickers", []))
    if not tickers:
        return error_result("INVALID_PARAMS", "tickers parameter is required.", helper="fetch_historical_data")

    start_date = params.get("start_date")
    end_date = params.get("end_date")
    interval = params.get("interval", "1d")

    try:
        # progress=False: yfinance writes a progress bar to stdout and that
        # breaks IPC framing. An empty frame is NO_DATA, not ok.
        # multi_level_index needs yfinance 0.2.48+; an older download() raises
        # TypeError for that keyword, so retry without it. Any other TypeError
        # still falls through to EXECUTION_ERROR.
        # auto_adjust is pinned True because its default differs before 0.2.51
        # (unadjusted Close plus Adj Close): OHLC is split-adjusted and there is
        # no Adj Close column on every version.
        download_kwargs: dict[str, Any] = {
            "start": start_date,
            "end": end_date,
            "interval": interval,
            "progress": False,
            "auto_adjust": True,
            "multi_level_index": False,
        }
        try:
            data = yf.download(tickers, **download_kwargs)
        except TypeError as exc:
            if "multi_level_index" not in str(exc):
                raise
            download_kwargs.pop("multi_level_index")
            data = yf.download(tickers, **download_kwargs)
        if data is None or getattr(data, "empty", True):
            return error_result("NO_DATA", f"No data returned for ticker(s): {', '.join(tickers)}.", helper="fetch_historical_data")

        data = data.reset_index()
        _stringify_datetimes(data)

        columns = [flat_label(col) for col in data.columns]
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
        return error_result("EXECUTION_ERROR", str(e), helper="fetch_historical_data")


@helper("quant")
def technical_analysis(
    params: dict[str, Any],
    data: Any,
    *,
    headers: bool = True,
    header_row: int = 0,
) -> dict[str, Any]:
    try:
        # Importing pandas_ta registers DataFrame.ta. The module object is unused.
        importlib.import_module("pandas_ta")
    except ImportError:
        return _missing_package_error("technical_analysis", "pandas-ta")

    raw_indicators = params.get("indicators", _INDICATOR_NAMES)
    # A string is one indicator name, not a sequence of characters. An unknown
    # name, or a value that is neither a name nor a list (such as an int), is an
    # invalid-parameter error rather than a silent run of the default list.
    if raw_indicators is None:
        indicators = list(_INDICATOR_NAMES)
    elif isinstance(raw_indicators, str):
        indicators = [raw_indicators]
    elif isinstance(raw_indicators, (list, tuple)):
        indicators = list(raw_indicators)
    else:
        return error_result(
            "INVALID_PARAMS",
            f"indicators must be a name or a list. Supported: {', '.join(_INDICATOR_NAMES)}.",
            helper="technical_analysis",
        )

    unknown = [str(ind) for ind in indicators if str(ind).strip().lower() not in _INDICATOR_NAMES]
    if unknown:
        return error_result(
            "INVALID_PARAMS",
            f"Unknown indicator(s): {', '.join(unknown)}. Supported: {', '.join(_INDICATOR_NAMES)}.",
            helper="technical_analysis",
        )

    # Coerce inside the try so a bad grid is a result error, not a worker crash.
    try:
        res = _resolve_df(data, headers=headers, header_row=header_row)
        df = res.df

        # headers=False leaves integer column labels. str() before lower().
        close_col = next((c for c in df.columns if str(c).strip().lower() == "close"), None)
        if not close_col:
            return error_result(
                "MISSING_COLUMN",
                "Could not find 'Close' column for technical analysis.",
                helper="technical_analysis",
            )

        import pandas as pd

        df[close_col] = pd.to_numeric(df[close_col], errors="coerce")
        df = _order_by_date(df)
        for ind in indicators:
            ind_lower = str(ind).strip().lower()
            getattr(df.ta, ind_lower)(close=close_col, append=True)

        _stringify_datetimes(df)

        return _ok_result(
            "technical_analysis",
            table={
                "columns": list(df.columns),
                "rows": df.values.tolist(),
            },
        )
    except Exception as e:
        log.exception("Error in technical_analysis")
        return error_result("EXECUTION_ERROR", str(e), helper="technical_analysis")


@helper("quant")
def portfolio_tearsheet(
    params: dict[str, Any],
    data: Any,
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
            return error_result("INVALID_DATA", "Input data is empty.", helper="portfolio_tearsheet")

        numeric_df, dates = _returns_frame_from_df(df)
        if numeric_df.empty:
            return error_result("INVALID_DATA", "No numeric data columns found for portfolio tearsheet.", helper="portfolio_tearsheet")

        col_param = params.get("column")
        if col_param is not None and str(col_param).strip() != "":
            # An unknown column is INVALID_PARAMS. Averaging every column would
            # hide the typo. A string "0" is the integer label 0 when the
            # frame was built with integer columns.
            if col_param not in numeric_df.columns:
                matches = [col for col in numeric_df.columns if str(col) == str(col_param)]
                if len(matches) != 1:
                    return error_result("INVALID_PARAMS", f"Specified column {col_param!r} not found in numeric columns.", helper="portfolio_tearsheet")
                col_param = matches[0]
            returns = numeric_df[col_param]
        else:
            if numeric_df.shape[1] == 1:
                returns = numeric_df.iloc[:, 0]
            else:
                returns = numeric_df.mean(axis=1)

        returns = pd.to_numeric(returns, errors="coerce").dropna()
        if dates is not None:
            dates = dates.loc[returns.index]
        if returns.empty:
            return error_result("INVALID_DATA", "No valid numeric returns found.", helper="portfolio_tearsheet")

        # Drop NaT rows. A synthetic daily index is reported in the warning.
        warnings: list[str] = []
        valid_mask = None if dates is None else dates.notna()
        if valid_mask is not None and valid_mask.any():
            returns = returns.loc[valid_mask]
            dates = dates.loc[valid_mask]
            returns.index = dates
            # Sort returns by date ascending before drawdown so newest-first sheets are not reversed; duplicate dates are averaged.
            returns = returns.sort_index(kind="mergesort")
            if not returns.index.is_unique:
                returns = returns.groupby(level=0).mean()
                warnings.append("Averaged duplicate dates.")
        else:
            returns.index = pd.date_range("2024-01-01", periods=len(returns), freq="D")
            warnings.append(
                "Calculated using synthetic daily calendar starting 2024-01-01 (no valid date column found)."
            )

        metrics = qs.reports.metrics(returns, display=False)
        # Handle both DataFrame (multi-col) and Series (1D) metrics returns.
        if isinstance(metrics, pd.DataFrame) and metrics.shape[1] >= 1:
            metrics_dict = {str(k): v for k, v in metrics.iloc[:, 0].to_dict().items()}
        elif isinstance(metrics, dict):
            metrics_dict = metrics
        else:
            metrics_dict = metrics.to_dict()

        return _ok_result(
            "portfolio_tearsheet",
            metrics=metrics_dict,
            **({"warning": " ".join(warnings)} if warnings else {}),
        )
    except Exception as e:
        log.exception("Error in portfolio_tearsheet")
        return error_result("EXECUTION_ERROR", str(e), helper="portfolio_tearsheet")


@helper("quant")
def efficient_frontier(
    params: dict[str, Any],
    data: Any,
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
        numeric_df = _returns_frame_from_df(df)[0]
        # Drop fully empty columns then non-finite rows so mean and covariance see identical rows.
        numeric_df = numeric_df.replace([math.inf, -math.inf], float("nan"))
        n_cols_before = int(numeric_df.shape[1])
        numeric_df = numeric_df.dropna(axis=1, how="all")
        n_empty_cols = n_cols_before - int(numeric_df.shape[1])
        n_rows_before = int(len(numeric_df))
        numeric_df = numeric_df.dropna()
        n_dropped_rows = n_rows_before - int(len(numeric_df))
        if numeric_df.empty or numeric_df.shape[1] < 2:
            return error_result("INVALID_DATA", "Need at least two numeric assets for efficient frontier.", helper="efficient_frontier")
        notes: list[str] = []
        if n_empty_cols:
            noun = "column" if n_empty_cols == 1 else "columns"
            notes.append(f"Dropped {n_empty_cols} empty {noun}.")
        if n_dropped_rows:
            noun = "row" if n_dropped_rows == 1 else "rows"
            notes.append(f"Dropped {n_dropped_rows} {noun} with missing or non-finite returns.")

        # A non-numeric rate must not escape as EXECUTION_ERROR, and a
        # weight_bounds value that is not a pair is an invalid-parameter error
        # rather than being ignored in favour of the library default.
        raw_rfr = params.get("risk_free_rate", 0.02)
        try:
            rfr = float(raw_rfr)
            if not math.isfinite(rfr):
                raise ValueError
        except (TypeError, ValueError):
            return error_result(
                "INVALID_PARAMS",
                "risk_free_rate must be a finite number.",
                helper="efficient_frontier",
            )

        raw_bounds = params.get("weight_bounds")
        bounds: tuple[float | None, float | None] | None = None
        if raw_bounds is not None:
            try:
                if not (isinstance(raw_bounds, (list, tuple)) and len(raw_bounds) == 2):
                    raise ValueError
                lo = None if raw_bounds[0] is None else float(raw_bounds[0])
                hi = None if raw_bounds[1] is None else float(raw_bounds[1])
                if any(bound is not None and not math.isfinite(bound) for bound in (lo, hi)):
                    raise ValueError
                bounds = (lo, hi)
            except (TypeError, ValueError):
                return error_result(
                    "INVALID_PARAMS",
                    "weight_bounds must be a pair of finite numbers.",
                    helper="efficient_frontier",
                )

        mu = mean_historical_return(numeric_df, returns_data=True)
        S = CovarianceShrinkage(numeric_df, returns_data=True).ledoit_wolf()

        ef = EfficientFrontier(mu, S, **({} if bounds is None else {"weight_bounds": bounds}))

        ef.max_sharpe(risk_free_rate=rfr)
        cleaned_weights = ef.clean_weights()

        return _ok_result(
            "efficient_frontier",
            weights=cleaned_weights,
            **({"warning": " ".join(notes)} if notes else {}),
        )
    except Exception as e:
        log.exception("Error in efficient_frontier")
        return error_result("EXECUTION_ERROR", str(e), helper="efficient_frontier")


def run_quant(
    spec: dict[str, Any] | str,
    data: Any = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spec-driven dispatcher — single trusted entry for host RPC and Run Python Script."""
    return run_domain("quant", spec, data, context=context)
