# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.client.run_analysis."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.client import run_analysis


@pytest.fixture
def ctx():
    return MagicMock()


def test_run_analysis_happy_path(ctx):
    worker_result = {
        "status": "ok",
        "helper": "describe_data",
        "metrics": {"row_count": 3},
    }
    spec = {"helper": "describe_data"}
    data = [["A"], [1], [2]]
    context = {"sheet_name": "Sheet1"}

    with (
        patch("plugin.scripting.client.python_exec_timeout", return_value=30),
        patch("plugin.scripting.client.run_trusted_worker_action", return_value=worker_result) as mock_run,
    ):
        result = run_analysis(ctx, spec, data, context=context)

    assert result is worker_result
    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs["domain"] == "analysis"
    assert kwargs["helper"] == spec["helper"]
    assert kwargs["data_range"] == data
    assert kwargs["context"] == context
    assert kwargs.get("session_id") is None
    assert kwargs["timeout_sec"] == 30


