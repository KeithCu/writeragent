# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted viz helpers."""

from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from plugin.scripting.viz import (
    get_viz_script_templates,
    parse_viz_script_header,
    run_viz,
    supports_viz_manual,
)


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


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_time_series_plot_parses_serial_history_beside_iso_forecast(mock_plt, _mock_payload):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    fig, ax = plt_mod.subplots.return_value
    ax.get_legend_handles_labels.return_value = ([], [])

    df = pd.DataFrame(
        {
            "date": [45292.0, "2024-03-01"],
            "Value": [100.0, None],
            "forecast": [None, 120.0],
        }
    )

    result = run_viz(
        {
            "helper": "time_series_plot",
            "params": {
                "date_col": "date",
                "value_col": "Value",
                "forecast_col": "forecast",
            },
        },
        df,
        {},
    )

    assert result["status"] == "ok"
    assert ax.plot.call_count >= 1
    for call in ax.plot.call_args_list:
        x_vals = call[0][0]
        for x in x_vals:
            assert pd.Timestamp(x).year == 2024


def test_insert_image_payload_writer_uses_product_display_name():
    ctx = MagicMock()
    doc = MagicMock()
    payload = {"__wa_payload__": "image", "format": "png", "data": b"x"}
    with (
        patch("plugin.scripting.viz.is_calc", return_value=False),
        patch("plugin.scripting.viz.is_writer", return_value=True),
        patch("plugin.scripting.viz.is_draw", return_value=False),
        patch("plugin.scripting.viz.write_image_payload_to_temp", return_value="/tmp/p.png"),
        patch("plugin.writer.images.image_tools.insert_image_at_locator") as ins,
        patch("plugin.framework.uno_context.product_display_name", return_value="LibrePy"),
    ):
        from plugin.scripting.viz import insert_image_payload_for_doc

        insert_image_payload_for_doc(ctx, doc, payload, title="Plot")
    assert ins.call_args.kwargs["description"] == "LibrePy plot"


def test_insert_image_payload_calc_passes_target_doc():
    """Calc egress must insert on the script document, not the front window."""
    ctx = MagicMock()
    doc = MagicMock(name="target")
    payload = {"__wa_payload__": "image", "format": "png", "data": b"x"}
    with (
        patch("plugin.scripting.viz.is_calc", return_value=True),
        patch("plugin.calc.python.image_egress.insert_image_result_on_sheet") as insert,
    ):
        from plugin.scripting.viz import insert_image_payload_for_doc

        insert_image_payload_for_doc(ctx, doc, payload, title="Plot")
    insert.assert_called_once_with(ctx, payload, doc=doc)


def test_insert_image_payload_calc_surfaces_egress_failure():
    ctx = MagicMock()
    doc = MagicMock(name="target")
    payload = {"__wa_payload__": "image", "format": "png", "data": b"x"}
    from plugin.calc.python.image_egress import ImageEgressError

    with (
        patch("plugin.scripting.viz.is_calc", return_value=True),
        patch(
            "plugin.calc.python.image_egress.insert_image_result_on_sheet",
            side_effect=ImageEgressError("target sheet has no DrawPage; image was not inserted"),
        ),
        pytest.raises(ImageEgressError),
    ):
        from plugin.scripting.viz import insert_image_payload_for_doc

        insert_image_payload_for_doc(ctx, doc, payload, title="Plot")


# --- Viz Run Python Script templates (from test_viz_templates.py) ---

def test_get_viz_script_templates_include_run_call():
    templates = get_viz_script_templates()
    assert "quick_plot" in templates
    assert "from writeragent.scripting.viz import quick_plot" in templates["quick_plot"]
    assert "# writeragent:viz" not in templates["quick_plot"]


def test_viz_template_body_includes_helper_params():
    code = get_viz_script_templates()["correlation_heatmap"]
    assert "correlation_heatmap(data, method='pearson')" in code
    assert "from writeragent.scripting.viz import correlation_heatmap" in code


def test_parse_viz_script_header_rejects_unknown_helper():
    code = "# writeragent:viz helper=not_a_helper params={}\n"
    assert parse_viz_script_header(code) is None


def test_run_trusted_viz_reads_on_main_and_runs_client_off_main():
    inside = {"flag": False}

    def exec_main(fn, *args, **kwargs):
        inside["flag"] = True
        try:
            return fn(*args, **kwargs)
        finally:
            inside["flag"] = False

    def client(_ctx, spec, _data, context=None):
        del context
        assert not inside["flag"], "client_run_viz must stay off the UNO hop"
        assert spec["helper"] == "time_series_plot"
        return {"status": "ok", "helper": "time_series_plot"}

    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=exec_main) as mock_main,
        patch("plugin.scripting.viz.is_calc", return_value=True),
        patch("plugin.scripting.viz.is_writer", return_value=False),
        patch("plugin.calc.calc_addin_data._resolve_python_data", return_value=([["date", "Value"], ["2024-01-01", 1.0]], None)),
        patch("plugin.scripting.viz.client_run_viz", side_effect=client) as mock_client,
        patch("plugin.calc.bridge.CalcBridge") as mock_bridge,
    ):
        mock_bridge.return_value.get_active_sheet.return_value.getName.return_value = "Sheet1"
        from plugin.scripting.viz import run_trusted_viz

        result = run_trusted_viz(
            MagicMock(),
            MagicMock(),
            helper="time_series_plot",
            data=[["date", "Value"], ["2024-01-01", 1.0]],
        )

    assert result["status"] == "ok"
    mock_main.assert_called_once()
    mock_client.assert_called_once()


def test_insert_viz_result_into_doc() -> None:
    from plugin.scripting.viz import insert_viz_result_into_doc

    doc = MagicMock()
    ctx = MagicMock()
    payload = {"__wa_payload__": "image", "format": "png", "data": b"x"}
    result = {"status": "ok", "image": payload}

    with patch("plugin.scripting.viz.insert_image_payload_for_doc") as mock_insert:
        res = insert_viz_result_into_doc(ctx, doc, result)
        assert res == 1
        mock_insert.assert_called_once_with(ctx, doc, payload, title="Plot")


def test_run_viz_bad_header_row_is_invalid_spec():
    result = run_viz({"helper": "quick_plot", "header_row": "nope"}, [])
    assert result["status"] == "error"
    assert result["code"] == "INVALID_SPEC"


@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_quick_plot_non_numeric_y_col(mock_plt):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    df = pd.DataFrame({"Sales": [10.0, 20.0, 30.0], "Region": ["A", "B", "C"]})

    result = run_viz({"helper": "quick_plot", "params": {"y_col": "Region"}}, df, {})

    assert result["status"] == "error"
    assert result["code"] == "NON_NUMERIC_COLUMN"
    assert result["message"] == "Non-numeric columns: Region"
    assert result["helper"] == "quick_plot"
    plt_mod.subplots.assert_not_called()


@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_plot_data_non_numeric_y(mock_plt):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    df = pd.DataFrame({"Sales": [10.0, 20.0, 30.0], "Region": ["A", "B", "C"]})

    result = run_viz({"helper": "plot_data", "params": {"spec": {"y": "Region"}}}, df, {})

    assert result["status"] == "error"
    assert result["code"] == "NON_NUMERIC_COLUMN"
    assert result["message"] == "Non-numeric columns: Region"
    assert result["helper"] == "plot_data"
    plt_mod.subplots.assert_not_called()


@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_plot_data_scatter_non_numeric_x(mock_plt):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    df = pd.DataFrame({"Sales": [10.0, 20.0, 30.0], "Region": ["A", "B", "C"]})

    result = run_viz(
        {
            "helper": "plot_data",
            "params": {"spec": {"chart_type": "scatter", "x": "Region", "y": "Sales"}},
        },
        df,
        {},
    )

    assert result["status"] == "error"
    assert result["code"] == "NON_NUMERIC_COLUMN"
    assert result["message"] == "Non-numeric columns: Region"
    assert result["helper"] == "plot_data"
    plt_mod.subplots.assert_not_called()


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_plot_data_missing_y_uses_first_numeric(mock_plt, _mock_payload):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    fig, ax = plt_mod.subplots.return_value
    df = pd.DataFrame({"Sales": [10.0, 20.0, 30.0], "Region": ["A", "B", "C"]})

    result = run_viz({"helper": "plot_data", "params": {"spec": {"y": "nope"}}}, df, {})

    assert result["status"] == "ok"
    ax.set_ylabel.assert_called_with("Sales")


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_plot_data_hue_is_ignored(mock_plt, _mock_payload):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    fig, ax = plt_mod.subplots.return_value
    df = pd.DataFrame({"x": [1.0, 2.0], "y": [3.0, 4.0], "g": ["A", "B"]})

    result = run_viz(
        {
            "helper": "plot_data",
            "params": {"spec": {"chart_type": "line", "x": "x", "y": "y", "hue": "g", "title": "T"}},
        },
        df,
        {},
    )

    assert result["status"] == "ok"
    assert result["legend"] is False
    ax.legend.assert_not_called()


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_quick_plot_line_plots_x_col(mock_plt, _mock_payload):
    """A line chart used to ignore x_col once the frame passed 12 rows."""
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    fig, ax = plt_mod.subplots.return_value
    months = [f"m{i}" for i in range(13)]
    sales = [float(i * 10) for i in range(13)]
    df = pd.DataFrame({"Month": months, "Sales": sales})

    result = run_viz({"helper": "quick_plot", "params": {"x_col": "Month"}}, df, {})

    assert result["status"] == "ok"
    assert result["chart_type"] == "line"
    ax.plot.assert_called_once()
    args, _kwargs = ax.plot.call_args
    assert list(args[0]) == months
    assert list(args[1]) == sales


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_quick_plot_bar_tick_labels_use_x_col(mock_plt, _mock_payload):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    fig, ax = plt_mod.subplots.return_value
    df = pd.DataFrame({"Region": ["A", "B", "C"], "Sales": [10.0, 20.0, 30.0]})

    result = run_viz({"helper": "quick_plot", "params": {"x_col": "Region"}}, df, {})

    assert result["status"] == "ok"
    assert result["chart_type"] == "bar"
    assert list(ax.set_xticklabels.call_args[0][0]) == ["A", "B", "C"]
    ax.plot.assert_not_called()


@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_time_series_missing_historical_column(mock_plt):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    df = pd.DataFrame({"date": ["2024-01-01"], "Value": [10.0]})

    result = run_viz(
        {
            "helper": "time_series_plot",
            "params": {"date_col": "date", "value_col": "Value", "historical_value_col": "Actual"},
        },
        df,
        {},
    )

    assert result["status"] == "error"
    assert result["code"] == "UNKNOWN_COLUMN"
    assert result["message"] == "Unknown historical value column 'Actual'"
    assert result["helper"] == "time_series_plot"
    plt_mod.subplots.assert_not_called()


@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_time_series_missing_historical_names_that_column_when_value_is_also_missing(mock_plt):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    df = pd.DataFrame({"date": ["2024-01-01"]})

    result = run_viz(
        {
            "helper": "time_series_plot",
            "params": {"date_col": "date", "value_col": "Value", "historical_value_col": "Actual"},
        },
        df,
        {},
    )

    assert result["status"] == "error"
    assert result["code"] == "UNKNOWN_COLUMN"
    assert result["message"] == "Unknown historical value column 'Actual'"
    plt_mod.subplots.assert_not_called()


@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_time_series_missing_value_column(mock_plt):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    df = pd.DataFrame({"date": ["2024-01-01"]})

    result = run_viz(
        {
            "helper": "time_series_plot",
            "params": {"date_col": "date", "value_col": "Value"},
        },
        df,
        {},
    )

    assert result["status"] == "error"
    assert result["code"] == "UNKNOWN_COLUMN"
    assert result["message"] == "Unknown value column 'Value'"
    plt_mod.subplots.assert_not_called()


@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_time_series_insufficient_data_does_not_open_figure(mock_plt):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    df = pd.DataFrame({"date": ["2024-01-01"], "Value": [None]})

    result = run_viz(
        {
            "helper": "time_series_plot",
            "params": {"date_col": "date", "value_col": "Value"},
        },
        df,
        {},
    )

    assert result["status"] == "error"
    assert result["code"] == "INSUFFICIENT_DATA"
    assert result["message"] == "No rows to plot."
    plt_mod.subplots.assert_not_called()


@patch("plugin.scripting.venv.venv_sandbox._figure_to_image_payload", side_effect=RuntimeError("render failed"))
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_figure_closes_when_savefig_raises(mock_plt, _mock_payload):
    """Savefig used to skip plt.close, and trusted actions never call _close_open_figures."""
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    fig, ax = plt_mod.subplots.return_value
    df = pd.DataFrame({"Sales": [10.0, 20.0, 30.0]})

    with pytest.raises(RuntimeError, match="render failed"):
        run_viz({"helper": "quick_plot", "params": {}}, df, {})

    plt_mod.close.assert_called_once_with(fig)


@pytest.mark.parametrize("method", ["nope", None, "", "covariance"])
def test_correlation_heatmap_rejects_unknown_method(method):
    df = pd.DataFrame({"a": [1.0, 2.0], "b": [3.0, 4.0]})

    result = run_viz({"helper": "correlation_heatmap", "params": {"method": method}}, df, {})

    assert result["status"] == "error"
    assert result["code"] == "INVALID_PARAMS"
    assert result["message"] == "method must be pearson, kendall, or spearman"
    assert result["helper"] == "correlation_heatmap"


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_correlation_heatmap_casefolds_method(mock_plt, _mock_payload):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    fig, ax = plt_mod.subplots.return_value
    df = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": [3.0, 4.0, 5.0]})

    result = run_viz({"helper": "correlation_heatmap", "params": {"method": "Pearson"}}, df, {})

    assert result["status"] == "ok"
    ax.set_title.assert_called_with("Correlation (pearson)")


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_correlation_heatmap_seaborn_failure_uses_imshow(mock_plt, _mock_payload):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    fig, ax = plt_mod.subplots.return_value
    df = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": [3.0, 4.0, 5.0]})

    fake_seaborn = MagicMock()
    fake_seaborn.heatmap.side_effect = RuntimeError("boom")

    with patch.dict(sys.modules, {"seaborn": fake_seaborn}):
        result = run_viz({"helper": "correlation_heatmap", "params": {"method": "pearson"}}, df, {})

    assert result["status"] == "ok"
    fake_seaborn.heatmap.assert_called_once()
    ax.imshow.assert_called_once()
    plt_mod.close.assert_called_once()


def test_run_trusted_viz_sheet_disposal_propagates():
    from plugin.scripting.viz import run_trusted_viz
    from plugin.framework.errors import DocumentDisposedError

    class DisposedException(Exception):
        pass

    client_mock = MagicMock()
    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.scripting.viz.is_calc", return_value=True),
        patch("plugin.scripting.viz.is_writer", return_value=False),
        patch("plugin.calc.calc_addin_data._resolve_python_data", return_value=([["x"], [1]], None)),
        patch("plugin.scripting.viz.client_run_viz", client_mock),
        patch("plugin.calc.bridge.CalcBridge") as mock_bridge,
    ):
        mock_bridge.return_value.get_active_sheet.side_effect = DisposedException("disposed")
        with pytest.raises(DocumentDisposedError):
            run_trusted_viz(
                MagicMock(),
                MagicMock(),
                helper="quick_plot",
                data=[["x"], [1]],
            )
    client_mock.assert_not_called()


def test_run_trusted_viz_sheet_name_failure_is_best_effort():
    from plugin.scripting.viz import run_trusted_viz

    client_mock = MagicMock(return_value={"status": "ok", "helper": "quick_plot"})
    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.scripting.viz.is_calc", return_value=True),
        patch("plugin.scripting.viz.is_writer", return_value=False),
        patch("plugin.calc.calc_addin_data._resolve_python_data", return_value=([["x"], [1]], None)),
        patch("plugin.scripting.viz.client_run_viz", client_mock),
        patch("plugin.calc.bridge.CalcBridge") as mock_bridge,
    ):
        mock_bridge.return_value.get_active_sheet.side_effect = RuntimeError("no sheet")
        result = run_trusted_viz(
            MagicMock(),
            MagicMock(),
            helper="quick_plot",
            data=[["x"], [1]],
        )
    assert result == {"status": "ok", "helper": "quick_plot"}
    client_mock.assert_called_once()


def test_insert_image_payload_writer_unlinks_temp():
    ctx = MagicMock()
    doc = MagicMock()
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        path = tmp.name
    assert os.path.exists(path)

    payload = {"__wa_payload__": "image", "format": "png", "data": b"x"}
    with (
        patch("plugin.scripting.viz.is_calc", return_value=False),
        patch("plugin.scripting.viz.is_writer", return_value=True),
        patch("plugin.scripting.viz.is_draw", return_value=False),
        patch("plugin.scripting.viz.write_image_payload_to_temp", return_value=path),
        patch("plugin.writer.images.image_tools.insert_image_at_locator") as ins,
    ):
        from plugin.scripting.viz import insert_image_payload_for_doc

        insert_image_payload_for_doc(ctx, doc, payload)

    assert not os.path.exists(path)
    ins.assert_called_once()


def test_insert_image_payload_writer_unlinks_temp_when_insert_raises():
    ctx = MagicMock()
    doc = MagicMock()
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        path = tmp.name
    assert os.path.exists(path)

    payload = {"__wa_payload__": "image", "format": "png", "data": b"x"}
    with (
        patch("plugin.scripting.viz.is_calc", return_value=False),
        patch("plugin.scripting.viz.is_writer", return_value=True),
        patch("plugin.scripting.viz.is_draw", return_value=False),
        patch("plugin.scripting.viz.write_image_payload_to_temp", return_value=path),
        patch("plugin.writer.images.image_tools.insert_image_at_locator", side_effect=RuntimeError("fail")),
        pytest.raises(RuntimeError),
    ):
        from plugin.scripting.viz import insert_image_payload_for_doc

        insert_image_payload_for_doc(ctx, doc, payload)

    assert not os.path.exists(path)


def test_insert_image_payload_draw_unlinks_temp():
    ctx = MagicMock()
    doc = MagicMock()
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        path = tmp.name
    assert os.path.exists(path)

    payload = {"__wa_payload__": "image", "format": "png", "data": b"x"}
    with (
        patch("plugin.scripting.viz.is_calc", return_value=False),
        patch("plugin.scripting.viz.is_writer", return_value=False),
        patch("plugin.scripting.viz.is_draw", return_value=True),
        patch("plugin.scripting.viz.write_image_payload_to_temp", return_value=path),
        patch("plugin.writer.images.image_tools.insert_image") as ins,
    ):
        from plugin.scripting.viz import insert_image_payload_for_doc

        insert_image_payload_for_doc(ctx, doc, payload)

    assert not os.path.exists(path)
    ins.assert_called_once()
    assert ins.call_args.kwargs.get("add_to_gallery") is False


def test_insert_image_payload_calc_does_not_write_its_own_temp():
    ctx = MagicMock()
    doc = MagicMock()
    payload = {"__wa_payload__": "image", "format": "png", "data": b"x"}
    with (
        patch("plugin.scripting.viz.is_calc", return_value=True),
        patch("plugin.scripting.viz.write_image_payload_to_temp") as mock_write,
        patch("plugin.calc.python.image_egress.insert_image_result_on_sheet") as mock_insert,
    ):
        from plugin.scripting.viz import insert_image_payload_for_doc

        insert_image_payload_for_doc(ctx, doc, payload)

    mock_write.assert_not_called()
    mock_insert.assert_called_once()


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_quick_plot_integer_columns_are_numeric(mock_plt, _mock_payload):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    fig, ax = plt_mod.subplots.return_value
    df = pd.DataFrame([[1.0, 2.0], [3.0, 4.0]])

    result = run_viz({"helper": "quick_plot", "params": {}}, df, {})

    assert result["status"] == "ok"
    assert result.get("code") != "NON_NUMERIC_COLUMN"
    ax.set_ylabel.assert_called_with("0")


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_quick_plot_y_col_string_matches_integer_column(mock_plt, _mock_payload):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    fig, ax = plt_mod.subplots.return_value
    df = pd.DataFrame([[1.0, 2.0], [3.0, 4.0]])

    result = run_viz({"helper": "quick_plot", "params": {"y_col": "1"}}, df, {})

    assert result["status"] == "ok"
    ax.set_ylabel.assert_called_with("1")


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_plot_data_integer_y_is_numeric(mock_plt, _mock_payload):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    fig, ax = plt_mod.subplots.return_value
    df = pd.DataFrame([[1.0, 2.0], [3.0, 4.0]])

    result = run_viz({"helper": "plot_data", "params": {"spec": {"y": 1, "chart_type": "line"}}}, df, {})

    assert result["status"] == "ok"
    ax.set_ylabel.assert_called_with("1")


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_quick_plot_omitted_y_col_plots_first_numeric_only(mock_plt, _mock_payload):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    fig, ax = plt_mod.subplots.return_value
    df = pd.DataFrame({"A": [1.0, 2.0, 3.0], "B": [4.0, 5.0, 6.0]})

    result = run_viz({"helper": "quick_plot", "params": {}}, df, {})

    assert result["status"] == "ok"
    assert ax.plot.call_count + ax.bar.call_count == 1
    ax.set_ylabel.assert_called_with("A")


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_quick_plot_y_col_selects_later_column(mock_plt, _mock_payload):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    fig, ax = plt_mod.subplots.return_value
    df = pd.DataFrame({"A": [1.0, 2.0, 3.0], "B": [4.0, 5.0, 6.0]})

    result = run_viz({"helper": "quick_plot", "params": {"y_col": "B"}}, df, {})

    assert result["status"] == "ok"
    ax.set_ylabel.assert_called_with("B")


@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_quick_plot_y_cols_key_is_invalid(mock_plt):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    df = pd.DataFrame({"A": [1.0, 2.0, 3.0], "B": [4.0, 5.0, 6.0]})

    result = run_viz({"helper": "quick_plot", "params": {"y_cols": ["B"]}}, df, {})

    assert result["status"] == "error"
    assert result["code"] == "INVALID_PARAMS"
    assert result["message"] == "quick_plot takes y_col, not y_cols"
    plt_mod.subplots.assert_not_called()


@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_quick_plot_y_col_list_is_invalid(mock_plt):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    df = pd.DataFrame({"A": [1.0, 2.0, 3.0], "B": [4.0, 5.0, 6.0]})

    result = run_viz({"helper": "quick_plot", "params": {"y_col": ["B"]}}, df, {})

    assert result["status"] == "error"
    assert result["code"] == "INVALID_PARAMS"
    assert result["message"] == "y_col must be one column name"
    plt_mod.subplots.assert_not_called()


@pytest.mark.parametrize("bad_type", ["barchart", "LINECHART", 1])
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_plot_data_unknown_chart_type(mock_plt, bad_type):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    df = pd.DataFrame({"A": [1.0, 2.0, 3.0]})

    result = run_viz({"helper": "plot_data", "params": {"spec": {"chart_type": bad_type}}}, df, {})

    assert result["status"] == "error"
    assert result["code"] == "INVALID_PARAMS"
    assert result["message"] == "chart_type must be line, scatter, histogram, or bar"
    assert result["helper"] == "plot_data"
    plt_mod.subplots.assert_not_called()


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_plot_data_chart_type_strips_and_casefolds(mock_plt, _mock_payload):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    df = pd.DataFrame({"A": [1.0, 2.0, 3.0]})

    result = run_viz({"helper": "plot_data", "params": {"spec": {"chart_type": " Bar "}}}, df, {})

    assert result["status"] == "ok"
    assert result["chart_type"] == "bar"


@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_time_series_all_null_forecast_column_is_insufficient(mock_plt):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    df = pd.DataFrame(
        {
            "date": ["2024-01-01", "2024-02-01"],
            "Value": [None, None],
            "forecast": [None, None],
        }
    )

    result = run_viz(
        {
            "helper": "time_series_plot",
            "params": {"date_col": "date", "value_col": "Value", "forecast_col": "forecast"},
        },
        df,
        {},
    )

    assert result["status"] == "error"
    assert result["code"] == "INSUFFICIENT_DATA"
    assert result["message"] == "No rows to plot."
    plt_mod.subplots.assert_not_called()


@patch("plugin.scripting.venv.viz._figure_payload", return_value=_mock_figure_payload())
@patch("plugin.scripting.venv.viz._require_matplotlib")
def test_time_series_band_only_still_plots(mock_plt, _mock_payload):
    plt_mod = _mock_plt_module()
    mock_plt.return_value = plt_mod
    fig, ax = plt_mod.subplots.return_value
    ax.get_legend_handles_labels.return_value = ([], [])
    df = pd.DataFrame(
        {
            "date": ["2024-01-01", "2024-02-01"],
            "Value": [None, None],
            "forecast": [None, None],
            "lower": [10.0, 15.0],
            "upper": [20.0, 25.0],
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
    ax.fill_between.assert_called_once()


def test_try_insert_plot_result_multi_data_images():
    from plugin.scripting import viz

    img1 = {"__wa_payload__": "image", "format": "png", "data": b"1"}
    img2 = {"__wa_payload__": "image", "format": "png", "data": b"2"}
    multi = {"__wa_payload__": "multi_data", "items": [img1, img2]}
    ctx = MagicMock()
    doc = MagicMock()

    with (
        patch("plugin.scripting.viz.is_calc", return_value=True),
        patch("plugin.calc.python.image_egress.insert_image_result_on_sheet") as mock_sheet,
        patch.object(viz, "insert_image_payload_for_doc", wraps=viz.insert_image_payload_for_doc) as mock_insert,
    ):
        res = viz.try_insert_plot_result(ctx, doc, multi)

    assert res is True
    assert mock_insert.call_count == 2
    assert mock_sheet.call_count == 2


def test_try_insert_plot_result_mixed_multi_data_is_not_a_plot():
    from plugin.scripting import viz

    img1 = {"__wa_payload__": "image", "format": "png", "data": b"1"}
    not_img = {"plain": "dict"}
    multi = {"__wa_payload__": "multi_data", "items": [img1, not_img]}
    ctx = MagicMock()
    doc = MagicMock()

    with (
        patch("plugin.scripting.viz.is_calc", return_value=True),
        patch("plugin.calc.python.image_egress.insert_image_result_on_sheet") as mock_sheet,
        patch.object(viz, "insert_image_payload_for_doc", wraps=viz.insert_image_payload_for_doc) as mock_insert,
    ):
        res = viz.try_insert_plot_result(ctx, doc, multi)

    assert res is False
    mock_insert.assert_not_called()
    mock_sheet.assert_not_called()


def test_supports_viz_manual():
    assert supports_viz_manual(None) is False

    doc_writer = MagicMock()
    doc_writer.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    assert supports_viz_manual(doc_writer) is True

    doc_calc = MagicMock()
    doc_calc.supportsService.side_effect = lambda s: s == "com.sun.star.sheet.SpreadsheetDocument"
    assert supports_viz_manual(doc_calc) is True

    doc_other = MagicMock()
    doc_other.supportsService.return_value = False
    assert supports_viz_manual(doc_other) is False
