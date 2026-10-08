# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Contract tests for trusted client runners built via _make_spec_runner."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import ToolExecutionError
from plugin.scripting import client


_SPEC_RUNNERS = (
    ("run_analysis", "writeragent:analysis", "analysis", "ANALYSIS_ERROR"),
    ("run_viz", "writeragent:viz", "viz", "VIZ_ERROR"),
    ("run_symbolic", "writeragent:symbolic", "symbolic", "SYMBOLIC_ERROR"),
    ("run_units", "writeragent:units", "units", "UNITS_ERROR"),
    ("run_optimize", "writeragent:optimize", "optimize", "OPTIMIZE_ERROR"),
    ("run_forecast", "writeragent:forecast", "forecast", "FORECAST_ERROR"),
)


@pytest.fixture
def ctx():
    return MagicMock()


@pytest.mark.parametrize("runner_name,session_prefix,domain,error_code", _SPEC_RUNNERS)
def test_spec_runner_happy_path(ctx, runner_name, session_prefix, domain, error_code):
    runner = getattr(client, runner_name)
    worker_result = {"status": "ok", "helper": "demo"}
    spec = {"helper": "demo"}
    with (
        patch("plugin.scripting.client.configured_python_exec_timeout", return_value=30),
        patch("plugin.scripting.client.run_trusted_worker_action", return_value=worker_result) as mock_run,
    ):
        result = runner(ctx, spec, [], context={"sheet_name": "Sheet1"})

    assert result["helper"] == "demo"
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("session_id") is None
    assert kwargs["domain"] == domain
    assert kwargs["helper"] == spec["helper"]


@pytest.mark.parametrize("runner_name,session_prefix,domain,error_code", _SPEC_RUNNERS)
def test_spec_runner_worker_error(ctx, runner_name, session_prefix, domain, error_code):
    runner = getattr(client, runner_name)
    with (
        patch("plugin.scripting.client.configured_python_exec_timeout", return_value=10),
        patch(
            "plugin.scripting.client.run_trusted_worker_action",
            side_effect=ToolExecutionError("boom", code=error_code),
        ),
    ):
        with pytest.raises(ToolExecutionError, match="boom") as exc_info:
            runner(ctx, {"helper": "demo"}, [])

    assert exc_info.value.code == error_code


def test_languagetool_and_vale_use_config_limit_timeouts(ctx):
    from plugin.scripting.config_limits import (
        LANGUAGETOOL_WORKER_TIMEOUT_SEC,
        VALE_WORKER_TIMEOUT_SEC,
    )

    with patch("plugin.scripting.client.run_trusted_worker_action", return_value={"status": "ok"}) as mock_run:
        client.run_languagetool_check(ctx, "text", "en-US")
    assert mock_run.call_args.kwargs["timeout_sec"] == LANGUAGETOOL_WORKER_TIMEOUT_SEC

    with patch("plugin.scripting.client.run_trusted_worker_action", return_value={"status": "ok"}) as mock_run:
        client.run_vale_check(ctx, "text", "/cfg", "styles")
    assert mock_run.call_args.kwargs["timeout_sec"] == VALE_WORKER_TIMEOUT_SEC


def test_headers_false_survives_client_path(ctx):
    """headers=False must still be false when the worker parses the spec.

    The spec runner used to forward only helper and params. The worker rebuilt
    the spec without headers, and parse_trusted_spec defaulted headers=True,
    so the first data row was consumed as column names.
    """
    from plugin.scripting.calc_functions_common import FORECAST_HELPER_NAMES
    from plugin.scripting.venv.coerce import parse_trusted_spec
    from plugin.scripting.venv.trusted_dispatch import _packet_parts

    grid = [["Date", "Value"], ["2024-01-01", 1]]
    spec = {
        "helper": "forecast_time_series",
        "headers": False,
        "header_row": 1,
        "params": {"periods": 2},
    }
    with (
        patch("plugin.scripting.client.configured_python_exec_timeout", return_value=30),
        patch("plugin.scripting.trusted_rpc.run_code_in_user_venv") as mock_run,
    ):
        mock_run.return_value = {"status": "ok", "result": {"status": "ok", "helper": "forecast_time_series"}}
        result = client.run_forecast(ctx, spec, grid)

    assert result["helper"] == "forecast_time_series"
    payload = mock_run.call_args.kwargs["data"]
    assert payload["headers"] is False
    assert payload["header_row"] == 1
    assert payload["params"] == {"periods": 2}
    rebuilt, data_range, rebuilt_context = _packet_parts(payload)
    parsed = parse_trusted_spec(rebuilt, helper_names=FORECAST_HELPER_NAMES, context=rebuilt_context)
    assert not isinstance(parsed, dict)
    assert parsed[2] is False
    assert parsed[3] == 1
    assert data_range == grid


def test_spec_runner_omits_headers_when_unset(ctx):
    with (
        patch("plugin.scripting.client.configured_python_exec_timeout", return_value=30),
        patch("plugin.scripting.trusted_rpc.run_code_in_user_venv") as mock_run,
    ):
        mock_run.return_value = {"status": "ok", "result": {"status": "ok"}}
        client.run_optimize(ctx, {"helper": "optimize_portfolio"}, [])
    payload = mock_run.call_args.kwargs["data"]
    assert "headers" not in payload
    assert "header_row" not in payload


def test_run_text_analytics_string_spec_applies_sentiment_model(ctx):
    with (
        patch(
            "plugin.framework.config.get_config_dict",
            return_value={"text_analytics_sentiment_model": "xlm-roberta"},
        ),
        patch("plugin.scripting.client.run_trusted_worker_action", return_value={"status": "ok"}) as mock_run,
    ):
        client.run_text_analytics(ctx, "sentiment", "hi")
    assert mock_run.call_args.kwargs["helper"] == "sentiment"
    assert mock_run.call_args.kwargs["params"]["model"] == "xlm-roberta"


