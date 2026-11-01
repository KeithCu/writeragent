# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv forecast compute — runs in user venv worker."""

from __future__ import annotations

import logging
from typing import Any

from plugin.scripting.calc_functions_common import (
    ANALYSIS_DATA_TABLE_ROWS as DATA_TABLE_ROWS,
    FORECAST_HELPER_NAMES as HELPER_NAMES,
)
from plugin.scripting.venv.coerce import (
    CoerceResult,
    coerce_bool,
    coerce_float,
    coerce_int,
    coerce_optional_int,
    parse_date_column,
    resolve_df as _resolve_df,
    table_from_df as _table_from_df,
)
from plugin.scripting.venv.trusted_dispatch import (
    dispatch_common_kwargs,
    error_result as _error_result,
    missing_package_error as _missing_package_error,
    ok_result as _ok_result,
    run_trusted_spec,
)

log = logging.getLogger(__name__)

_MIN_FORECAST_POINTS = 8
# Explicit holt_winters and auto used a literal 2. They share this constant
# with _infer_seasonal_periods, _resolve_period, and _cycle_shortfall so the cycle rule cannot drift.
_MIN_DECOMPOSE_CYCLES = 2
_MAX_FORECAST_PERIODS = 10_000



def _require_statsmodels() -> Any | None:
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
    # resolve_df returns the same CoerceResult when the caller passed one.
    # rows_dropped and duplicate_dates_collapsed were written into that dict, so
    # a later helper that returns the same metadata reported this call's drops.
    # Copy first; the caller's dict stays unchanged.
    coerced = CoerceResult(df=coerced.df, metadata=dict(coerced.metadata))
    df = coerced.df
    if date_col not in df.columns:
        return None, None, _error_result("UNKNOWN_COLUMN", f"Column {date_col!r} not found", helper=helper)
    if value_col not in df.columns:
        return None, None, _error_result("UNKNOWN_COLUMN", f"Column {value_col!r} not found", helper=helper)

    # Equal names make df[[date_col, value_col]] a frame with duplicate labels.
    # work[date_col] is then a DataFrame, and parse_date_column raises
    # ValueError ("Data must be 1-dimensional"). run_forecast reported that as
    # FORECAST_FAILED. Reject it before the selection.
    if date_col == value_col:
        return None, None, _error_result(
            "INVALID_PARAM",
            f"date_col and value_col must be different columns, got {date_col!r}",
            helper=helper,
        )

    work = df[[date_col, value_col]].copy()
    work[date_col] = parse_date_column(work[date_col])
    work[value_col] = pd.to_numeric(work[value_col], errors="coerce")
    n_before_drop = len(work)
    work = work.dropna(subset=[date_col, value_col]).sort_values(date_col)
    dropped = n_before_drop - len(work)
    if dropped > 0:
        coerced.metadata["rows_dropped"] = int(dropped)
    if work.empty:
        return None, None, _error_result("INSUFFICIENT_DATA", "No valid date/value rows after coercion", helper=helper)

    # STL and Holt-Winters require a unique index. Two sheet rows on the same
    # day used to be left as-is and then either error or distort the season.
    n_before = len(work)
    work = work.groupby(date_col, as_index=False, sort=True)[value_col].mean()
    collapsed = n_before - len(work)
    if collapsed:
        coerced.metadata["duplicate_dates_collapsed"] = int(collapsed)

    series = work.set_index(date_col)[value_col]
    if len(series) < _MIN_FORECAST_POINTS:
        return None, None, _error_result(
            "INSUFFICIENT_DATA",
            f"Need at least {_MIN_FORECAST_POINTS} observations; got {len(series)}",
            helper=helper,
        )
    return coerced, series, None


def _dropped_row_flags(coerced: CoerceResult) -> list[str]:
    dropped = coerced.metadata.get("rows_dropped")
    if dropped and int(dropped) > 0:
        return [f"Dropped {int(dropped)} rows with an unparseable date or value"]
    return []


# One seasonal cycle for a regular calendar. Annual and sub-hour series have
# no cycle we can safely guess, so they stay trend-only unless the caller
# passes a period. BH is business hours 09:00-17:00, and 24 is neither a
# business day nor a business week of those hours, so those series stay trend-only.
_SEASON_BY_FREQ_BASE: dict[str, int] = {
    **dict.fromkeys(("D", "C"), 7),
    **dict.fromkeys(("B",), 5),
    **dict.fromkeys(("W",), 52),
    **dict.fromkeys(("M", "ME", "MS", "BM", "BMS", "BME"), 12),
    **dict.fromkeys(("Q", "QE", "QS", "BQ", "BQS", "BQE"), 4),
    **dict.fromkeys(("SME", "H"), 24),
}


def _freq_base(freq: str) -> str:
    return str(freq).split("-", 1)[0].upper()


def _weekday_daily(series: Any, step: Any) -> bool:
    """True when the typical gap is one day and the index has no weekend."""
    days = float(step.total_seconds() / 86400.0)
    if not 0.5 <= days <= 1.5:
        return False
    weekdays = {int(day) for day in series.index.day_of_week}
    return bool(weekdays) and weekdays.isdisjoint({5, 6})


def _period_from_gaps(series: Any) -> int | None:
    """Guess a cycle when ``infer_freq`` fails (weekend gaps, short spans)."""
    deltas = series.index.to_series().diff().dropna()
    if deltas.empty:
        return None
    step = deltas.median()
    days = float(step.total_seconds() / 86400.0)
    if _weekday_daily(series, step):
        return 5
    if 0.5 <= days <= 1.5:
        return 7
    if 6 <= days <= 8:
        return 52
    if 27 <= days <= 32:
        return 12
    if 85 <= days <= 95:
        return 4
    return None


def _candidate_seasonal_period(series: Any) -> int | None:
    import pandas as pd

    freq = pd.infer_freq(series.index)
    if freq is not None:
        mapped = _SEASON_BY_FREQ_BASE.get(_freq_base(freq))
        if mapped is not None:
            return mapped
        # Known but non-seasonal (yearly, minutes): do not fall through to a
        # gap guess that might invent a cycle.
        return None
    return _period_from_gaps(series)


def _cycle_shortfall(series: Any, season: int) -> str | None:
    """The shared two-cycle error, or None when the series is long enough."""
    need = season * _MIN_DECOMPOSE_CYCLES
    if len(series) < need:
        return f"Need at least {need} observations for period={season}; got {len(series)}"
    return None


def _infer_seasonal_periods(series: Any, seasonal_periods: int | None) -> int | None:
    """Season length from the dates, or the caller's explicit period.

    The previous rule used the row count alone (``n >= 24`` → 12, ``n >= 14``
    → 7). A 30-row daily sheet was fit as a 12-day season, and future dates
    were still daily because those used ``infer_freq`` separately. An explicit
    period still wins. An inferred period is used only when the series covers
    at least two full cycles; otherwise the caller stays trend-only.
    """
    if seasonal_periods is not None and int(seasonal_periods) > 1:
        return int(seasonal_periods)
    candidate = _candidate_seasonal_period(series)
    if candidate is None:
        return None
    if _cycle_shortfall(series, candidate) is not None:
        return None
    return candidate


def _seasonality_skip_reason(series: Any) -> str:
    """Why auto did not fit a seasonal model.

    A known frequency with no entry in _SEASON_BY_FREQ_BASE (yearly, minutes)
    used to share the "could not infer" sentence with a series whose frequency
    could not be inferred at all.
    """
    candidate = _candidate_seasonal_period(series)
    if candidate is None:
        import pandas as pd

        freq = pd.infer_freq(series.index)
        if freq is not None:
            return f"frequency {freq} has no seasonal period to infer; used trend-only"
        return "could not infer a seasonal period from the dates; used trend-only"
    need = candidate * _MIN_DECOMPOSE_CYCLES
    return (
        f"not enough cycles for a seasonal model "
        f"(need {need} observations for period {candidate}); used trend-only"
    )


def _duplicate_date_flags(coerced: CoerceResult) -> list[str]:
    collapsed = coerced.metadata.get("duplicate_dates_collapsed")
    if not collapsed:
        return []
    return [f"Aggregated {int(collapsed)} duplicate date rows by mean"]


def _spacing(series: Any) -> tuple[Any, str, bool]:
    import pandas as pd

    freq = pd.infer_freq(series.index)
    if freq is not None:
        return pd.tseries.frequencies.to_offset(freq), str(freq), False

    deltas = series.index.to_series().diff().dropna()
    pos_deltas = deltas[deltas > pd.Timedelta(0)]
    # Prepared series have at least 8 unique sorted timestamps, so these diffs
    # are non-empty and strictly positive. The 30-day fallback never ran.
    step = pos_deltas.median()

    if _weekday_daily(series, step):
        return pd.tseries.frequencies.to_offset("B"), "B", False

    days = float(step.total_seconds() / 86400.0)
    max_delta = pos_deltas.max()
    min_delta = pos_deltas.min()
    irregular = bool((max_delta > 1.5 * step) or (min_delta < 0.5 * step))

    # What was wrong: day >= 28 selected MonthEnd / QuarterEnd / YearEnd for
    # dates that are not on that anchor.
    # How: those offsets roll to the current period end, so 2023-12-28 + MonthEnd
    # is 2023-12-31, and the same roll happens for 28 March (QuarterEnd) and
    # 28 December (YearEnd).
    # Why this fixes it: the anchored offset is used only when every observation
    # is on it. Anything else keeps the day of month via DateOffset. The whole
    # index is required because non-leap 28 February is a month end even when the
    # series is "the 28th."
    if 27 <= days <= 32:
        if series.index.is_month_start.all():
            return pd.tseries.frequencies.to_offset("MS"), "MS", irregular
        if series.index.is_month_end.all():
            return pd.tseries.frequencies.to_offset("ME"), "ME", irregular
        return pd.DateOffset(months=1), "month", irregular

    if 85 <= days <= 95:
        if series.index.is_quarter_end.all():
            return pd.tseries.frequencies.to_offset("QE"), "QE", irregular
        if series.index.is_quarter_start.all():
            return pd.tseries.frequencies.to_offset("QS"), "QS", irregular
        return pd.DateOffset(months=3), "quarter", irregular

    if 360 <= days <= 370:
        if series.index.is_year_end.all():
            return pd.tseries.frequencies.to_offset("YE"), "YE", irregular
        if series.index.is_year_start.all():
            return pd.tseries.frequencies.to_offset("YS"), "YS", irregular
        return pd.DateOffset(years=1), "year", irregular

    return step, "median_step", irregular


def _future_index(series: Any, periods: int) -> tuple[Any, str, bool]:
    """Dates after the last observation, frequency label, and irregular flag."""
    import pandas as pd

    step, label, irregular = _spacing(series)
    last_date = series.index[-1]
    # date_range applies DateOffset iteratively, which clips a short month and
    # then keeps the clipped day (e.g. 2023-01-30 -> 2023-02-28 -> 2023-03-28).
    # Stepping directly from the original last timestamp (last + step * k) keeps 2023-03-30.
    if label in ("month", "quarter", "year"):
        future = pd.DatetimeIndex([last_date + step * k for k in range(1, periods + 1)])
    else:
        future = pd.date_range(start=last_date + step, periods=periods, freq=step)
    return future, label, irregular


def _resolve_period(series: Any, period: Any, helper: str) -> tuple[int | None, dict[str, Any] | None]:
    try:
        coerced_period = coerce_optional_int(period, name="period")
    except ValueError as exc:
        return None, _error_result("INVALID_PARAM", str(exc), helper=helper)

    # period=1 is an explicit bad parameter. It used to fail `season < 2` and
    # come back as INSUFFICIENT_DATA ("needs an explicit period"), which is the
    # message for a missing inference. Same rule as seasonal_periods.
    if coerced_period is not None and coerced_period <= 1:
        return None, _error_result(
            "INVALID_PARAM",
            "period must be an integer greater than 1",
            helper=helper,
        )

    season = coerced_period if coerced_period is not None else _infer_seasonal_periods(series, None)
    if season is None or season < 2:
        return None, _error_result(
            "INSUFFICIENT_DATA",
            f"{helper} needs an explicit period, or a date frequency with at least two full seasonal cycles",
            helper=helper,
        )
    shortfall = _cycle_shortfall(series, season)
    if shortfall is not None:
        return None, _error_result("INSUFFICIENT_DATA", shortfall, helper=helper)
    return season, None


def _prepare_time_series_with_period(
    data: Any,
    *,
    date_col: str,
    value_col: str,
    headers: bool,
    header_row: int,
    sheet_hint: str | None,
    helper: str,
    period: Any,
) -> tuple[CoerceResult | None, Any | None, int | None, dict[str, Any] | None]:
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
        return None, None, None, err
    assert coerced is not None and series is not None

    season, period_err = _resolve_period(series, period, helper)
    if period_err is not None:
        return None, None, None, period_err
    assert season is not None
    return coerced, series, season, None


def _forecast_moving_average(series: Any, *, periods: int, future_dates: Any, freq_label: str) -> tuple[Any, dict[str, Any]]:
    import pandas as pd

    window = max(2, min(12, len(series) // 3))
    last_ma = float(series.rolling(window).mean().iloc[-1])
    forecast_df = pd.DataFrame({"date": future_dates, "forecast": [last_ma] * periods})
    metrics = {
        "model": "moving_average",
        "periods": periods,
        "n_obs": int(len(series)),
        "window": window,
        "freq": freq_label,
    }
    return forecast_df, metrics


def _fallback_ma(series: Any, periods: int, flags: list[str], future_dates: Any, freq_label: str, reason: str | None) -> tuple[Any, dict[str, Any]]:
    """Moving average plus its flags.

    `reason` is appended before the fixed interval sentence. None means there
    is no reason (explicit moving_average with no seasonal_periods). The
    Holt-Winters fallback must pass the Holt-Winters exception; the inner
    ARIMA exception is discarded on purpose.
    """
    forecast_df, metrics = _forecast_moving_average(series, periods=periods, future_dates=future_dates, freq_label=freq_label)
    if reason is not None:
        flags.append(reason)
    flags.append("moving_average has no prediction interval")
    return forecast_df, metrics


# HoltWintersResults has forecast/predict/simulate and no get_prediction.
# get_prediction lives on ETSResults / statespace results (statsmodels
# tsa.holtwinters.results.HoltWintersResults). Calling it always raised
# AttributeError, which was swallowed as "confidence intervals unavailable".
_HW_SIM_REPETITIONS = 400
_HW_INTERVALS_OMITTED_FLAG = (
    "Holt-Winters prediction intervals are omitted; the forecast column is the point forecast."
)
_HW_INTERVALS_NOTE_MESSAGE = (
    "HoltWintersResults has no analytic get_prediction; simulate() did not return a 95% band."
)


def _holt_winters_intervals(fit: Any, periods: int) -> tuple[Any, Any] | None:
    """95% band from ``HoltWintersResults.simulate`` state-space paths.

    Returns ``(lower, upper)`` aligned by position with the horizon, or
    ``None`` when simulation cannot build a band. Callers must say so in
    ``interval_note`` instead of inventing lower/upper.
    """
    import numpy as np
    import pandas as pd

    try:
        sims = fit.simulate(
            periods,
            repetitions=_HW_SIM_REPETITIONS,
            error="add",
            rng=np.random.default_rng(0),
        )
    except TypeError:
        # statsmodels before 0.14 named this argument random_state.
        try:
            sims = fit.simulate(
                periods,
                repetitions=_HW_SIM_REPETITIONS,
                error="add",
                random_state=0,
            )
        except Exception:
            log.exception("Holt-Winters interval simulation failed")
            return None
    except Exception:
        log.exception("Holt-Winters interval simulation failed")
        return None

    if not isinstance(sims, pd.DataFrame) or len(sims) != periods or sims.shape[1] < 2:
        return None
    lower = sims.quantile(0.025, axis=1)
    upper = sims.quantile(0.975, axis=1)
    if len(lower) != periods or len(upper) != periods:
        return None
    if not np.isfinite(lower.to_numpy()).all() or not np.isfinite(upper.to_numpy()).all():
        return None
    return lower, upper


def _forecast_holt_winters(series: Any, *, periods: int, seasonal_periods: int, future_dates: Any, freq_label: str) -> tuple[Any, dict[str, Any], list[str]]:
    import numpy as np
    import pandas as pd
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    # Positional endog: a gapped DatetimeIndex has no frequency, statsmodels
    # raises No supported index is available, and auto used to substitute a flat
    # moving average. Dates come from _future_index.
    endog = pd.Series(series.to_numpy(), index=pd.RangeIndex(len(series)))
    model = ExponentialSmoothing(
        endog,
        trend="add",
        seasonal="add",
        seasonal_periods=seasonal_periods,
    )
    fit = model.fit(optimized=True)
    forecast_vals = fit.forecast(periods)

    flags: list[str] = []
    intervals = _holt_winters_intervals(fit, periods)
    point = np.asarray(forecast_vals, dtype=float)
    if intervals is None:
        flags.append(_HW_INTERVALS_OMITTED_FLAG)
        forecast_df = pd.DataFrame({"date": future_dates, "forecast": point})
    else:
        lower, upper = intervals
        forecast_df = pd.DataFrame({
            "date": future_dates,
            "forecast": point,
            "lower": np.asarray(lower, dtype=float),
            "upper": np.asarray(upper, dtype=float),
        })

    metrics: dict[str, Any] = {
        "model": "holt_winters",
        "periods": periods,
        "n_obs": int(len(series)),
        "seasonal_periods": seasonal_periods,
        "freq": freq_label,
    }
    if hasattr(fit, "aic"):
        metrics["aic"] = float(fit.aic)
    if hasattr(fit, "sse"):
        metrics["sse"] = float(fit.sse)
    if intervals is None:
        metrics["interval_note"] = {
            "available": False,
            "method": "HoltWintersResults.simulate",
            "message": _HW_INTERVALS_NOTE_MESSAGE,
        }
    return forecast_df, metrics, flags


def _forecast_arima(series: Any, *, periods: int, future_dates: Any, freq_label: str) -> tuple[Any, dict[str, Any]]:
    import numpy as np
    import pandas as pd
    from statsmodels.tsa.arima.model import ARIMA

    # Positional endog: a gapped DatetimeIndex has no frequency, statsmodels
    # raises No supported index is available, and auto used to substitute a flat
    # moving average. Dates come from _future_index.
    endog = pd.Series(series.to_numpy(), index=pd.RangeIndex(len(series)))
    model = ARIMA(endog, order=(1, 1, 1))
    fit = model.fit()
    pred = fit.get_forecast(steps=periods)
    forecast_vals = pred.predicted_mean
    conf = pred.conf_int()

    point = np.asarray(forecast_vals, dtype=float)
    columns: dict[str, Any] = {"date": future_dates, "forecast": point}
    if conf is not None and len(conf) >= periods:
        columns["lower"] = np.asarray(conf.iloc[:periods, 0], dtype=float)
        columns["upper"] = np.asarray(conf.iloc[:periods, 1], dtype=float)
    forecast_df = pd.DataFrame(columns)
    metrics: dict[str, Any] = {
        "model": "arima",
        "periods": periods,
        "n_obs": int(len(series)),
        "order": "(1,1,1)",
        "freq": freq_label,
    }
    if hasattr(fit, "aic"):
        metrics["aic"] = float(fit.aic)
    return forecast_df, metrics


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
    try:
        periods_val = coerce_int(periods, name="periods")
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper=helper)

    try:
        seasonal_periods_val = coerce_optional_int(seasonal_periods, name="seasonal_periods")
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper=helper)

    # periods=0 used to become 1 via max(1, ...) and return one row with no flag.
    # seasonal_periods <= 1 is already INVALID_PARAM. Reject the same class here,
    # before preparing the series, so it wins over a bad model name.
    if periods_val < 1:
        return _error_result(
            "INVALID_PARAM",
            "periods must be an integer greater than 0",
            helper=helper,
        )

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
    assert coerced is not None and series is not None

    capped = periods_val > _MAX_FORECAST_PERIODS
    periods_clamped = min(periods_val, _MAX_FORECAST_PERIODS)
    flags: list[str] = _duplicate_date_flags(coerced)
    if capped:
        flags.append(f"periods capped at {_MAX_FORECAST_PERIODS}")

    model_name = str(model or "auto").strip().lower()
    if model_name not in ("auto", "holt_winters", "arima", "moving_average"):
        return _error_result(
            "INVALID_PARAM",
            f"Unknown model {model_name!r}; must be one of auto, holt_winters, arima, moving_average",
            helper=helper,
        )

    if seasonal_periods_val is not None and seasonal_periods_val <= 1:
        return _error_result(
            "INVALID_PARAM",
            "seasonal_periods must be an integer greater than 1",
            helper=helper,
        )

    # The 10_000 cap is frequency-blind: a yearly series steps past
    # pd.Timestamp.max, and only the statsmodels branches catch the raise.
    # Build the horizon once here so every model returns a parameter error.
    try:
        future_dates, freq_label, irregular_dates = _future_index(series, periods_clamped)
    except (ValueError, OverflowError):
        return _error_result(
            "INVALID_PARAM",
            f"periods={periods_val} is past the supported date range for this series",
            helper=helper,
        )

    if model_name == "moving_average":
        reason = "seasonal_periods does not apply to moving_average; ignored" if seasonal_periods_val is not None else None
        forecast_df, metrics = _fallback_ma(series, periods_clamped, flags, future_dates, freq_label, reason)
    elif _require_statsmodels() is None:
        if model_name == "auto":
            forecast_df, metrics = _fallback_ma(series, periods_clamped, flags, future_dates, freq_label, "statsmodels missing; used moving_average fallback")
        else:
            return _missing_package_error(helper, "statsmodels")
    elif model_name == "holt_winters":
        season = _infer_seasonal_periods(series, seasonal_periods_val)
        if season is None:
            return _error_result(
                "INSUFFICIENT_DATA",
                "holt_winters requires seasonal_periods or enough data to infer seasonality",
                helper=helper,
            )
        shortfall = _cycle_shortfall(series, season)
        if shortfall is not None:
            return _error_result("INSUFFICIENT_DATA", shortfall, helper=helper)
        try:
            forecast_df, metrics, hw_flags = _forecast_holt_winters(series, periods=periods_clamped, seasonal_periods=season, future_dates=future_dates, freq_label=freq_label)
            flags.extend(hw_flags)
        except Exception as exc:
            return _error_result("FORECAST_FAILED", str(exc), helper=helper)
    elif model_name == "arima":
        try:
            forecast_df, metrics = _forecast_arima(series, periods=periods_clamped, future_dates=future_dates, freq_label=freq_label)
            if seasonal_periods_val is not None:
                flags.append("seasonal_periods does not apply to non-seasonal ARIMA(1,1,1); ignored")
        except Exception as exc:
            return _error_result("FORECAST_FAILED", str(exc), helper=helper)
    else:
        # model_name == "auto"
        season = _infer_seasonal_periods(series, seasonal_periods_val)
        has_enough = season is not None and _cycle_shortfall(series, season) is None
        if has_enough:
            assert season is not None
            try:
                forecast_df, metrics, hw_flags = _forecast_holt_winters(series, periods=periods_clamped, seasonal_periods=season, future_dates=future_dates, freq_label=freq_label)
                flags.extend(hw_flags)
            except Exception as exc:
                try:
                    forecast_df, metrics = _forecast_arima(series, periods=periods_clamped, future_dates=future_dates, freq_label=freq_label)
                    flags.append(f"primary model failed ({exc}); used arima")
                except Exception:
                    forecast_df, metrics = _fallback_ma(series, periods_clamped, flags, future_dates, freq_label, f"statsmodels forecast failed ({exc}); used moving_average")
        else:
            if seasonal_periods_val is not None:
                need = seasonal_periods_val * _MIN_DECOMPOSE_CYCLES
                flags.append(
                    f"seasonal_periods={seasonal_periods_val} needs at least {need} observations; got {len(series)}; used trend-only"
                )
            else:
                flags.append(_seasonality_skip_reason(series))
            try:
                forecast_df, metrics = _forecast_arima(series, periods=periods_clamped, future_dates=future_dates, freq_label=freq_label)
            except Exception as exc:
                forecast_df, metrics = _fallback_ma(series, periods_clamped, flags, future_dates, freq_label, f"statsmodels forecast failed ({exc}); used moving_average")

    if irregular_dates:
        flags.append("Dates are irregular, so the forecast assumes equal spacing between observations.")
    flags.extend(_dropped_row_flags(coerced))

    table = _table_from_df(forecast_df, name="forecast", max_rows=periods_clamped)
    return _ok_result(helper, metrics=metrics, tables=[table], flags=flags, metadata=coerced.metadata)


def _append_truncation_flag(flags: list[str], table: dict[str, Any], row_count: int, label: str) -> None:
    if table.get("truncated"):
        flags.append(f"{label} shows the first {DATA_TABLE_ROWS} of {row_count} rows")


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
    if _require_statsmodels() is None:
        return _missing_package_error(helper, "statsmodels")

    decomp_model = str(model or "additive").strip().lower()
    if decomp_model not in ("additive", "multiplicative"):
        return _error_result("INVALID_PARAM", f"model must be additive or multiplicative, got {decomp_model!r}", helper=helper)

    coerced, series, season, err = _prepare_time_series_with_period(
        data,
        date_col=date_col,
        value_col=value_col,
        headers=headers,
        header_row=header_row,
        sheet_hint=sheet_hint,
        helper=helper,
        period=period,
    )
    if err is not None:
        return err
    assert coerced is not None and series is not None and season is not None

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
    table = _table_from_df(decomp_df, name="decomposition", max_rows=DATA_TABLE_ROWS)
    metrics = {
        "model": decomp_model,
        "period": season,
        "n_obs": int(len(series)),
    }
    flags = _duplicate_date_flags(coerced) + _dropped_row_flags(coerced)
    _append_truncation_flag(flags, table, len(decomp_df), "decomposition table")
    return _ok_result(
        helper,
        metrics=metrics,
        tables=[table],
        flags=flags,
        metadata=coerced.metadata,
    )


# Approximately Φ^{-1}(0.75), the factor that makes a MAD-based score
# comparable to a standard deviation for normal residuals. The literal must stay 0.6745.
_NORMAL_MAD_SCALE = 0.6745


def _robust_z_scores(values: Any) -> Any:
    """MAD-based robust z-scores; fall back to std when MAD is zero."""
    import numpy as np

    arr = np.asarray(values, dtype=float)
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    if mad > 0:
        return _NORMAL_MAD_SCALE * (arr - median) / mad
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
    method_name = str(method or "stl_residual").strip().lower()
    if method_name != "stl_residual":
        return _error_result("INVALID_PARAM", f"Unknown method {method_name!r}; only stl_residual is supported", helper=helper)

    try:
        threshold_val = coerce_float(threshold, name="threshold")
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper=helper)

    # threshold <= 0 used to satisfy abs(score) > threshold for every row.
    if threshold_val <= 0:
        return _error_result("INVALID_PARAM", "threshold must be a number greater than 0", helper=helper)

    try:
        include_all_val = coerce_bool(include_all, name="include_all", default=False)
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper=helper)

    if _require_statsmodels() is None:
        return _missing_package_error(helper, "statsmodels")

    coerced, series, season, err = _prepare_time_series_with_period(
        data,
        date_col=date_col,
        value_col=value_col,
        headers=headers,
        header_row=header_row,
        sheet_hint=sheet_hint,
        helper=helper,
        period=period,
    )
    if err is not None:
        return err
    assert coerced is not None and series is not None and season is not None

    try:
        from statsmodels.tsa.seasonal import STL

        stl_result = STL(series, period=season, robust=True).fit()
    except Exception as exc:
        return _error_result("FORECAST_FAILED", str(exc), helper=helper)

    expected = stl_result.trend + stl_result.seasonal
    resid = stl_result.resid
    scores = _robust_z_scores(resid.values)

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

    flags = _duplicate_date_flags(coerced) + _dropped_row_flags(coerced)

    anomalies_table = _table_from_df(anomalies_df, name="anomalies", max_rows=DATA_TABLE_ROWS)
    _append_truncation_flag(flags, anomalies_table, len(anomalies_df), "anomalies table")
    tables = [anomalies_table]

    if include_all_val:
        scores_table = _table_from_df(scores_df, name="all_scores", max_rows=DATA_TABLE_ROWS)
        _append_truncation_flag(flags, scores_table, len(scores_df), "all_scores table")
        tables.append(scores_table)

    metrics = {
        "n_anomalies": int(len(anomalies_df)),
        "period": season,
        "threshold": threshold_val,
        "method": method_name,
        "n_obs": int(len(series)),
    }
    return _ok_result(
        helper,
        metrics=metrics,
        tables=tables,
        flags=flags,
        metadata=coerced.metadata,
    )


def _dispatch_helper(name: str, data: Any, params: dict[str, Any], *, headers: bool, header_row: int, context: dict[str, Any]) -> dict[str, Any]:
    common = dispatch_common_kwargs(context, headers=headers, header_row=header_row)

    if name == "forecast_time_series":
        f_params: dict[str, Any] = {k: params[k] for k in ("periods", "model", "date_col", "value_col", "seasonal_periods") if k in params}
        return forecast_time_series(data, **f_params, **common)
    if name == "decompose_time_series":
        d_params: dict[str, Any] = {k: params[k] for k in ("date_col", "value_col", "model", "period") if k in params}
        return decompose_time_series(data, **d_params, **common)
    if name == "anomaly_detection_time_series":
        a_params: dict[str, Any] = {k: params[k] for k in ("date_col", "value_col", "period", "method", "threshold", "include_all") if k in params}
        return anomaly_detection_time_series(data, **a_params, **common)
    return _error_result("UNKNOWN_HELPER", f"Forecast helper {name!r} not found", helper=name)


def run_forecast(
    spec: dict[str, Any] | str,
    data: Any,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spec-driven dispatcher — single trusted entry for host RPC."""
    return run_trusted_spec(
        spec,
        data,
        context,
        helper_names=HELPER_NAMES,
        dispatch_fn=_dispatch_helper,
        domain="Forecast",
        error_code="FORECAST_FAILED",
    )
