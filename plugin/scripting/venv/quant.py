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

from plugin.scripting.calc_functions_common import QUANT_HELPER_NAMES as HELPER_NAMES
from plugin.scripting.venv.coerce import (
    error_result as _error_result,
    is_missing_value as _is_missing_value,
    missing_package_error as _missing_package_error,
    ok_result as _ok_result,
    parse_date_column,
    parse_trusted_spec as _parse_trusted_spec,
    resolve_df as _resolve_df,
)

log = logging.getLogger(__name__)

_INDICATOR_NAMES = ("macd", "rsi", "bbands")
_SUPPORTED_INDICATORS = frozenset(_INDICATOR_NAMES)
_DEFAULT_INDICATORS = list(_INDICATOR_NAMES)
_DATE_COLUMN_NAMES = frozenset({"date", "datetime", "timestamp"})


def _ticker_tokens(token: str) -> list[str]:
    """Split one cell on commas. ``AAPL, MSFT`` is two symbols; ``BRK.B`` is one."""
    return [part.strip() for part in token.split(",") if part.strip()]


def _flatten_tickers(raw_tickers: Any) -> list[str]:
    """Normalize raw ticker inputs into a clean list of non-empty strings."""
    if isinstance(raw_tickers, str):
        return _ticker_tokens(raw_tickers)
    if hasattr(raw_tickers, "values") and isinstance(raw_tickers.values, list):
        return [
            piece
            for row in raw_tickers.values
            if isinstance(row, (list, tuple))
            for cell in row
            if not _is_missing_value(cell)
            for piece in _ticker_tokens(str(cell))
        ]
    if isinstance(raw_tickers, (list, tuple)):
        clean_list: list[str] = []
        for item in raw_tickers:
            if isinstance(item, (list, tuple)):
                for sub in item:
                    if not _is_missing_value(sub):
                        clean_list.extend(_ticker_tokens(str(sub)))
            elif not _is_missing_value(item):
                clean_list.extend(_ticker_tokens(str(item)))
        return clean_list
    return []


def _date_column(df: Any) -> Any:
    return next(
        (col for col in df.columns if str(col).strip().lower() in _DATE_COLUMN_NAMES),
        None,
    )


def _order_by_date(df: Any) -> Any:
    """Sort ascending on a date column. Sheet order stays when there is none.

    MACD and RSI follow row order. A newest-first sheet used to be left as-is,
    so the path was reversed and the indicators were silently wrong. The date
    text itself is not rewritten; a helper key is dropped after the sort.
    """
    col = _date_column(df)
    if col is None:
        return df
    # What was wrong: pd.to_datetime on a mixed object column interpreted numeric
    # serials as nanoseconds since 1970 (e.g. 45293 -> 1970-01-01 00:00:00.000045293),
    # which sorted 1970 before calendar strings like 2024-01-01.
    # How it happened: a pure serial column sorts the same either way because nanoseconds
    # are monotonic, but mixed object columns (e.g. serial 45293, string 2024-01-01,
    # and not-a-date) sorted out of calendar order. resolve_df leaves that column as
    # object because not-a-date blocks all-datetime promotion.
    # Why this change fixes it: parse_date_column resolves Calc serials with epoch
    # 1899-12-30 and handles strings/invalid cells, sorting string 2024-01-01, then
    # serial 45293, then invalid cells.
    parsed = parse_date_column(df[col])
    if not bool(parsed.notna().any()):
        return df
    work = df.copy()
    key = "_wa_date_order"
    while key in work.columns:
        key += "_"
    work[key] = parsed
    work = work.sort_values(key, kind="mergesort", na_position="last")
    return work.drop(columns=[key])


def _stringify_datetimes(df: Any) -> None:
    """Turn datetime columns into text before ``tolist()``.

    What was wrong: only columns named Date or Datetime were stringified, and
    ``select_dtypes(datetime64)`` misses timezone-aware columns. ``tolist()``
    then yielded ``pandas.Timestamp``.
    How it happened: the host unpickler allows builtins and a few NumPy
    reconstructors, not pandas, so the frame failed before egress.
    Why this change: every datetime64 and datetimetz column becomes text,
    including an unnamed yfinance index that ``reset_index`` calls ``index``.
    """
    cols = list(df.select_dtypes(include=["datetime64", "datetimetz"]).columns)
    for col in cols:
        df[col] = df[col].astype(str)


def _column_label(col: Any) -> str:
    """Flatten a MultiIndex label. ``("Close", "AAPL")`` becomes ``Close / AAPL``.

    ``multi_level_index=False`` only drops the ticker level for a single
    ticker. Several tickers stay a MultiIndex, and keeping ``c[0]`` made
    every Close column the same name. ``reset_index`` pads the index name
    with an empty level, so blank parts are skipped.
    """
    if isinstance(col, tuple):
        parts = [str(part).strip() for part in col if str(part).strip()]
        return " / ".join(parts) if parts else ""
    return str(col)


def _returns_frame_from_df(df: Any) -> tuple[Any, Any]:
    """Extract date column if present and coerce remaining columns to numeric returns."""
    import pandas as pd

    date_col = _date_column(df)
    dates = None
    if date_col is not None:
        # What was wrong: pd.to_datetime interpreted Calc serial numbers (e.g. 45292)
        # and year integers as nanoseconds since 1970 instead of calendar dates.
        # How it happened: pd.to_datetime treats numeric values without unit as nanoseconds;
        # because the resulting 1970 timestamps were not NaT, portfolio_tearsheet annualized
        # a one-day span and never warned about missing dates.
        # Why this change fixes it: parse_date_column resolves Calc serials (epoch 1899-12-30)
        # and 1900-2200 year values into proper calendar dates.
        dates = parse_date_column(df[date_col])
        df = df.drop(columns=[date_col])

    # What was wrong: pd.to_numeric turns datetime64, timezone-aware datetimes, and
    # timedeltas into int64 nanoseconds (pandas 2.3.3: 2024-01-01 becomes 1704067200000000000).
    # How it happened: resolve_df promotes every all-date object column regardless of name
    # (Period, Unnamed: 0), and a DataFrame argument is copied with its dtypes kept, so
    # that column was averaged into the tearsheet and into mean_historical_return / Ledoit-Wolf.
    # Why this change fixes it: dropping those dtypes after the named date column is removed
    # keeps the date index and leaves real return columns, including a numeric Year.
    extra_time = list(
        df.select_dtypes(include=["datetime64", "datetimetz", "timedelta64"]).columns
    )
    if len(extra_time) > 0:
        df = df.drop(columns=extra_time)

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
        # multi_level_index arrived in yfinance 0.2.48. An older download()
        # raises TypeError for that keyword; retry without it. Any other
        # TypeError still falls through to EXECUTION_ERROR.
        # auto_adjust default flipped to True in yfinance 0.2.51. Leaving it
        # unset made Close unadjusted and added Adj Close on older installs,
        # so both the numbers and the column set depended on the package.
        # Pin True: OHLC is split-adjusted and there is no Adj Close column.
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
            return _error_result("NO_DATA", f"No data returned for ticker(s): {', '.join(tickers)}.", helper="fetch_historical_data")

        data = data.reset_index()
        _stringify_datetimes(data)

        columns = [_column_label(col) for col in data.columns]
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
        # Importing pandas_ta registers DataFrame.ta. The module object is unused.
        importlib.import_module("pandas_ta")
    except ImportError:
        return _missing_package_error("technical_analysis", "pandas-ta")

    raw_indicators = params.get("indicators", _DEFAULT_INDICATORS)
    # A string is one indicator name, not a sequence of characters.
    # An unknown name is INVALID_PARAMS. An int used to be replaced with
    # the default list and return ok.
    if raw_indicators is None:
        indicators = list(_DEFAULT_INDICATORS)
    elif isinstance(raw_indicators, str):
        indicators = [raw_indicators]
    elif isinstance(raw_indicators, (list, tuple)):
        indicators = list(raw_indicators)
    else:
        return _error_result(
            "INVALID_PARAMS",
            f"indicators must be a name or a list. Supported: {', '.join(_INDICATOR_NAMES)}.",
            helper="technical_analysis",
        )

    unknown = [str(ind) for ind in indicators if str(ind).strip().lower() not in _SUPPORTED_INDICATORS]
    if unknown:
        return _error_result(
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
        if close_col:
            import pandas as pd

            df[close_col] = pd.to_numeric(df[close_col], errors="coerce")
            df = _order_by_date(df)
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
            # hide the typo. A string "0" is the integer label 0 when the
            # frame was built with integer columns.
            if col_param not in numeric_df.columns:
                matches = [col for col in numeric_df.columns if str(col) == str(col_param)]
                if len(matches) != 1:
                    return _error_result("INVALID_PARAMS", f"Specified column {col_param!r} not found in numeric columns.", helper="portfolio_tearsheet")
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
            return _error_result("INVALID_DATA", "No valid numeric returns found.", helper="portfolio_tearsheet")

        # Drop NaT rows. A synthetic daily index is reported in the warning.
        synthetic_dates = False
        duplicate_dates = False
        if dates is not None:
            valid_mask = dates.notna()
            if valid_mask.any():
                returns = returns.loc[valid_mask]
                dates = dates.loc[valid_mask]
                returns.index = dates
                # Drawdown follows date order. A newest-first sheet used to
                # keep that order, so the path was reversed. Duplicate dates
                # are averaged, same as the forecast time-series prep.
                returns = returns.sort_index(kind="mergesort")
                if not returns.index.is_unique:
                    duplicate_dates = True
                    returns = returns.groupby(level=0).mean()
            else:
                synthetic_dates = True
                returns.index = pd.date_range("2024-01-01", periods=len(returns), freq="D")
        else:
            synthetic_dates = True
            returns.index = pd.date_range("2024-01-01", periods=len(returns), freq="D")

        metrics = qs.reports.metrics(returns, display=False)
        # What was wrong: a Series has .iloc but shape is (n,), so shape[1] raised
        # IndexError and the outer handler returned EXECUTION_ERROR instead of to_dict().
        # How it happened: the guard was written for quantstats' one-column DataFrame.
        # Why this change fixes it: isinstance(..., pd.DataFrame) keeps that path and
        # lets a Series hit the existing else.
        if isinstance(metrics, pd.DataFrame) and metrics.shape[1] >= 1:
            metrics_dict = {str(k): v for k, v in metrics.iloc[:, 0].to_dict().items()}
        elif isinstance(metrics, dict):
            metrics_dict = metrics
        else:
            metrics_dict = metrics.to_dict()

        extra: dict[str, Any] = {}
        if synthetic_dates:
            extra["warning"] = "Calculated using synthetic daily calendar starting 2024-01-01 (no valid date column found)."
        if duplicate_dates:
            note = "Averaged duplicate dates."
            if "warning" in extra:
                extra["warning"] = f"{extra['warning']} {note}"
            else:
                extra["warning"] = note

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
        # What was wrong: a hole in one asset still came back status ok.
        # How it happened: CovarianceShrinkage.ledoit_wolf does np.nan_to_num
        # (pypfopt/risk_models.py), so NaN becomes 0 and ±inf becomes ~1e308.
        # mean_historical_return skips NaN via prod() and count()
        # (pypfopt/expected_returns.py). Mean and covariance then disagree.
        # Why this change: drop a blank column first, then any row that is
        # not fully finite, so both calls see the same days. An empty column
        # dropped with how="any" would have thrown away every row.
        numeric_df = numeric_df.replace([math.inf, -math.inf], float("nan"))
        n_cols_before = int(numeric_df.shape[1])
        numeric_df = numeric_df.dropna(axis=1, how="all")
        n_empty_cols = n_cols_before - int(numeric_df.shape[1])
        n_rows_before = int(len(numeric_df))
        numeric_df = numeric_df.dropna()
        n_dropped_rows = n_rows_before - int(len(numeric_df))
        if numeric_df.empty or numeric_df.shape[1] < 2:
            return _error_result("INVALID_DATA", "Need at least two numeric assets for efficient frontier.", helper="efficient_frontier")
        notes: list[str] = []
        if n_empty_cols:
            noun = "column" if n_empty_cols == 1 else "columns"
            notes.append(f"Dropped {n_empty_cols} empty {noun}.")
        if n_dropped_rows:
            noun = "row" if n_dropped_rows == 1 else "rows"
            notes.append(f"Dropped {n_dropped_rows} {noun} with missing or non-finite returns.")

        # A non-numeric rate used to escape as EXECUTION_ERROR. A weight_bounds
        # value that was not a pair was ignored, and the library default ran.
        raw_rfr = params.get("risk_free_rate", 0.02)
        try:
            rfr = float(raw_rfr)
        except (TypeError, ValueError):
            return _error_result("INVALID_PARAMS", "risk_free_rate must be a finite number.", helper="efficient_frontier")
        if not math.isfinite(rfr):
            return _error_result("INVALID_PARAMS", "risk_free_rate must be a finite number.", helper="efficient_frontier")

        raw_bounds = params.get("weight_bounds")
        bounds: tuple[float | None, float | None] | None
        if raw_bounds is None:
            bounds = None
        elif isinstance(raw_bounds, (list, tuple)) and len(raw_bounds) == 2:
            try:
                bounds = (
                    None if raw_bounds[0] is None else float(raw_bounds[0]),
                    None if raw_bounds[1] is None else float(raw_bounds[1]),
                )
            except (TypeError, ValueError):
                return _error_result("INVALID_PARAMS", "weight_bounds must be a pair of finite numbers.", helper="efficient_frontier")
            if any(bound is not None and not math.isfinite(bound) for bound in bounds):
                return _error_result("INVALID_PARAMS", "weight_bounds must be a pair of finite numbers.", helper="efficient_frontier")
        else:
            return _error_result("INVALID_PARAMS", "weight_bounds must be a pair of finite numbers.", helper="efficient_frontier")

        mu = mean_historical_return(numeric_df, returns_data=True)
        S = CovarianceShrinkage(numeric_df, returns_data=True).ledoit_wolf()

        if bounds is None:
            ef = EfficientFrontier(mu, S)
        else:
            ef = EfficientFrontier(mu, S, weight_bounds=bounds)

        ef.max_sharpe(risk_free_rate=rfr)
        cleaned_weights = ef.clean_weights()

        extra: dict[str, Any] = {}
        if notes:
            extra["warning"] = " ".join(notes)
        return _ok_result(
            "efficient_frontier",
            weights=cleaned_weights,
            **extra,
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

    if result.get("status") == "ok" and ctx:
        result["context"] = {k: v for k, v in ctx.items() if k in ("sheet_name", "range_a1", "task_hint")}
    return result
