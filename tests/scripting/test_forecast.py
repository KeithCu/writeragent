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
