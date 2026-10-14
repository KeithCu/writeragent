# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted quant helpers."""

from __future__ import annotations

import builtins
import math
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.client import run_quant
from plugin.scripting.quant import (
    HELPER_NAMES,
    QUANT_HEADER_PREFIX,
    get_quant_template,
)
from plugin.scripting.venv.quant import run_quant as venv_run_quant
from plugin.framework.errors import ToolExecutionError


def test_quant_template_is_executable():
    code = get_quant_template("fetch_historical_data")
    assert code is not None
    assert "from writeragent.scripting.quant import run_quant" in code
    assert "fetch_historical_data" in code
    assert QUANT_HEADER_PREFIX not in code.splitlines()[0]
    assert "'tickers': 'AAPL'" in code
    assert "'tickers': 'data'" not in code


def test_run_quant_missing_package(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yfinance":
            raise ImportError("no yfinance")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = venv_run_quant({"helper": "fetch_historical_data", "params": {"tickers": ["AAPL"]}})
    assert result["status"] == "error"
    assert result["code"] == "MISSING_PACKAGE"


def test_run_quant_invalid_params(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "yfinance", object())
    result = venv_run_quant({"helper": "fetch_historical_data", "params": {}})
    assert result["status"] == "error"
    assert result["code"] == "INVALID_PARAMS"
    assert result["helper"] == "fetch_historical_data"
    from plugin.calc.quant_egress import is_quant_result

    assert is_quant_result(result) is True


def test_fetch_historical_data_calc_range(monkeypatch):
    import sys
    from plugin.scripting.calc_range import CalcRange
    import pandas as pd

    fake_yf = MagicMock()
    fake_df = pd.DataFrame({"Date": ["2023-01-01"], "Close": [150.0]})
    fake_yf.download.return_value = fake_df
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    cr = CalcRange([["AAPL"], ["MSFT"], [None]])
    result = venv_run_quant({"helper": "fetch_historical_data", "params": {"tickers": cr}})
    assert result["status"] == "ok"
    fake_yf.download.assert_called_once()
    assert fake_yf.download.call_args[0][0] == ["AAPL", "MSFT"]


@pytest.fixture
def ctx():
    return MagicMock()


def test_client_run_quant_happy_path(ctx):
    worker_result = {"status": "ok", "helper": "fetch_historical_data", "table": {"columns": ["Date"], "rows": []}}
    with (
        patch("plugin.scripting.client.configured_python_exec_timeout", return_value=30),
        patch("plugin.scripting.client.run_trusted_worker_action", return_value=worker_result) as mock_run,
    ):
        result = run_quant(ctx, {"helper": "fetch_historical_data", "params": {"tickers": ["AAPL"]}})

    assert result["helper"] == "fetch_historical_data"
    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    # writeragent:* ids are not sandbox sessions; the client no longer forwards one.
    assert "session_id" not in kwargs
    assert kwargs["domain"] == "quant"
    assert kwargs["helper"] == "fetch_historical_data"


def test_client_run_quant_worker_error(ctx):
    with (
        patch("plugin.scripting.client.configured_python_exec_timeout", return_value=10),
        patch(
            "plugin.scripting.client.run_trusted_worker_action",
            side_effect=ToolExecutionError("boom", code="QUANT_ERROR"),
        ),
    ):
        with pytest.raises(ToolExecutionError, match="boom"):
            run_quant(ctx, {"helper": "fetch_historical_data", "params": {"tickers": ["AAPL"]}})


def test_helper_names_cover_templates():
    for helper in HELPER_NAMES:
        assert get_quant_template(helper) is not None


def test_portfolio_tearsheet_portfolio_returns_grid():
    pytest.importorskip("quantstats")
    grid = [
        ["AAPL", "MSFT", "GOOG"],
        [0.01, 0.008, 0.012],
        [0.02, -0.01, 0.025],
        [-0.005, 0.015, 0.01],
        [0.012, 0.005, -0.008],
        [0.008, 0.02, 0.015],
        [0.015, -0.005, 0.02],
        [-0.01, 0.012, 0.005],
        [0.02, 0.008, 0.018],
        [0.005, 0.015, -0.002],
        [0.01, 0.01, 0.01],
    ]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {}}, data=grid)
    assert result["status"] == "ok"
    assert result["helper"] == "portfolio_tearsheet"
    assert "metrics" in result
    assert isinstance(result["metrics"], dict)
    assert "Cumulative Return" in result["metrics"] or len(result["metrics"]) > 0


def test_portfolio_tearsheet_with_date_column_and_single_column_param():
    pytest.importorskip("quantstats")
    grid = [
        ["Date", "AAPL", "MSFT"],
        ["2024-01-01", 0.01, 0.02],
        ["2024-01-02", 0.02, -0.01],
        ["2024-01-03", -0.005, 0.015],
    ]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {"column": "MSFT"}}, data=grid)
    assert result["status"] == "ok"
    assert "metrics" in result


def test_portfolio_tearsheet_invalid_data():
    pytest.importorskip("quantstats")
    grid = [
        ["HeaderA", "HeaderB"],
        ["text1", "text2"],
        ["text3", "text4"],
    ]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {}}, data=grid)
    assert result["status"] == "error"
    assert result["code"] == "INVALID_DATA"
    assert result["helper"] == "portfolio_tearsheet"


def test_technical_analysis_missing_column_sets_helper(monkeypatch):
    pytest.importorskip("pandas")
    import importlib

    real_import = importlib.import_module

    def _fake_import(name, package=None):
        if name == "pandas_ta":
            return object()
        return real_import(name, package)

    monkeypatch.setattr("plugin.scripting.venv.quant.importlib.import_module", _fake_import)
    result = venv_run_quant(
        {"helper": "technical_analysis", "params": {}},
        data=[["Open"], [1.0]],
    )
    assert result["status"] == "error"
    assert result["code"] == "MISSING_COLUMN"
    assert result["helper"] == "technical_analysis"
    from plugin.calc.quant_egress import is_quant_result

    assert is_quant_result(result) is True


def test_efficient_frontier_treats_grid_as_returns():
    pytest.importorskip("pypfopt")
    grid = [
        ["AAPL", "MSFT", "GOOG"],
        [0.01, 0.008, 0.012],
        [0.02, -0.01, 0.025],
        [-0.005, 0.015, 0.01],
        [0.012, 0.005, -0.008],
        [0.008, 0.02, 0.015],
        [0.015, -0.005, 0.02],
        [-0.01, 0.012, 0.005],
        [0.02, 0.008, 0.018],
        [0.005, 0.015, -0.002],
        [0.01, 0.01, 0.01],
    ]
    result = venv_run_quant({"helper": "efficient_frontier", "params": {}}, data=grid)
    assert result["status"] == "ok", result
    weights = result["weights"]
    assert abs(sum(weights.values()) - 1.0) < 1e-3
    # Reading the same returns as prices collapses onto MSFT with weight 1.
    assert all(0.05 < float(weight) < 0.8 for weight in weights.values())


def test_is_quant_result_uses_helper_names_not_fetch_prefix():
    from plugin.calc.quant_egress import is_quant_result

    assert is_quant_result({"status": "ok", "helper": "fetch_historical_data"}) is True
    assert is_quant_result({"status": "ok", "helper": "fetch_not_a_helper"}) is False
    assert is_quant_result({"status": "error", "code": "QUANT_ERROR"}) is True
    assert is_quant_result({"status": "error", "code": "OTHER"}) is False


def test_fetch_historical_data_empty_returns_no_data(monkeypatch):
    import sys
    import pandas as pd

    fake_yf = MagicMock()
    fake_yf.download.return_value = pd.DataFrame()
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    result = venv_run_quant({"helper": "fetch_historical_data", "params": {"tickers": ["INVALID"]}})
    assert result["status"] == "error"
    assert result["code"] == "NO_DATA"
    fake_yf.download.assert_called_once()
    assert fake_yf.download.call_args[1]["progress"] is False
    assert fake_yf.download.call_args[1]["multi_level_index"] is False


def test_portfolio_tearsheet_bad_column(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "quantstats", MagicMock())
    grid = [["Date", "AAPL", "MSFT"], ["2024-01-01", 0.01, 0.02]]
    result = venv_run_quant(
        {"helper": "portfolio_tearsheet", "params": {"column": "NonExistent"}},
        data=grid,
    )
    assert result["status"] == "error"
    assert result["code"] == "INVALID_PARAMS"
    assert "NonExistent" in result["message"]


def test_portfolio_tearsheet_synthetic_dates_warning(monkeypatch):
    import sys
    import pandas as pd

    mock_qs = MagicMock()
    mock_qs.reports.metrics.return_value = pd.DataFrame({"val": [0.05]}, index=["Cumulative Return"])
    monkeypatch.setitem(sys.modules, "quantstats", mock_qs)

    grid = [["AAPL", "MSFT"], [0.01, 0.02], [0.02, -0.01]]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {}}, data=grid)
    assert result["status"] == "ok"
    assert "warning" in result
    assert "synthetic" in result["warning"].lower()


def test_portfolio_tearsheet_nat_dates_dropped(monkeypatch):
    import sys
    import pandas as pd

    mock_qs = MagicMock()
    mock_qs.reports.metrics.return_value = pd.DataFrame({"val": [0.05]}, index=["Cumulative Return"])
    monkeypatch.setitem(sys.modules, "quantstats", mock_qs)

    grid = [
        ["Date", "AAPL"],
        ["2024-01-01", 0.01],
        ["not-a-date", 0.02],
        ["2024-01-02", 0.03],
    ]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {}}, data=grid)
    assert result["status"] == "ok"
    passed_series = mock_qs.reports.metrics.call_args[0][0]
    assert len(passed_series) == 2


def test_portfolio_tearsheet_parses_calc_serial_dates(monkeypatch):
    import sys
    import pandas as pd

    mock_qs = MagicMock()
    mock_qs.reports.metrics.return_value = pd.DataFrame({"val": [0.05]}, index=["Cumulative Return"])
    monkeypatch.setitem(sys.modules, "quantstats", mock_qs)

    grid = [
        ["Date", "AAPL"],
        [45292.0, 0.01],
        [45294.0, -0.01],
        [45293.0, 0.02],
    ]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {}}, data=grid)
    assert result["status"] == "ok"
    assert "warning" not in result
    passed_series = mock_qs.reports.metrics.call_args[0][0]
    assert len(passed_series) == 3
    assert list(passed_series.values) == [0.01, 0.02, -0.01]
    assert [ts.strftime("%Y-%m-%d") for ts in passed_series.index] == ["2024-01-01", "2024-01-02", "2024-01-03"]


def test_portfolio_tearsheet_parses_year_column(monkeypatch):
    import sys
    import pandas as pd

    mock_qs = MagicMock()
    mock_qs.reports.metrics.return_value = pd.DataFrame({"val": [0.05]}, index=["Cumulative Return"])
    monkeypatch.setitem(sys.modules, "quantstats", mock_qs)

    grid = [
        ["Date", "AAPL"],
        [2020, 0.10],
        [2019, 0.20],
    ]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {}}, data=grid)
    assert result["status"] == "ok"
    passed_series = mock_qs.reports.metrics.call_args[0][0]
    assert len(passed_series) == 2
    assert list(passed_series.values) == [0.20, 0.10]
    assert [ts.strftime("%Y-%m-%d") for ts in passed_series.index] == ["2019-01-01", "2020-01-01"]


def test_portfolio_tearsheet_drops_extra_datetime_column(monkeypatch):
    import sys
    import pandas as pd

    mock_qs = MagicMock()
    mock_qs.reports.metrics.return_value = pd.DataFrame({"val": [0.05]}, index=["Cumulative Return"])
    monkeypatch.setitem(sys.modules, "quantstats", mock_qs)

    grid = [
        ["Date", "Period", "AAPL", "MSFT"],
        ["2024-01-02", "2024-01-02", 0.02, 0.04],
        ["2024-01-01", "2024-01-01", 0.01, 0.03],
    ]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {}}, data=grid)
    assert result["status"] == "ok"
    assert "warning" not in result
    passed_series = mock_qs.reports.metrics.call_args[0][0]
    assert len(passed_series) == 2
    assert list(passed_series.values) == [0.02, 0.03]
    assert all(abs(v) < 1 for v in passed_series.values)
    assert [ts.strftime("%Y-%m-%d") for ts in passed_series.index] == ["2024-01-01", "2024-01-02"]


def test_portfolio_tearsheet_period_without_date_name_is_not_a_return(monkeypatch):
    import sys
    import pandas as pd

    mock_qs = MagicMock()
    mock_qs.reports.metrics.return_value = pd.DataFrame({"val": [0.05]}, index=["Cumulative Return"])
    monkeypatch.setitem(sys.modules, "quantstats", mock_qs)

    grid = [["Period", "AAPL"], ["2024-01-01", 0.01], ["2024-01-02", 0.02]]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {}}, data=grid)
    assert result["status"] == "ok"
    assert "warning" in result
    assert "synthetic" in result["warning"].lower()
    passed_series = mock_qs.reports.metrics.call_args[0][0]
    assert list(passed_series.values) == [0.01, 0.02]


def test_portfolio_tearsheet_mixed_date_formats_with_junk(monkeypatch):
    import sys
    import pandas as pd

    mock_qs = MagicMock()
    mock_qs.reports.metrics.return_value = pd.DataFrame({"val": [0.05]}, index=["Cumulative Return"])
    monkeypatch.setitem(sys.modules, "quantstats", mock_qs)

    grid = [
        ["Date", "AAPL"],
        ["2024-01-03", 0.03],
        ["01/01/2024", 0.01],
        ["notes", 0.09],
        ["2024-01-02 15:30:00", 0.02],
    ]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {}}, data=grid)
    assert result["status"] == "ok"
    passed_series = mock_qs.reports.metrics.call_args[0][0]
    assert len(passed_series) == 3
    assert list(passed_series.values) == [0.01, 0.02, 0.03]
    assert [ts.strftime("%Y-%m-%d %H:%M:%S") for ts in passed_series.index] == [
        "2024-01-01 00:00:00",
        "2024-01-02 15:30:00",
        "2024-01-03 00:00:00",
    ]


def test_portfolio_tearsheet_metrics_series(monkeypatch):
    import sys
    import pandas as pd

    mock_qs = MagicMock()
    mock_qs.reports.metrics.return_value = pd.Series({"Cumulative Return": 0.05, "Sharpe": 1.2})
    monkeypatch.setitem(sys.modules, "quantstats", mock_qs)

    grid = [["AAPL"], [0.01], [0.02]]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {}}, data=grid)
    assert result["status"] == "ok", result
    assert result["metrics"]["Cumulative Return"] == 0.05
    assert result["metrics"]["Sharpe"] == 1.2


def test_technical_analysis_string_indicator_and_headers_false(monkeypatch):
    import importlib

    real_import = importlib.import_module

    def _fake_import(name, package=None):
        if name == "pandas_ta":
            return object()
        return real_import(name, package)

    monkeypatch.setattr("plugin.scripting.venv.quant.importlib.import_module", _fake_import)

    # String indicator is normalized to list and Close column works with headers=False
    grid = [[100.0], [105.0], [110.0]]
    # With headers=False and no column named Close, it reports MISSING_COLUMN instead of AttributeError on c.lower()
    result = venv_run_quant(
        {"helper": "technical_analysis", "params": {"indicators": "rsi"}, "headers": False},
        data=grid,
    )
    assert result["status"] == "error"
    assert result["code"] == "MISSING_COLUMN"


def test_technical_analysis_unknown_indicator(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "pandas_ta", object())
    grid = [["Close"], [100.0]]
    result = venv_run_quant(
        {"helper": "technical_analysis", "params": {"indicators": ["rsi", "invalid_indicator"]}},
        data=grid,
    )
    assert result["status"] == "error"
    assert result["code"] == "INVALID_PARAMS"
    assert "invalid_indicator" in result["message"]


def test_efficient_frontier_honors_params_and_case_insensitive_date(monkeypatch):
    import sys

    mock_pypfopt = MagicMock()
    mock_ef_class = MagicMock()
    mock_ef_inst = MagicMock()
    mock_ef_class.return_value = mock_ef_inst
    mock_ef_inst.clean_weights.return_value = {"A": 0.6, "B": 0.4}

    monkeypatch.setitem(sys.modules, "pypfopt", mock_pypfopt)
    monkeypatch.setitem(sys.modules, "pypfopt.efficient_frontier", MagicMock(EfficientFrontier=mock_ef_class))
    monkeypatch.setitem(sys.modules, "pypfopt.expected_returns", MagicMock(mean_historical_return=MagicMock()))
    monkeypatch.setitem(sys.modules, "pypfopt.risk_models", MagicMock(CovarianceShrinkage=MagicMock()))

    grid = [
        ["DATETIME", "A", "B"],
        ["2024-01-01", 0.01, 0.02],
        ["2024-01-02", 0.02, -0.01],
    ]
    result = venv_run_quant(
        {
            "helper": "efficient_frontier",
            "params": {"risk_free_rate": 0.03, "weight_bounds": [0.1, 0.9]},
        },
        data=grid,
    )
    assert result["status"] == "ok"
    assert "warning" not in result
    mock_ef_class.assert_called_once()
    assert mock_ef_class.call_args[1]["weight_bounds"] == (0.1, 0.9)
    mock_ef_inst.max_sharpe.assert_called_once_with(risk_free_rate=0.03)


@contextmanager
def _fake_ta_accessor() -> Iterator[list[list[float]]]:
    """Register a DataFrame.ta stand-in and remove it when the test ends."""
    import pandas as pd

    seen: list[list[float]] = []

    class _FakeTa:
        def __init__(self, pandas_obj: Any) -> None:
            self._obj = pandas_obj

        def macd(self, close: Any = None, append: bool = True) -> None:
            seen.append([float(v) for v in self._obj[close].tolist()])
            self._obj["MACD_12_26_9"] = [float("nan")] * len(self._obj)

        def rsi(self, close: Any = None, append: bool = True) -> None:
            return None

        def bbands(self, close: Any = None, append: bool = True) -> None:
            return None

    pd.api.extensions.register_dataframe_accessor("ta")(_FakeTa)
    try:
        yield seen
    finally:
        pd.DataFrame._accessors.discard("ta")
        if hasattr(pd.DataFrame, "ta"):
            delattr(pd.DataFrame, "ta")


def test_technical_analysis_sorts_newest_first(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "pandas_ta", object())
    grid = [
        ["Date", "Close"],
        ["2024-01-03", 30.0],
        ["2024-01-01", 10.0],
        ["2024-01-02", 20.0],
    ]
    with _fake_ta_accessor() as seen:
        result = venv_run_quant(
            {"helper": "technical_analysis", "params": {"indicators": ["macd"]}},
            data=grid,
        )
    assert result["status"] == "ok", result
    assert seen == [[10.0, 20.0, 30.0]]
    assert result["table"]["rows"][0][0] == "2024-01-01"
    assert result["table"]["rows"][-1][0] == "2024-01-03"


def test_technical_analysis_sorts_mixed_serial_and_text(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "pandas_ta", object())
    grid = [
        ["Date", "Close"],
        [45293, 20.0],
        ["2024-01-01", 10.0],
        ["not-a-date", 99.0],
        [45294, 30.0],
    ]
    with _fake_ta_accessor() as seen:
        result = venv_run_quant(
            {"helper": "technical_analysis", "params": {"indicators": ["macd"]}},
            data=grid,
        )
    assert result["status"] == "ok", result
    assert seen == [[10.0, 20.0, 30.0, 99.0]]
    rows = result["table"]["rows"]
    assert [r[0] for r in rows] == ["2024-01-01", 45293, 45294, "not-a-date"]



def test_technical_analysis_warmup_nan_stays_float(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "pandas_ta", object())
    with _fake_ta_accessor():
        result = venv_run_quant(
            {"helper": "technical_analysis", "params": {"indicators": ["macd"]}},
            data=[["Close"], [1.0], [2.0]],
        )
    assert result["status"] == "ok", result
    cell = result["table"]["rows"][0][-1]
    assert type(cell) is float
    assert math.isnan(cell)


def test_technical_analysis_rejects_non_list_indicators(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "pandas_ta", object())
    result = venv_run_quant(
        {"helper": "technical_analysis", "params": {"indicators": 1}},
        data=[["Close"], [1.0]],
    )
    assert result["status"] == "error"
    assert result["code"] == "INVALID_PARAMS"


def test_portfolio_tearsheet_sorts_and_averages_duplicate_dates(monkeypatch):
    import sys
    import pandas as pd

    mock_qs = MagicMock()
    mock_qs.reports.metrics.return_value = pd.DataFrame({"val": [0.05]}, index=["Cumulative Return"])
    monkeypatch.setitem(sys.modules, "quantstats", mock_qs)
    grid = [
        ["Date", "AAPL"],
        ["2024-01-03", 0.03],
        ["2024-01-01", 0.01],
        ["2024-01-01", 0.05],
        ["2024-01-02", 0.02],
    ]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {}}, data=grid)
    assert result["status"] == "ok", result
    series = mock_qs.reports.metrics.call_args[0][0]
    assert len(series) == 3
    assert str(series.index[0]).startswith("2024-01-01")
    assert str(series.index[1]).startswith("2024-01-02")
    assert str(series.index[2]).startswith("2024-01-03")
    assert math.isclose(float(series.iloc[0]), 0.03)
    assert "duplicate" in result["warning"].lower()


def test_portfolio_tearsheet_string_matches_int_column(monkeypatch):
    import sys
    import pandas as pd

    mock_qs = MagicMock()
    mock_qs.reports.metrics.return_value = pd.DataFrame({"val": [0.05]}, index=["Cumulative Return"])
    monkeypatch.setitem(sys.modules, "quantstats", mock_qs)
    frame = pd.DataFrame({0: [0.01, 0.02], 1: [0.03, -0.01]})
    result = venv_run_quant(
        {"helper": "portfolio_tearsheet", "params": {"column": "0"}},
        data=frame,
    )
    assert result["status"] == "ok", result
    series = mock_qs.reports.metrics.call_args[0][0]
    assert [float(v) for v in series.tolist()] == [0.01, 0.02]


def test_fetch_stringifies_unnamed_and_timezone_datetimes(monkeypatch):
    import sys
    import pandas as pd

    idx = pd.to_datetime(["2024-01-02", "2024-01-01"])
    frame = pd.DataFrame({"Close": [2.0, 1.0]}, index=idx)
    frame["When"] = pd.to_datetime(["2024-01-02 15:00", "2024-01-01 15:00"], utc=True)
    fake_yf = MagicMock()
    fake_yf.download.return_value = frame
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    result = venv_run_quant({"helper": "fetch_historical_data", "params": {"tickers": ["AAPL"]}})
    assert result["status"] == "ok", result
    columns = result["table"]["columns"]
    assert columns[0] == "index"
    assert "When" in columns
    when_at = columns.index("When")
    for row in result["table"]["rows"]:
        assert isinstance(row[0], str)
        assert isinstance(row[when_at], str)
        assert not row[when_at].startswith("Timestamp")


def test_fetch_keeps_ticker_in_multiindex_columns(monkeypatch):
    import sys
    import pandas as pd

    frame = pd.DataFrame(
        [[1.0, 2.0]],
        columns=pd.MultiIndex.from_tuples([("Close", "AAPL"), ("Close", "MSFT")]),
        index=pd.to_datetime(["2024-01-01"]),
    )
    frame.index.name = "Date"
    fake_yf = MagicMock()
    fake_yf.download.return_value = frame
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    result = venv_run_quant(
        {"helper": "fetch_historical_data", "params": {"tickers": ["AAPL", "MSFT"]}},
    )
    assert result["status"] == "ok", result
    assert result["table"]["columns"] == ["Date", "Close / AAPL", "Close / MSFT"]


def test_fetch_splits_comma_separated_tickers(monkeypatch):
    import sys
    import pandas as pd

    fake_yf = MagicMock()
    fake_yf.download.return_value = pd.DataFrame({"Close": [1.0]})
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    venv_run_quant({"helper": "fetch_historical_data", "params": {"tickers": "AAPL, MSFT"}})
    assert fake_yf.download.call_args[0][0] == ["AAPL", "MSFT"]

    venv_run_quant({"helper": "fetch_historical_data", "params": {"tickers": "BRK.B"}})
    assert fake_yf.download.call_args[0][0] == ["BRK.B"]

    venv_run_quant({"helper": "fetch_historical_data", "params": {"tickers": ["AAPL,MSFT"]}})
    assert fake_yf.download.call_args[0][0] == ["AAPL", "MSFT"]


def test_fetch_retries_when_multi_level_index_is_unknown(monkeypatch):
    import sys
    import pandas as pd

    calls: list[dict[str, Any]] = []

    def download(tickers: Any, **kwargs: Any) -> pd.DataFrame:
        calls.append(dict(kwargs))
        if "multi_level_index" in kwargs:
            raise TypeError("download() got an unexpected keyword argument 'multi_level_index'")
        return pd.DataFrame({"Close": [1.0]}, index=pd.to_datetime(["2024-01-01"]))

    fake_yf = MagicMock()
    fake_yf.download.side_effect = download
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    result = venv_run_quant({"helper": "fetch_historical_data", "params": {"tickers": ["AAPL"]}})
    assert result["status"] == "ok", result
    assert "multi_level_index" in calls[0]
    assert "multi_level_index" not in calls[1]
    assert calls[0]["auto_adjust"] is True
    assert calls[1]["auto_adjust"] is True


def test_fetch_other_typeerror_stays_execution_error(monkeypatch):
    import sys

    def download(*args: Any, **kwargs: Any) -> None:
        raise TypeError("interval not understood")

    fake_yf = MagicMock()
    fake_yf.download.side_effect = download
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    result = venv_run_quant({"helper": "fetch_historical_data", "params": {"tickers": ["AAPL"]}})
    assert result["status"] == "error"
    assert result["code"] == "EXECUTION_ERROR"
    assert "interval" in result["message"]


def _install_frontier_mocks(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import sys

    mock_ef_class = MagicMock()
    mock_ef_inst = MagicMock()
    mock_ef_class.return_value = mock_ef_inst
    mock_ef_inst.clean_weights.return_value = {"A": 0.6, "B": 0.4}
    monkeypatch.setitem(sys.modules, "pypfopt", MagicMock())
    monkeypatch.setitem(sys.modules, "pypfopt.efficient_frontier", MagicMock(EfficientFrontier=mock_ef_class))
    monkeypatch.setitem(sys.modules, "pypfopt.expected_returns", MagicMock(mean_historical_return=MagicMock()))
    monkeypatch.setitem(sys.modules, "pypfopt.risk_models", MagicMock(CovarianceShrinkage=MagicMock()))
    return mock_ef_class


def test_efficient_frontier_rejects_bad_bounds_and_rate(monkeypatch):
    mock_ef_class = _install_frontier_mocks(monkeypatch)
    grid = [
        ["A", "B"],
        [0.01, 0.02],
        [0.02, -0.01],
    ]
    bounds = venv_run_quant(
        {"helper": "efficient_frontier", "params": {"weight_bounds": [0.1]}},
        data=grid,
    )
    assert bounds["status"] == "error"
    assert bounds["code"] == "INVALID_PARAMS"
    rate = venv_run_quant(
        {"helper": "efficient_frontier", "params": {"risk_free_rate": "nope"}},
        data=grid,
    )
    assert rate["status"] == "error"
    assert rate["code"] == "INVALID_PARAMS"
    mock_ef_class.assert_not_called()


def test_fetch_pins_auto_adjust(monkeypatch):
    import sys
    import pandas as pd

    fake_yf = MagicMock()
    fake_yf.download.return_value = pd.DataFrame({"Close": [1.0]})
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    result = venv_run_quant({"helper": "fetch_historical_data", "params": {"tickers": ["AAPL"]}})
    assert result["status"] == "ok", result
    assert fake_yf.download.call_args.kwargs["auto_adjust"] is True


def test_efficient_frontier_drops_incomplete_rows(monkeypatch):
    import sys

    _install_frontier_mocks(monkeypatch)
    mean = sys.modules["pypfopt.expected_returns"].mean_historical_return
    grid = [
        ["A", "B"],
        [0.01, 0.02],
        [0.02, float("nan")],
        [0.03, 0.01],
    ]
    result = venv_run_quant({"helper": "efficient_frontier", "params": {}}, data=grid)
    assert result["status"] == "ok", result
    passed = mean.call_args[0][0]
    assert len(passed) == 2
    assert [float(v) for v in passed["A"].tolist()] == [0.01, 0.03]
    assert result["warning"] == "Dropped 1 row with missing or non-finite returns."


def test_efficient_frontier_drops_empty_column(monkeypatch):
    import sys

    _install_frontier_mocks(monkeypatch)
    mean = sys.modules["pypfopt.expected_returns"].mean_historical_return
    grid = [
        ["A", "B", "C"],
        [0.01, float("nan"), 0.02],
        [0.03, float("nan"), 0.01],
    ]
    result = venv_run_quant({"helper": "efficient_frontier", "params": {}}, data=grid)
    assert result["status"] == "ok", result
    passed = mean.call_args[0][0]
    assert list(passed.columns) == ["A", "C"]
    assert len(passed) == 2
    assert result["warning"] == "Dropped 1 empty column."


def test_efficient_frontier_accepts_none_bounds(monkeypatch):
    mock_ef_class = _install_frontier_mocks(monkeypatch)
    grid = [
        ["A", "B"],
        [0.01, 0.02],
        [0.02, -0.01],
    ]
    result = venv_run_quant(
        {"helper": "efficient_frontier", "params": {"weight_bounds": [None, None]}},
        data=grid,
    )
    assert result["status"] == "ok", result
    assert mock_ef_class.call_args[1]["weight_bounds"] == (None, None)
    assert "warning" not in result


def test_efficient_frontier_drops_datetime_and_keeps_year(monkeypatch):
    import sys
    import pandas as pd

    _install_frontier_mocks(monkeypatch)
    mean = sys.modules["pypfopt.expected_returns"].mean_historical_return
    df = pd.DataFrame({
        "Date": pd.to_datetime(["2024-01-01", "2024-01-02"]),
        "When": pd.to_datetime(["2024-01-01", "2024-01-02"], utc=True),
        "Delta": pd.to_timedelta(["1 days", "2 days"]),
        "Year": [2024, 2024],
        "AAPL": [0.01, 0.02],
        "MSFT": [0.03, 0.04],
    })
    result = venv_run_quant({"helper": "efficient_frontier", "params": {}}, data=df)
    assert result["status"] == "ok", result
    passed = mean.call_args[0][0]
    assert list(passed.columns) == ["Year", "AAPL", "MSFT"]
    assert list(passed["AAPL"]) == [0.01, 0.02]

