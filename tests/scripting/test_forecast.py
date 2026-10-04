# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted forecast helpers."""

from __future__ import annotations

from importlib.util import find_spec
from unittest.mock import patch

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
    for row in result["tables"][0]["rows"]:
        assert pd.Timestamp(row[0]).dayofweek < 5


def test_forecast_periods_are_capped():
    rows = [{"Date": f"2024-01-{day:02d}", "Value": float(day)} for day in range(1, 10)]
    result = forecast_time_series(pd.DataFrame(rows), periods=10**9, model="moving_average")
    assert result["status"] == "ok", result
    assert result["metrics"]["periods"] == 10_000
    # The sheet table is capped separately. The horizon itself must not be 1e9.
    assert len(result["tables"][0]["rows"]) < 10_000


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
    assert "confidence intervals unavailable" not in result["flags"]
    assert note["message"] in result["flags"]


def test_duplicate_dates_are_aggregated():
    rows = [{"Date": f"2024-01-{day:02d}", "Value": float(day)} for day in range(1, 10)]
    rows.append({"Date": "2024-01-01", "Value": 100.0})
    result = forecast_time_series(pd.DataFrame(rows), periods=2, model="moving_average")
    assert result["status"] == "ok"
    assert any("Aggregated 1 duplicate" in flag for flag in result["flags"])


def test_insert_forecast_result_into_calc() -> None:
    from unittest.mock import MagicMock, patch
    from plugin.scripting.forecast import insert_forecast_result_into_calc

    doc = MagicMock()
    ctx = MagicMock()
    result = {"status": "ok", "tables": [{"columns": ["Date", "Forecast"], "rows": [["2024-01-01", 10.0]]}]}

    with patch("plugin.calc.tabular_egress.insert_tabular_result_into_calc", return_value=1) as mock_insert:
        res = insert_forecast_result_into_calc(doc, ctx, result)
        assert res == 1
        mock_insert.assert_called_once()

