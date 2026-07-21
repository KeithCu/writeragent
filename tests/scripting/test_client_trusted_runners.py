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


