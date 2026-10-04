# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted viz helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from plugin.scripting.viz import get_viz_script_templates, parse_viz_script_header, run_viz


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
        patch("plugin.scripting.viz.calc_tool_context", return_value=MagicMock()),
        patch("plugin.scripting.viz._resolve_python_data", return_value=([["date", "Value"], ["2024-01-01", 1.0]], None)),
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


def test_insert_viz_result_into_doc_inserts_even_if_stopped() -> None:
    """Document mutation should proceed even if stopped; do not abort after plot is generated."""
    from plugin.scripting.viz import insert_viz_result_into_doc

    class MockCtx:
        def stop_checker(self):
            return True

    doc = MagicMock()
    ctx = MockCtx()
    payload = {"__wa_payload__": "image", "format": "png", "data": b"x"}
    result = {"status": "ok", "image": payload}

    with patch("plugin.scripting.viz.insert_image_payload_for_doc") as mock_insert:
        res = insert_viz_result_into_doc(ctx, doc, result)
        assert res == 1
        mock_insert.assert_called_once_with(ctx, doc, payload, title="Plot")
