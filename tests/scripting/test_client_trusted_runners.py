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
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

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
    assert kwargs["session_id"] == session_prefix
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


def test_run_harper_check_pumps_ui_after_start_and_heartbeat(ctx) -> None:
    pump_calls: list[object] = []

    def _record_pump(c: object) -> None:
        pump_calls.append(c)

    def _fake_in_process(text, config_dir, *, bcp47="en-US", heartbeat_fn=None):
        if heartbeat_fn is not None:
            heartbeat_fn({"message": "Downloading harper-ls v2.7.0…"})
        return {"errors": []}

    with (
        patch("plugin.writer.locale.grammar_obs.emit_harper_worker_status") as mock_emit,
        patch("plugin.scripting.client._pump_grammar_status_ui", side_effect=_record_pump),
        patch("plugin.scripting.venv.harper.run_harper_check", side_effect=_fake_in_process) as mock_run,
        patch("plugin.scripting.client.run_trusted_worker_action") as mock_trusted,
    ):
        result = client.run_harper_check(ctx, "They is here.", "/tmp/cfg", bcp47="en-US")

    assert result == {"errors": []}
    mock_trusted.assert_not_called()
    mock_run.assert_called_once()
    assert mock_emit.call_args_list[0].args == ("They is here.", "Starting Harper…")
    assert mock_emit.call_args_list[1].args == ("They is here.", "Downloading harper-ls v2.7.0…")
    assert pump_calls == [ctx, ctx]


def test_run_harper_check_heartbeat_skips_empty_message(ctx) -> None:
    def _fake_in_process(text, config_dir, *, bcp47="en-US", heartbeat_fn=None):
        if heartbeat_fn is not None:
            heartbeat_fn({"message": "   "})
        return {"errors": []}

    with (
        patch("plugin.writer.locale.grammar_obs.emit_harper_worker_status") as mock_emit,
        patch("plugin.scripting.client._pump_grammar_status_ui") as mock_pump,
        patch("plugin.scripting.venv.harper.run_harper_check", side_effect=_fake_in_process),
        patch("plugin.scripting.client.run_trusted_worker_action") as mock_trusted,
    ):
        client.run_harper_check(ctx, "Hi.", "/tmp/cfg")

    mock_trusted.assert_not_called()
    mock_emit.assert_called_once_with("Hi.", "Starting Harper…")
    # Start pump once; empty heartbeat must not emit or pump again
    assert mock_pump.call_count == 1


def test_run_harper_check_does_not_use_trusted_worker(ctx) -> None:
    with (
        patch("plugin.writer.locale.grammar_obs.emit_harper_worker_status"),
        patch("plugin.scripting.client._pump_grammar_status_ui"),
        patch("plugin.scripting.venv.harper.run_harper_check", return_value={"errors": []}) as mock_in_process,
        patch("plugin.scripting.client.run_trusted_worker_action") as mock_trusted,
    ):
        client.run_harper_check(ctx, "Hi.", "/tmp/cfg")

    mock_trusted.assert_not_called()
    mock_in_process.assert_called_once()
