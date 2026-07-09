# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.client.run_analysis."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.client import run_analysis
from plugin.framework.errors import ToolExecutionError
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@pytest.fixture
def ctx():
    return MagicMock()


def test_run_analysis_happy_path(ctx):
    worker_result = {
        "status": "ok",
        "result": {
            "status": "ok",
            "helper": "describe_data",
            "metrics": {"row_count": 3},
        },
    }
    spec = {"helper": "describe_data"}
    data = [["A"], [1], [2]]
    context = {"sheet_name": "Sheet1"}

    with (
        patch("plugin.scripting.client.configured_python_exec_timeout", return_value=30),
        patch("plugin.scripting.client.run_code_in_user_venv", return_value=worker_result) as mock_run,
     ):
        result = run_analysis(ctx, spec, data, context=context)

    assert result["helper"] == "describe_data"
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0] is ctx
    assert kwargs["action"] == "run_trusted_action"
    assert kwargs["data"]["domain"] == "analysis"
    assert kwargs["data"]["helper"] == spec["helper"]
    assert kwargs["data"]["data_range"] == data
    assert kwargs["data"]["context"] == context
    assert kwargs["session_id"] == "writeragent:analysis"
    assert kwargs["timeout_sec"] == 30


def test_run_analysis_worker_error(ctx):
    with (
        patch("plugin.scripting.client.configured_python_exec_timeout", return_value=10),
        patch(
            "plugin.scripting.client.run_code_in_user_venv",
            return_value={"status": "error", "message": "boom"},
        ),
    ):
        with pytest.raises(ToolExecutionError, match="boom"):
            run_analysis(ctx, "describe_data", [])


def test_run_analysis_malformed_result(ctx):
    with (
        patch("plugin.scripting.client.configured_python_exec_timeout", return_value=10),
        patch(
            "plugin.scripting.client.run_code_in_user_venv",
            return_value={"status": "ok", "result": "not-a-dict"},
        ),
    ):
        with pytest.raises(ToolExecutionError, match="unexpected result"):
            run_analysis(ctx, "describe_data", [])
