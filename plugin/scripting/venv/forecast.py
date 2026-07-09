# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv forecast compute — runs in user venv worker."""

from __future__ import annotations

import logging
from typing import Any

from plugin.scripting.calc_functions_common import (
    FORECAST_HELPER_NAMES as HELPER_NAMES,
    FORECAST_MAX_TABLE_ROWS as MAX_TABLE_ROWS,
)
from plugin.scripting.venv.coerce import (
    CoerceResult,
    coerce_to_dataframe,
    ok_result as _ok_result,
    error_result as _error_result,
    missing_package_error as _missing_package_error,
    table_from_df as _table_from_df,
)
import logging

log = logging.getLogger(__name__)

_MIN_FORECAST_POINTS = 8
_MIN_DECOMPOSE_CYCLES = 2


def _resolve_df(data: Any, *, headers: bool = True, header_row: int = 0, sheet_hint: str | None = None) -> CoerceResult:
    if isinstance(data, CoerceResult):
        return data
    if hasattr(data, "columns") and hasattr(data, "index"):
        df = data.copy()
        meta: dict[str, Any] = {
            "n_rows": int(len(df)),
            "n_cols": int(len(df.columns)),
            "numeric_cols": [str(c) for c in df.select_dtypes(include="number").columns],
            "dropped_rows": 0,
        }
        if sheet_hint:
            meta["sheet_hint"] = sheet_hint
        return CoerceResult(df=df, metadata=meta)
    return coerce_to_dataframe(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)


def _require_statsmodels(helper: str) -> Any | None:
    try:
        import statsmodels  # noqa: F401

        return statsmodels
    except ImportError:
        return None


def _prepare_time_series(
    data: Any,
    *,
    date_col: str,
    value_col: str,
    headers: bool,
    header_row: int,
    sheet_hint: str | None,
    helper: str,
) -> tuple[CoerceResult | None, Any | None, dict[str, Any] | None]:
    """Return (coerced, series, error_dict)."""
    import pandas as pd

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    if date_col not in df.columns:
        return None, None, _error_result("UNKNOWN_COLUMN", f"Column {date_col!r} not found", helper=helper)
    if value_col not in df.columns:
        return None, None, _error_result("UNKNOWN_COLUMN", f"Column {value_col!r} not found", helper=helper)

    work = df[[date_col, value_col]].copy()
    work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
    work[value_col] = pd.to_numeric(work[value_col], errors="coerce")
    work = work.dropna(subset=[date_col, value_col]).sort_values(date_col)
    if work.empty:
        return None, None, _error_result("INSUFFICIENT_DATA", "No valid date/value rows after coercion", helper=helper)

    series = work.set_index(date_col)[value_col]
    if not series.index.is_monotonic_increasing:
        series = series.sort_index()
    if len(series) < _MIN_FORECAST_POINTS:
        return None, None, _error_result(
            "INSUFFICIENT_DATA",
            f"Need at least {_MIN_FORECAST_POINTS} observations; got {len(series)}",
            helper=helper,
        )
    return coerced, series, None


def _infer_seasonal_periods(series: Any, seasonal_periods: int | None) -> int | None:
    if seasonal_periods is not None and seasonal_periods > 1:
        return int(seasonal_periods)
    n = len(series)
    if n >= 24:
        return 12
    if n >= 14:
        return 7
    return None


def _forecast_moving_average(series: Any, *, periods: int) -> tuple[Any, str, dict[str, Any]]:
    import pandas as pd

    window = max(2, min(12, len(series) // 3))
    last_ma = float(series.rolling(window).mean().iloc[-1])
    last_date = series.index[-1]
    freq = pd.infer_freq(series.index)
    if freq is None:
        deltas = series.index.to_series().diff().dropna()
        step = deltas.median() if not deltas.empty else pd.Timedelta(days=30)
    else:
        step = pd.tseries.frequencies.to_offset(freq)
    future_dates = pd.date_range(start=last_date + step, periods=periods, freq=step)
    forecast_df = pd.DataFrame({"date": future_dates, "forecast": [last_ma] * periods})
    metrics = {"model": "moving_average", "periods": periods, "n_obs": int(len(series)), "window": window}
    return forecast_df, "moving_average", metrics


def _forecast_holt_winters(series: Any, *, periods: int, seasonal_periods: int) -> tuple[Any, str, dict[str, Any], list[str]]:
    import pandas as pd
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    model = ExponentialSmoothing(
        series,
        trend="add",
        seasonal="add",
        seasonal_periods=seasonal_periods,
    )
    fit = model.fit(optimized=True)
    forecast_vals = fit.forecast(periods)
    last_date = series.index[-1]
    freq = pd.infer_freq(series.index)
    if freq is None:
        deltas = series.index.to_series().diff().dropna()
        step = deltas.median() if not deltas.empty else pd.Timedelta(days=30)
    else:
        step = pd.tseries.frequencies.to_offset(freq)
    future_dates = pd.date_range(start=last_date + step, periods=periods, freq=step)

    rows: list[dict[str, Any]] = []
    flags: list[str] = []
    try:
        pred = fit.get_prediction(start=len(series), end=len(series) + periods - 1)
        summary = pred.summary_frame(alpha=0.05)
        for idx, dt in enumerate(future_dates):
            row: dict[str, Any] = {
                "date": dt,
                "forecast": float(forecast_vals.iloc[idx]) if hasattr(forecast_vals, "iloc") else float(forecast_vals[idx]),
            }
            if "mean_ci_lower" in summary.columns and "mean_ci_upper" in summary.columns:
                row["lower"] = float(summary["mean_ci_lower"].iloc[idx])
                row["upper"] = float(summary["mean_ci_upper"].iloc[idx])
            rows.append(row)
    except Exception:
        flags.append("confidence intervals unavailable")
        for idx, dt in enumerate(future_dates):
            val = float(forecast_vals.iloc[idx]) if hasattr(forecast_vals, "iloc") else float(forecast_vals[idx])
            rows.append({"date": dt, "forecast": val})

    forecast_df = pd.DataFrame(rows)
    metrics: dict[str, Any] = {
        "model": "holt_winters",
        "periods": periods,
        "n_obs": int(len(series)),
        "seasonal_periods": seasonal_periods,
    }
    if hasattr(fit, "aic"):
        metrics["aic"] = float(fit.aic)
    if hasattr(fit, "sse"):
        metrics["sse"] = float(fit.sse)
    return forecast_df, "holt_winters", metrics, flags


def _forecast_arima(series: Any, *, periods: int) -> tuple[Any, str, dict[str, Any], list[str]]:
    import pandas as pd
    from statsmodels.tsa.arima.model import ARIMA

    model = ARIMA(series, order=(1, 1, 1))
    fit = model.fit()
    pred = fit.get_forecast(steps=periods)
    forecast_vals = pred.predicted_mean
    conf = pred.conf_int()
    last_date = series.index[-1]
    freq = pd.infer_freq(series.index)
    if freq is None:
        deltas = series.index.to_series().diff().dropna()
        step = deltas.median() if not deltas.empty else pd.Timedelta(days=30)
    else:
        step = pd.tseries.frequencies.to_offset(freq)
    future_dates = pd.date_range(start=last_date + step, periods=periods, freq=step)

    rows = []
    for idx, dt in enumerate(future_dates):
        row = {"date": dt, "forecast": float(forecast_vals.iloc[idx])}
        if conf is not None and len(conf) > idx:
            row["lower"] = float(conf.iloc[idx, 0])
            row["upper"] = float(conf.iloc[idx, 1])
        rows.append(row)
    forecast_df = pd.DataFrame(rows)
    metrics = {"model": "arima", "periods": periods, "n_obs": int(len(series)), "order": "(1,1,1)"}
    if hasattr(fit, "aic"):
        metrics["aic"] = float(fit.aic)
    return forecast_df, "arima", metrics, []


def forecast_time_series(
    data: Any,
    *,
    periods: int = 12,
    model: str = "auto",
    date_col: str = "Date",
    value_col: str = "Value",
    seasonal_periods: int | None = None,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Forward predictions on a date-indexed series."""
    helper = "forecast_time_series"
    coerced, series, err = _prepare_time_series(
        data,
        date_col=date_col,
        value_col=value_col,
        headers=headers,
        header_row=header_row,
        sheet_hint=sheet_hint,
        helper=helper,
    )
    if err is not None:
        return err
    if series is None:
        return _error_result("INSUFFICIENT_DATA", "No valid time series", helper=helper)

    periods = max(1, int(periods))
    model_name = str(model or "auto").strip().lower()
    flags: list[str] = []

    if model_name == "moving_average":
        forecast_df, used_model, metrics = _forecast_moving_average(series, periods=periods)
    else:
        if _require_statsmodels(helper) is None and model_name != "moving_average":
            if model_name == "auto":
                forecast_df, used_model, metrics = _forecast_moving_average(series, periods=periods)
                flags.append("statsmodels missing; used moving_average fallback")
            else:
                return _missing_package_error(helper, "statsmodels")
        else:
            season = _infer_seasonal_periods(series, seasonal_periods)
            try:
                if model_name in ("auto", "holt_winters") and season is not None and len(series) >= season * _MIN_DECOMPOSE_CYCLES:
                    forecast_df, used_model, metrics, hw_flags = _forecast_holt_winters(series, periods=periods, seasonal_periods=season)
                    flags.extend(hw_flags)
                elif model_name in ("auto", "arima"):
                    forecast_df, used_model, metrics, arima_flags = _forecast_arima(series, periods=periods)
                    flags.extend(arima_flags)
                elif model_name == "holt_winters":
                    if season is None:
                        return _error_result(
                            "INSUFFICIENT_DATA",
                            "holt_winters requires seasonal_periods or enough data to infer seasonality",
                            helper=helper,
                        )
                    forecast_df, used_model, metrics, hw_flags = _forecast_holt_winters(series, periods=periods, seasonal_periods=season)
                    flags.extend(hw_flags)
                else:
                    return _error_result("FORECAST_FAILED", f"Unknown model {model_name!r}", helper=helper)
            except Exception as exc:
                if model_name == "auto":
                    try:
                        forecast_df, used_model, metrics, arima_flags = _forecast_arima(series, periods=periods)
                        flags.extend(arima_flags)
                        flags.append(f"primary model failed ({exc}); used arima")
                    except Exception:
                        forecast_df, used_model, metrics = _forecast_moving_average(series, periods=periods)
                        flags.append(f"statsmodels forecast failed ({exc}); used moving_average")
                else:
                    return _error_result("FORECAST_FAILED", str(exc), helper=helper)

    metrics.setdefault("model", used_model)
    table = _table_from_df(forecast_df, name="forecast")
    return _ok_result(helper, metrics=metrics, tables=[table], flags=flags, metadata=coerced.metadata if coerced else {})


def decompose_time_series(
    data: Any,
    *,
    date_col: str = "Date",
    value_col: str = "Value",
    model: str = "additive",
    period: int | None = None,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Trend / seasonal / residual decomposition via statsmodels."""
    import pandas as pd

    helper = "decompose_time_series"
    if _require_statsmodels(helper) is None:
        return _missing_package_error(helper, "statsmodels")

    coerced, series, err = _prepare_time_series(
        data,
        date_col=date_col,
        value_col=value_col,
        headers=headers,
        header_row=header_row,
        sheet_hint=sheet_hint,
        helper=helper,
    )
    if err is not None:
        return err
    if series is None:
        return _error_result("INSUFFICIENT_DATA", "No valid time series", helper=helper)

    decomp_model = str(model or "additive").strip().lower()
    if decomp_model not in ("additive", "multiplicative"):
        return _error_result("FORECAST_FAILED", f"model must be additive or multiplicative, got {decomp_model!r}", helper=helper)

    season = period if period is not None else _infer_seasonal_periods(series, None)
    if season is None or season < 2:
        return _error_result(
            "INSUFFICIENT_DATA",
            "decompose_time_series requires period or enough data to infer seasonality (>= 24 points for monthly=12)",
            helper=helper,
        )
    if len(series) < season * _MIN_DECOMPOSE_CYCLES:
        return _error_result(
            "INSUFFICIENT_DATA",
            f"Need at least {season * _MIN_DECOMPOSE_CYCLES} observations for period={season}; got {len(series)}",
            helper=helper,
        )

    try:
        from statsmodels.tsa.seasonal import seasonal_decompose

        result = seasonal_decompose(series, model=decomp_model, period=season)
    except Exception as exc:
        return _error_result("FORECAST_FAILED", str(exc), helper=helper)

    decomp_df = pd.DataFrame(
        {
            "date": series.index,
            "observed": series.values,
            "trend": result.trend,
            "seasonal": result.seasonal,
            "resid": result.resid,
        }
    )
    table = _table_from_df(decomp_df, name="decomposition")
    metrics = {
        "model": decomp_model,
        "period": season,
        "n_obs": int(len(series)),
    }
    return _ok_result(helper, metrics=metrics, tables=[table], metadata=coerced.metadata if coerced else {})


def _robust_z_scores(values: Any) -> Any:
    """MAD-based robust z-scores; fall back to std when MAD is zero."""
    import numpy as np

    arr = np.asarray(values, dtype=float)
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    if mad > 0:
        return 0.6745 * (arr - median) / mad
    std = float(np.std(arr))
    if std > 0:
        return (arr - float(np.mean(arr))) / std
    return np.zeros_like(arr)


def anomaly_detection_time_series(
    data: Any,
    *,
    date_col: str = "Date",
    value_col: str = "Value",
    period: int | None = None,
    method: str = "stl_residual",
    threshold: float = 3.0,
    include_all: bool = False,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Flag temporal outliers via STL residuals and robust z-scores."""
    import pandas as pd

    helper = "anomaly_detection_time_series"
    if _require_statsmodels(helper) is None:
        return _missing_package_error(helper, "statsmodels")

    method_name = str(method or "stl_residual").strip().lower()
    if method_name != "stl_residual":
        return _error_result("FORECAST_FAILED", f"Unknown method {method_name!r}; only stl_residual is supported", helper=helper)

    coerced, series, err = _prepare_time_series(
        data,
        date_col=date_col,
        value_col=value_col,
        headers=headers,
        header_row=header_row,
        sheet_hint=sheet_hint,
        helper=helper,
    )
    if err is not None:
        return err
    if series is None:
        return _error_result("INSUFFICIENT_DATA", "No valid time series", helper=helper)

    season = period if period is not None else _infer_seasonal_periods(series, None)
    if season is None or season < 2:
        return _error_result(
            "INSUFFICIENT_DATA",
            "anomaly_detection_time_series requires period or enough data to infer seasonality (>= 24 points for monthly=12)",
            helper=helper,
        )
    if len(series) < season * _MIN_DECOMPOSE_CYCLES:
        return _error_result(
            "INSUFFICIENT_DATA",
            f"Need at least {season * _MIN_DECOMPOSE_CYCLES} observations for period={season}; got {len(series)}",
            helper=helper,
        )

    try:
        from statsmodels.tsa.seasonal import STL

        stl_result = STL(series, period=season, robust=True).fit()
    except Exception as exc:
        return _error_result("FORECAST_FAILED", str(exc), helper=helper)

    expected = stl_result.trend + stl_result.seasonal
    resid = stl_result.resid
    scores = _robust_z_scores(resid.values)
    threshold_val = float(threshold)

    scores_df = pd.DataFrame(
        {
            "date": series.index,
            "observed": series.values,
            "expected": expected.values,
            "residual": resid.values,
            "score": scores,
        }
    )
    anomalies_df = scores_df[scores_df["score"].abs() > threshold_val].copy()
    tables = [_table_from_df(anomalies_df, name="anomalies")]
    if include_all:
        tables.append(_table_from_df(scores_df, name="all_scores"))

    metrics = {
        "n_anomalies": int(len(anomalies_df)),
        "period": season,
        "threshold": threshold_val,
        "method": method_name,
        "n_obs": int(len(series)),
    }
    return _ok_result(helper, metrics=metrics, tables=tables, metadata=coerced.metadata if coerced else {})


def _dispatch_helper(name: str, data: Any, params: dict[str, Any], *, headers: bool, header_row: int, context: dict[str, Any]) -> dict[str, Any]:
    sheet_hint = context.get("sheet_name") if isinstance(context.get("sheet_name"), str) else None
    common: dict[str, Any] = {"headers": headers, "header_row": header_row, "sheet_hint": sheet_hint}

    if name == "forecast_time_series":
        return forecast_time_series(
            data,
            periods=int(params.get("periods", 12)),
            model=str(params.get("model", "auto")),
            date_col=str(params.get("date_col", "Date")),
            value_col=str(params.get("value_col", "Value")),
            seasonal_periods=params.get("seasonal_periods"),
            **common,
        )
    if name == "decompose_time_series":
        return decompose_time_series(
            data,
            date_col=str(params.get("date_col", "Date")),
            value_col=str(params.get("value_col", "Value")),
            model=str(params.get("model", "additive")),
            period=params.get("period"),
            **common,
        )
    if name == "anomaly_detection_time_series":
        return anomaly_detection_time_series(
            data,
            date_col=str(params.get("date_col", "Date")),
            value_col=str(params.get("value_col", "Value")),
            period=params.get("period"),
            method=str(params.get("method", "stl_residual")),
            threshold=float(params.get("threshold", 3.0)),
            include_all=bool(params.get("include_all", False)),
            **common,
        )
    return _error_result("UNKNOWN_HELPER", f"Forecast helper {name!r} not found", helper=name)


def run_forecast(
    spec: dict[str, Any] | str,
    data: Any,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spec-driven dispatcher — single trusted entry for host RPC."""
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
        return _error_result("UNKNOWN_HELPER", f"Unknown helper {helper!r}", helper=helper)

    params: dict[str, Any] = spec_dict["params"] if isinstance(spec_dict.get("params"), dict) else {}
    headers = bool(spec_dict.get("headers", True))
    header_row = int(spec_dict.get("header_row", 0))
    ctx = context if isinstance(context, dict) else {}

    try:
        result = _dispatch_helper(helper, data, params, headers=headers, header_row=header_row, context=ctx)
    except Exception as exc:
        log.exception("Forecast helper %s failed", helper)
        return _error_result("FORECAST_FAILED", str(exc), helper=helper)

    if isinstance(result, dict) and result.get("status") == "ok" and ctx:
        result["context"] = {k: v for k, v in ctx.items() if k in ("sheet_name", "range_a1", "task_hint")}

    return result
