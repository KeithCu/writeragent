# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted viz helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

from plugin.scripting.viz import run_viz


def _mock_figure_payload():
    return {"__wa_payload__": "image", "format": "png", "data": b"abc123"}


def _mock_plt_module():
    plt_mod = MagicMock()
    fig = MagicMock()
    ax = MagicMock()
    ax.get_title.return_value = "Test plot"
    plt_mod.subplots.return_value = (fig, ax)
    return plt_mod


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_run_viz_quick_plot(mock_plt, _mock_payload):
    mock_plt.return_value = _mock_plt_module()
    df = pd.DataFrame({"Sales": [10, 20, 30], "Region": ["A", "B", "C"]})

    result = run_viz({"helper": "quick_plot", "params": {}}, df, {})

    assert result["status"] == "ok"
    assert result["helper"] == "quick_plot"
    assert result["image"]["__wa_payload__"] == "image"


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_run_viz_plot_data_scatter(mock_plt, _mock_payload):
    mock_plt.return_value = _mock_plt_module()
    df = pd.DataFrame({"x": [1, 2, 3], "y": [4, 5, 6]})

    result = run_viz(
        {
            "helper": "plot_data",
            "params": {"spec": {"chart_type": "scatter", "x": "x", "y": "y", "title": "Test"}},
        },
        df,
        {},
    )

    assert result["status"] == "ok"
    assert result["chart_type"] == "scatter"
    assert result["title"] == "Test"


def test_run_viz_missing_matplotlib():
    with patch("plugin.scripting.venv.viz._require_matplotlib", return_value=None):
        result = run_viz({"helper": "quick_plot"}, pd.DataFrame({"a": [1]}), {})
    assert result["status"] == "error"
    assert result["code"] == "MISSING_PACKAGE"


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_time_series_plot_with_forecast_bands(mock_plt, _mock_payload):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    fig, ax = plt_mod.subplots.return_value
    ax.get_legend_handles_labels.return_value = ([], [])

    df = pd.DataFrame(
        {
            "date": ["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"],
            "Value": [100.0, 110.0, None, None],
            "forecast": [None, None, 120.0, 125.0],
            "lower": [None, None, 115.0, 118.0],
            "upper": [None, None, 125.0, 132.0],
        }
    )

    result = run_viz(
        {
            "helper": "time_series_plot",
            "params": {
                "date_col": "date",
                "value_col": "Value",
                "forecast_col": "forecast",
                "lower_col": "lower",
                "upper_col": "upper",
            },
        },
        df,
        {},
    )

    assert result["status"] == "ok"
    assert result["helper"] == "time_series_plot"
    ax.plot.assert_called()
    ax.fill_between.assert_called_once()
