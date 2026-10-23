# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted forecast helpers."""

from __future__ import annotations

import json
from importlib.util import find_spec
from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from plugin.scripting.forecast import anomaly_detection_time_series, decompose_time_series, forecast_time_series, run_forecast
from plugin.scripting.venv import forecast as venv_forecast


def _require_statsmodels_installed() -> None:
    if find_spec("statsmodels") is None:
        pytest.skip("statsmodels not installed")


def _seasonal_series(n: int = 36) -> pd.DataFrame:
    rows = []
    for i in range(n):
        month = (i % 12) + 1
        year = 2020 + i // 12
        seasonal = 10.0 * (1 if month <= 6 else -1)
        rows.append({"Date": f"{year}-{month:02d}-01", "Value": 100.0 + i + seasonal})
    return pd.DataFrame(rows)


def test_forecast_time_series_ok():
    data = _seasonal_series()
    result = forecast_time_series(data, periods=6, model="auto")
    assert result["status"] == "ok"
    assert result["helper"] == "forecast_time_series"
    assert "metrics" in result
    assert "tables" in result
    assert result["tables"][0]["name"] == "forecast"
    assert len(result["tables"][0]["rows"]) == 6


def test_decompose_time_series_ok():
    _require_statsmodels_installed()
    data = _seasonal_series()
    result = decompose_time_series(data, period=12)
    assert result["status"] == "ok"
    assert result["helper"] == "decompose_time_series"
    assert result["tables"][0]["name"] == "decomposition"
    assert "trend" in result["tables"][0]["columns"]
    assert result["tables"][0]["truncated"] is False
    assert not any("shows the first" in flag for flag in result["flags"])


def test_run_forecast_dispatch():
    data = _seasonal_series()
    result = run_forecast({"helper": "forecast_time_series", "params": {"periods": 3}}, data)
    assert result["status"] == "ok"
    assert result["helper"] == "forecast_time_series"


def test_forecast_insufficient_data():
    data = pd.DataFrame({"Date": ["2024-01-01"], "Value": [1.0]})
    result = forecast_time_series(data)
    assert result["status"] == "error"
    assert result["code"] == "INSUFFICIENT_DATA"


def test_forecast_unknown_column():
    data = pd.DataFrame({"X": [1, 2, 3, 4, 5, 6, 7, 8]})
    result = forecast_time_series(data, date_col="Date", value_col="Value")
    assert result["status"] == "error"
    assert result["code"] == "UNKNOWN_COLUMN"


def test_decompose_missing_statsmodels():
    data = _seasonal_series()
    with patch("plugin.scripting.venv.forecast._require_statsmodels", return_value=None):
        result = decompose_time_series(data, period=12)
    assert result["status"] == "error"
    assert result["code"] == "MISSING_PACKAGE"


def test_forecast_moving_average_fallback():
    data = _seasonal_series()
    with patch("plugin.scripting.venv.forecast._require_statsmodels", return_value=None):
        result = forecast_time_series(data, periods=4, model="auto")
    assert result["status"] == "ok"
    assert result["metrics"]["model"] == "moving_average"


def test_run_forecast_unknown_helper():
    result = run_forecast({"helper": "not_a_helper"}, _seasonal_series())
    assert result["status"] == "error"
    assert result["code"] == "UNKNOWN_HELPER"


def _seasonal_series_with_spike(n: int = 36, spike_idx: int = 18, spike_amount: float = 500.0) -> pd.DataFrame:
    df = _seasonal_series(n)
    df.loc[spike_idx, "Value"] = float(df.loc[spike_idx, "Value"]) + spike_amount
    return df


def test_anomaly_detection_time_series_flags_spike():
    _require_statsmodels_installed()
    data = _seasonal_series_with_spike()
    result = anomaly_detection_time_series(data, period=12, threshold=3.0)
    assert result["status"] == "ok"
    assert result["helper"] == "anomaly_detection_time_series"
    assert result["metrics"]["n_anomalies"] >= 1
    table = result["tables"][0]
    assert table["name"] == "anomalies"
    assert table["columns"] == ["date", "observed", "expected", "residual", "score"]
    assert len(table["rows"]) >= 1


def test_anomaly_detection_missing_statsmodels():
    data = _seasonal_series_with_spike()
    with patch("plugin.scripting.venv.forecast._require_statsmodels", return_value=None):
        result = anomaly_detection_time_series(data, period=12)
    assert result["status"] == "error"
    assert result["code"] == "MISSING_PACKAGE"


def test_anomaly_detection_insufficient_data():
    _require_statsmodels_installed()
    data = _seasonal_series(n=12)
    result = anomaly_detection_time_series(data, period=12)
    assert result["status"] == "error"
    assert result["code"] == "INSUFFICIENT_DATA"


def test_run_forecast_anomaly_dispatch():
    _require_statsmodels_installed()
    data = _seasonal_series_with_spike()
    result = run_forecast({"helper": "anomaly_detection_time_series", "params": {"period": 12}}, data)
    assert result["status"] == "ok"
    assert result["helper"] == "anomaly_detection_time_series"


@pytest.mark.parametrize("model", ["moving_average"])
def test_forecast_time_series_models(model: str):
    data = _seasonal_series()
    result = forecast_time_series(data, periods=3, model=model)
    assert result["status"] == "ok"
    assert result["metrics"]["model"] == model


def _indexed(frame: pd.DataFrame) -> pd.Series:
    work = frame.copy()
    work["Date"] = pd.to_datetime(work["Date"])
    return work.set_index("Date")["Value"]


def test_daily_series_infers_weekly_season_not_monthly():
    # 30 daily rows used to become period 12 because n >= 24.
    dates = pd.date_range("2024-01-01", periods=30, freq="D")
    frame = pd.DataFrame({"Date": dates, "Value": [100.0 + (i % 7) for i in range(30)]})
    assert venv_forecast._infer_seasonal_periods(_indexed(frame), None) == 7
    assert venv_forecast._infer_seasonal_periods(_indexed(frame), 12) == 12
    result = forecast_time_series(frame, periods=2, model="auto")
    assert result["status"] == "ok"
    assert result["metrics"].get("seasonal_periods", 7) == 7


def test_weekly_series_stays_trend_only():
    # 30 weeks is not two years, so a seasonal model must not run.
    dates = pd.date_range("2024-01-07", periods=30, freq="W")
    frame = pd.DataFrame({"Date": dates, "Value": [float(i) for i in range(30)]})
    assert venv_forecast._infer_seasonal_periods(_indexed(frame), None) is None
    result = forecast_time_series(frame, periods=3, model="auto")
    assert result["status"] == "ok"
    assert result["metrics"]["model"] != "holt_winters"
    assert result["metrics"].get("seasonal_periods") != 12
    assert any("not enough cycles" in flag for flag in result["flags"])


def test_monthly_series_infers_annual_season():
    frame = _seasonal_series(36)
    assert venv_forecast._infer_seasonal_periods(_indexed(frame), None) == 12


def test_weekday_forecast_skips_weekends():
    dates = pd.bdate_range("2024-01-02", periods=60)
    frame = pd.DataFrame({"Date": dates, "Value": [float(i) for i in range(60)]})
    assert venv_forecast._infer_seasonal_periods(_indexed(frame), None) == 5
    result = forecast_time_series(frame, periods=5, model="moving_average")
    assert result["status"] == "ok"
    assert result["metrics"]["freq"] == "B"
    assert not any("irregular" in flag.lower() for flag in result["flags"])
    for row in result["tables"][0]["rows"]:
        assert pd.Timestamp(row[0]).dayofweek < 5


def test_forecast_periods_are_capped():
    rows = [{"Date": f"2024-01-{day:02d}", "Value": float(day)} for day in range(1, 10)]
    result = forecast_time_series(pd.DataFrame(rows), periods=10**9, model="moving_average")
    assert result["status"] == "ok", result
    assert result["metrics"]["periods"] == 10_000
    assert len(result["tables"][0]["rows"]) == 10_000
    assert result["tables"][0]["truncated"] is False
    assert "periods capped at 10000" in result["flags"]


def test_holt_winters_intervals_use_simulate_not_get_prediction():
    _require_statsmodels_installed()
    result = forecast_time_series(
        _seasonal_series(),
        periods=4,
        model="holt_winters",
        seasonal_periods=12,
    )
    assert result["status"] == "ok", result
    table = result["tables"][0]
    assert table["columns"] == ["date", "forecast", "lower", "upper"]
    assert "confidence intervals unavailable" not in result["flags"]
    assert "interval_note" not in result["metrics"]
    assert not any("does not apply" in flag for flag in result["flags"])
    for row in table["rows"]:
        point, lower, upper = row[1], row[2], row[3]
        assert lower <= point <= upper


def test_holt_winters_interval_failure_is_a_structured_note(monkeypatch):
    _require_statsmodels_installed()
    monkeypatch.setattr(venv_forecast, "_holt_winters_intervals", lambda fit, periods: None)
    result = forecast_time_series(
        _seasonal_series(),
        periods=3,
        model="holt_winters",
        seasonal_periods=12,
    )
    assert result["status"] == "ok", result
    assert "lower" not in result["tables"][0]["columns"]
    note = result["metrics"]["interval_note"]
    assert note["available"] is False
    assert note["method"] == "HoltWintersResults.simulate"
    assert "get_prediction" in note["message"]
    assert note["message"] not in result["flags"]
    assert "Holt-Winters prediction intervals are omitted; the forecast column is the point forecast." in result["flags"]
    assert "confidence intervals unavailable" not in result["flags"]


def test_duplicate_dates_are_aggregated():
    rows = [{"Date": f"2024-01-{day:02d}", "Value": float(day)} for day in range(1, 10)]
    rows.append({"Date": "2024-01-01", "Value": 100.0})
    result = forecast_time_series(pd.DataFrame(rows), periods=2, model="moving_average")
    assert result["status"] == "ok"
    assert any("Aggregated 1 duplicate" in flag for flag in result["flags"])


def test_insert_forecast_result_into_calc() -> None:
    from unittest.mock import MagicMock, patch
    from plugin.scripting.helper_domain import insert_helper_result_into_calc

    doc = MagicMock()
    ctx = MagicMock()
    result = {"status": "ok", "tables": [{"columns": ["Date", "Forecast"], "rows": [["2024-01-01", 10.0]]}]}

    with patch("plugin.calc.tabular_egress.insert_tabular_result_into_calc", return_value=1) as mock_insert:
        res = insert_helper_result_into_calc(doc, ctx, result)
        assert res == 1
        mock_insert.assert_called_once()


def test_calc_serial_dates_are_not_1970():
    _require_statsmodels_installed()
    serials = [45292.0 + i for i in range(60)]
    values = [10.0 + float(i % 7) * 2.0 + float(i) * 0.1 for i in range(60)]
    frame = pd.DataFrame({"Date": serials, "Value": values})
    result = forecast_time_series(frame, periods=7, model="auto")
    assert result["status"] == "ok", result
    first_forecast_date = str(result["tables"][0]["rows"][0][0])
    assert first_forecast_date.startswith("2024-03-01")
    assert result["metrics"]["seasonal_periods"] == 7
    freq = str(result["metrics"].get("freq", ""))
    assert "ns" not in freq.lower()
    flags = result.get("flags", [])
    assert not any("season" in f.lower() and "could not be inferred" in f.lower() for f in flags)


def test_year_integers_forecast_the_next_year():
    frame = pd.DataFrame({"Year": list(range(2000, 2024)), "Value": [float(i) for i in range(24)]})
    result = forecast_time_series(frame, date_col="Year", periods=3)
    assert result["status"] == "ok", result
    rows = result["tables"][0]["rows"]
    assert str(rows[0][0]).startswith("2024-01-01")
    assert not any("1970" in str(r[0]) for r in rows)


def test_gapped_month_end_is_not_a_flat_average():
    _require_statsmodels_installed()
    dates = pd.date_range("2018-01-31", periods=36, freq="ME").delete(15)
    values = [float(np.sin(2 * np.pi * i / 12) * 10 + i * 0.5 + 50) for i in range(len(dates))]
    frame = pd.DataFrame({"Date": dates, "Value": values})
    result = forecast_time_series(frame, periods=6, model="auto")
    assert result["status"] == "ok", result
    assert result["metrics"]["model"] != "moving_average"
    assert pd.Timestamp(result["tables"][0]["rows"][0][0]) > dates[-1]
    for row in result["tables"][0]["rows"]:
        assert pd.Timestamp(row[0]).day >= 28
    assert "Dates are irregular, so the forecast assumes equal spacing between observations." in result["flags"]


def test_gapped_daily_is_not_a_flat_average():
    _require_statsmodels_installed()
    dates = pd.date_range("2024-01-01", periods=60, freq="D").delete(25)
    values = [float(10.0 + (i % 7) * 3.0 + i * 0.1) for i in range(len(dates))]
    frame = pd.DataFrame({"Date": dates, "Value": values})
    result = forecast_time_series(frame, periods=7, model="auto")
    assert result["status"] == "ok", result
    assert result["metrics"]["model"] != "moving_average"
    assert "Dates are irregular, so the forecast assumes equal spacing between observations." in result["flags"]


def test_periods_over_50_returns_every_row():
    frame = pd.DataFrame({"Date": pd.date_range("2024-01-01", periods=20, freq="D"), "Value": [float(i) for i in range(20)]})
    result = forecast_time_series(frame, periods=100, model="moving_average")
    assert result["status"] == "ok", result
    table = result["tables"][0]
    assert len(table["rows"]) == 100
    assert table["truncated"] is False
    assert table["total_rows"] == 100


def test_explicit_season_too_short_is_flagged():
    frame = pd.DataFrame({"Date": pd.date_range("2024-01-01", periods=20, freq="D"), "Value": [float(i) for i in range(20)]})
    result = forecast_time_series(frame, periods=3, model="auto", seasonal_periods=12)
    assert result["status"] == "ok", result
    assert result["metrics"]["model"] != "holt_winters"
    assert any("seasonal_periods=12" in f and "got 20" in f for f in result["flags"])
    assert not any("does not apply" in flag for flag in result["flags"])


def test_holt_winters_too_short_is_insufficient_data():
    _require_statsmodels_installed()
    frame = pd.DataFrame({"Date": pd.date_range("2024-01-01", periods=20, freq="D"), "Value": [float(i) for i in range(20)]})
    result = forecast_time_series(frame, periods=3, model="holt_winters", seasonal_periods=12)
    assert result["status"] == "error"
    assert result["code"] == "INSUFFICIENT_DATA"
    assert "heuristic method" not in result.get("error", "")


def test_bad_seasonal_period_is_invalid_param():
    frame = pd.DataFrame({"Date": pd.date_range("2024-01-01", periods=20, freq="D"), "Value": [float(i) for i in range(20)]})
    res1 = forecast_time_series(frame, seasonal_periods="weekly")
    assert res1["status"] == "error"
    assert res1["code"] == "INVALID_PARAM"
    res2 = run_forecast({"helper": "forecast_time_series", "params": {"seasonal_periods": "weekly"}}, data=frame)
    assert res2["status"] == "error"
    assert res2["code"] == "INVALID_PARAM"
    dec_ok = decompose_time_series(frame, period="7")
    assert dec_ok["status"] == "ok"
    dec_bad = decompose_time_series(frame, period="weekly")
    assert dec_bad["status"] == "error"
    assert dec_bad["code"] == "INVALID_PARAM"


def test_unknown_model_is_invalid_param():
    frame = pd.DataFrame({"Date": pd.date_range("2024-01-01", periods=20, freq="D"), "Value": [float(i) for i in range(20)]})
    result = forecast_time_series(frame, model="prophet")
    assert result["status"] == "error"
    assert result["code"] == "INVALID_PARAM"


def test_decompose_nan_cells_are_none():
    _require_statsmodels_installed()
    frame = pd.DataFrame({"Date": pd.date_range("2024-01-01", periods=28, freq="D"), "Value": [float(i) for i in range(28)]})
    res = decompose_time_series(frame, period=7)
    assert res["status"] == "ok", res
    table = res["tables"][0]
    cols = table["columns"]
    rows = table["rows"]
    trend_idx = cols.index("trend")
    resid_idx = cols.index("resid")
    assert rows[0][trend_idx] is None
    assert rows[0][resid_idx] is None
    dumped = json.dumps(rows, allow_nan=False, default=str)
    assert dumped is not None


def test_unparseable_dates_are_counted():
    rows = [{"Date": f"2024-01-{day:02d}", "Value": float(day)} for day in range(1, 11)]
    rows.append({"Date": "not-a-date", "Value": 99.0})
    frame = pd.DataFrame(rows)
    res = forecast_time_series(frame, periods=3, model="moving_average")
    assert res["status"] == "ok", res
    assert "Dropped 1 rows with an unparseable date or value" in res["flags"]


def test_moving_average_states_no_interval():
    frame = pd.DataFrame({"Date": pd.date_range("2024-01-01", periods=10, freq="D"), "Value": [float(i) for i in range(10)]})
    res = forecast_time_series(frame, periods=3, model="moving_average")
    assert res["status"] == "ok", res
    assert "moving_average has no prediction interval" in res["flags"]
    assert not any("does not apply" in flag for flag in res["flags"])


def test_anomaly_tables_use_200_rows():
    _require_statsmodels_installed()
    frame = pd.DataFrame({"Date": pd.date_range("2024-01-01", periods=60, freq="D"), "Value": [float(i % 7) for i in range(60)]})
    res = anomaly_detection_time_series(frame, period=7, include_all=True)
    assert res["status"] == "ok", res
    all_scores = next(t for t in res["tables"] if t["name"] == "all_scores")
    assert all_scores["truncated"] is False
    assert len(all_scores["rows"]) == 60


def test_periods_below_one_is_invalid_param():
    frame = pd.DataFrame({
        "Date": pd.date_range("2024-01-01", periods=10, freq="D"),
        "Value": [float(i) for i in range(10)],
    })
    for p in (0, -3, "0"):
        res = forecast_time_series(frame, periods=p, model="moving_average")
        assert res["status"] == "error"
        assert res["code"] == "INVALID_PARAM"
        assert res["message"] == "periods must be an integer greater than 0"

    res_run = run_forecast({"helper": "forecast_time_series", "params": {"periods": 0}}, data=frame)
    assert res_run["status"] == "error"
    assert res_run["code"] == "INVALID_PARAM"
    assert res_run["message"] == "periods must be an integer greater than 0"

    ok_res = forecast_time_series(frame, periods=1, model="moving_average")
    assert ok_res["status"] == "ok"
    assert ok_res["metrics"]["periods"] == 1


def test_period_below_two_is_invalid_param():
    _require_statsmodels_installed()
    frame = pd.DataFrame({
        "Date": pd.date_range("2024-01-01", periods=20, freq="D"),
        "Value": [float(i) for i in range(20)],
    })
    for p in (1, 0, -5):
        dec = decompose_time_series(frame, period=p)
        assert dec["status"] == "error"
        assert dec["code"] == "INVALID_PARAM"
        assert dec["message"] == "period must be an integer greater than 1"

        anom = anomaly_detection_time_series(frame, period=p)
        assert anom["status"] == "error"
        assert anom["code"] == "INVALID_PARAM"
        assert anom["message"] == "period must be an integer greater than 1"

    weekly_frame = pd.DataFrame({
        "Date": pd.date_range("2024-01-07", periods=30, freq="W"),
        "Value": [float(i) for i in range(30)],
    })
    dec_omitted = decompose_time_series(weekly_frame)
    assert dec_omitted["status"] == "error"
    assert dec_omitted["code"] == "INSUFFICIENT_DATA"


def test_same_date_and_value_column_is_invalid_param():
    frame = pd.DataFrame({"Date": pd.date_range("2024-01-01", periods=12, freq="D")})
    res = forecast_time_series(frame, date_col="Date", value_col="Date", periods=2, model="moving_average")
    assert res["status"] == "error"
    assert res["code"] == "INVALID_PARAM"
    assert res["message"] == "date_col and value_col must be different columns, got 'Date'"

    run_res = run_forecast(
        {"helper": "forecast_time_series", "params": {"date_col": "Date", "value_col": "Date", "periods": 2}},
        data=frame,
    )
    assert run_res["status"] == "error"
    assert run_res["code"] == "INVALID_PARAM"
    assert run_res["message"] == "date_col and value_col must be different columns, got 'Date'"

    dec_res = decompose_time_series(frame, date_col="Date", value_col="Date", period=7)
    assert dec_res["status"] == "error"
    assert dec_res["code"] == "INVALID_PARAM"
    assert dec_res["message"] == "date_col and value_col must be different columns, got 'Date'"

    frame_no_date = pd.DataFrame({"Other": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]})
    res_no_date = forecast_time_series(frame_no_date, date_col="Date", value_col="Date")
    assert res_no_date["status"] == "error"
    assert res_no_date["code"] == "UNKNOWN_COLUMN"


def test_prepare_does_not_mutate_caller_metadata():
    from plugin.scripting.venv.coerce import resolve_df

    rows: list[dict[str, Any]] = [
        {"Date": f"2024-01-{day:02d}", "Value": float(day)} for day in range(1, 11)
    ]
    rows.append({"Date": "not-a-date", "Value": 1.0})
    rows.append({"Date": "2024-01-01", "Value": 99.0})
    frame = pd.DataFrame(rows)

    coerced = resolve_df(frame)
    snapshot = dict(coerced.metadata)

    res = forecast_time_series(coerced, periods=2, model="moving_average")
    assert res["status"] == "ok"
    assert coerced.metadata == snapshot
    assert "rows_dropped" not in coerced.metadata
    assert "duplicate_dates_collapsed" not in coerced.metadata
    assert res["metadata"] is not coerced.metadata
    assert res["metadata"]["rows_dropped"] == 1
    assert res["metadata"]["duplicate_dates_collapsed"] == 1
    assert "Dropped 1 rows with an unparseable date or value" in res["flags"]
    assert "Aggregated 1 duplicate date rows by mean" in res["flags"]


def test_known_nonseasonal_frequency_is_named():
    _require_statsmodels_installed()

    # Yearly
    yearly_frame = pd.DataFrame({
        "Date": pd.date_range("2000-01-01", periods=12, freq="YS"),
        "Value": [float(i) for i in range(12)],
    })
    res_yearly = forecast_time_series(yearly_frame, periods=2, model="auto")
    assert res_yearly["status"] == "ok"
    expected_yearly = f"frequency {pd.infer_freq(yearly_frame['Date'])} has no seasonal period to infer; used trend-only"
    assert any(expected_yearly in flag for flag in res_yearly["flags"])
    assert not any("could not infer a seasonal period from the dates" in flag for flag in res_yearly["flags"])

    # Minutes
    min_frame = pd.DataFrame({
        "Date": pd.date_range("2024-01-01", periods=30, freq="min"),
        "Value": [float(i) for i in range(30)],
    })
    res_min = forecast_time_series(min_frame, periods=2, model="auto")
    assert res_min["status"] == "ok"
    expected_min = f"frequency {pd.infer_freq(min_frame['Date'])} has no seasonal period to infer; used trend-only"
    assert any(expected_min in flag for flag in res_min["flags"])
    assert not any("could not infer a seasonal period from the dates" in flag for flag in res_min["flags"])

    # No frequency
    no_freq_dates = [
        "2024-01-01",
        "2024-01-04",
        "2024-01-09",
        "2024-01-16",
        "2024-01-25",
        "2024-02-06",
        "2024-02-20",
        "2024-03-10",
    ]
    no_freq_frame = pd.DataFrame({
        "Date": pd.to_datetime(no_freq_dates),
        "Value": [float(i) for i in range(8)],
    })
    res_no_freq = forecast_time_series(no_freq_frame, periods=2, model="auto")
    assert res_no_freq["status"] == "ok"
    exact_sentence = "could not infer a seasonal period from the dates; used trend-only"
    assert exact_sentence in res_no_freq["flags"]
    assert not any("has no seasonal period to infer" in flag for flag in res_no_freq["flags"])


def test_cycle_threshold_follows_min_decompose_cycles(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(venv_forecast, "_MIN_DECOMPOSE_CYCLES", 3)
    frame = pd.DataFrame({
        "Date": pd.date_range("2024-01-01", periods=20, freq="D"),
        "Value": [float(i) for i in range(20)],
    })
    res_auto = forecast_time_series(frame, periods=2, model="auto", seasonal_periods=7)
    assert res_auto["status"] == "ok"
    assert any("needs at least 21" in flag and "got 20" in flag for flag in res_auto["flags"])

    res_hw = forecast_time_series(frame, periods=2, model="holt_winters", seasonal_periods=7)
    assert res_hw["status"] == "error"
    assert res_hw["code"] == "INSUFFICIENT_DATA"
    assert "21" in res_hw["message"]


def test_month_snapshot_on_the_28th_forecasts_the_next_month():
    dates = [pd.Timestamp(f"2023-{m:02d}-28") for m in range(5, 13)]
    frame = pd.DataFrame({"Date": dates, "Value": [float(i) for i in range(len(dates))]})
    result = forecast_time_series(frame, periods=3, model="moving_average")
    assert result["status"] == "ok"
    assert result["metrics"]["freq"] == "month"
    first_iso = str(result["tables"][0]["rows"][0][0])
    assert first_iso.startswith("2024-01-28")
    assert not first_iso.startswith("2023-12-31")


def test_28th_series_ending_on_non_leap_february_is_not_month_end():
    dates = [pd.Timestamp("2021-03-28") + pd.DateOffset(months=i) for i in range(24)]
    assert dates[-1] == pd.Timestamp("2023-02-28")
    frame = pd.DataFrame({"Date": dates, "Value": [float(i) for i in range(len(dates))]})
    result = forecast_time_series(frame, periods=3, model="moving_average")
    assert result["status"] == "ok"
    assert result["metrics"]["freq"] == "month"
    first_iso = str(result["tables"][0]["rows"][0][0])
    assert first_iso.startswith("2023-03-28")
    assert not first_iso.startswith("2023-03-31")


def test_gapped_true_month_end_stays_on_month_end():
    dates = pd.date_range("2021-03-31", periods=24, freq="ME").delete(5)
    assert dates[-1] == pd.Timestamp("2023-02-28")
    assert pd.infer_freq(dates) is None
    frame = pd.DataFrame({"Date": dates, "Value": [float(i) for i in range(len(dates))]})
    result = forecast_time_series(frame, periods=3, model="moving_average")
    assert result["status"] == "ok"
    assert result["metrics"]["freq"] == "ME"
    first_iso = str(result["tables"][0]["rows"][0][0])
    assert first_iso.startswith("2023-03-31")


def test_day_30_snapshot_keeps_the_day_after_february():
    dates = []
    for i in range(24):
        year = 2022 + i // 12
        month = (i % 12) + 1
        if month == 2:
            dates.append(pd.Timestamp(f"{year}-02-28"))
        else:
            dates.append(pd.Timestamp(f"{year}-{month:02d}-30"))
    assert dates[-1] == pd.Timestamp("2023-12-30")
    frame = pd.DataFrame({"Date": dates, "Value": [float(i) for i in range(len(dates))]})
    result = forecast_time_series(frame, periods=3, model="moving_average")
    assert result["status"] == "ok"
    assert result["metrics"]["freq"] == "month"
    rows = result["tables"][0]["rows"]
    assert str(rows[0][0]).startswith("2024-01-30")
    assert str(rows[1][0]).startswith("2024-02-29")
    assert str(rows[2][0]).startswith("2024-03-30")


def test_quarter_snapshot_on_the_28th_steps_a_quarter():
    dates = []
    for y in (2022, 2023):
        for m in (3, 6, 9, 12):
            dates.append(pd.Timestamp(f"{y}-{m:02d}-28"))
    frame = pd.DataFrame({"Date": dates, "Value": [float(i) for i in range(len(dates))]})
    result = forecast_time_series(frame, periods=3, model="moving_average")
    assert result["status"] == "ok"
    assert result["metrics"]["freq"] == "quarter"
    first_iso = str(result["tables"][0]["rows"][0][0])
    assert first_iso.startswith("2024-03-28")
    assert not first_iso.startswith("2023-12-31")


def test_year_snapshot_on_dec_28_steps_a_year():
    dates = [pd.Timestamp(f"{y}-12-28") for y in range(2016, 2024)]
    frame = pd.DataFrame({"Date": dates, "Value": [float(i) for i in range(len(dates))]})
    result = forecast_time_series(frame, periods=3, model="moving_average")
    assert result["status"] == "ok"
    assert result["metrics"]["freq"] == "year"
    first_iso = str(result["tables"][0]["rows"][0][0])
    assert first_iso.startswith("2024-12-28")
    assert not first_iso.startswith("2023-12-31")


def test_gapped_month_start_stays_on_month_start():
    dates = pd.date_range("2018-01-01", periods=24, freq="MS").delete(10)
    frame = pd.DataFrame({"Date": dates, "Value": [float(i) for i in range(len(dates))]})
    result = forecast_time_series(frame, periods=3, model="moving_average")
    assert result["status"] == "ok"
    assert result["metrics"]["freq"] == "MS"
    first_iso = str(result["tables"][0]["rows"][0][0])
    assert first_iso.startswith("2020-01-01")


def test_gapped_quarter_end_stays_on_quarter_end():
    dates = pd.date_range("2016-03-31", periods=16, freq="QE").delete(3)
    frame = pd.DataFrame({"Date": dates, "Value": [float(i) for i in range(len(dates))]})
    result = forecast_time_series(frame, periods=3, model="moving_average")
    assert result["status"] == "ok"
    assert result["metrics"]["freq"] == "QE"
    first_iso = str(result["tables"][0]["rows"][0][0])
    assert first_iso.startswith("2020-03-31")


def test_nonpositive_anomaly_threshold_is_invalid_param():
    frame = pd.DataFrame({
        "Date": pd.date_range("2024-01-01", periods=20, freq="D"),
        "Value": [float(i) for i in range(20)],
    })
    for val in (0, -1, 0.0, "0", "-1.5"):
        res = anomaly_detection_time_series(frame, period=7, threshold=val)
        assert res["status"] == "error"
        assert res["code"] == "INVALID_PARAM"
        assert res["message"] == "threshold must be a number greater than 0"
    res_run = run_forecast(
        {"helper": "anomaly_detection_time_series", "params": {"period": 7, "threshold": 0}},
        data=frame,
    )
    assert res_run["status"] == "error"
    assert res_run["code"] == "INVALID_PARAM"
    assert res_run["message"] == "threshold must be a number greater than 0"

    _require_statsmodels_installed()
    spike_data = _seasonal_series_with_spike()
    ok_res = anomaly_detection_time_series(spike_data, period=7, threshold=0.5)
    assert ok_res["status"] == "ok"
    assert ok_res["metrics"]["threshold"] == 0.5


def test_decompose_truncation_is_flagged():
    _require_statsmodels_installed()
    dates = pd.date_range("2024-01-01", periods=201, freq="D")
    frame = pd.DataFrame({"Date": dates, "Value": [10.0 + (i % 7) for i in range(201)]})
    result = decompose_time_series(frame, period=7)
    assert result["status"] == "ok", result
    table = result["tables"][0]
    assert table["truncated"] is True
    assert len(table["rows"]) == 200
    assert table["total_rows"] == 201
    assert "decomposition table shows the first 200 of 201 rows" in result["flags"]


def test_moving_average_ignores_seasonal_periods():
    frame = pd.DataFrame({
        "Date": pd.date_range("2024-01-01", periods=20, freq="D"),
        "Value": [float(i) for i in range(20)],
    })
    result = forecast_time_series(frame, periods=3, model="moving_average", seasonal_periods=4)
    assert result["status"] == "ok", result
    flags = result["flags"]
    ign_flag = "seasonal_periods does not apply to moving_average; ignored"
    no_int_flag = "moving_average has no prediction interval"
    assert ign_flag in flags
    assert no_int_flag in flags
    assert flags.index(ign_flag) < flags.index(no_int_flag)


def test_arima_ignores_seasonal_periods():
    _require_statsmodels_installed()
    frame = pd.DataFrame({
        "Date": pd.date_range("2024-01-01", periods=20, freq="D"),
        "Value": [float(i) for i in range(20)],
    })
    result = forecast_time_series(frame, periods=3, model="arima", seasonal_periods=4)
    assert result["status"] == "ok", result
    assert result["metrics"]["model"] == "arima"
    assert "seasonal_periods does not apply to non-seasonal ARIMA(1,1,1); ignored" in result["flags"]


def test_default_params_match_venv_signatures():
    import inspect
    from plugin.scripting.forecast import _DEFAULT_PARAMS

    for helper_name, defaults in _DEFAULT_PARAMS.items():
        fn = getattr(venv_forecast, helper_name)
        sig = inspect.signature(fn)
        for param_name, expected_default in defaults.items():
            assert param_name in sig.parameters, f"{param_name} not in {helper_name} signature"
            assert sig.parameters[param_name].default == expected_default, (
                f"{helper_name}.{param_name} default {sig.parameters[param_name].default!r} != {expected_default!r}"
            )




