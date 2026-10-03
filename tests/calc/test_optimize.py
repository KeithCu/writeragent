# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc optimize_data threading."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.optimize import OptimizeDataTool


@pytest.fixture
def calc_ctx():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc_type = "calc"
    return ctx


@patch("plugin.scripting.optimize.insert_optimize_result_into_calc")
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.scripting.optimize.run_trusted_optimize")
def test_optimize_data_keeps_ipc_off_main_thread(mock_run_trusted, mock_main_thread, mock_insert, calc_ctx):
    inside = {"flag": False}

    def exec_main(fn, *args, **kwargs):
        inside["flag"] = True
        try:
            return fn(*args, **kwargs)
        finally:
            inside["flag"] = False

    def run(*_args, **_kwargs):
        assert not inside["flag"], "optimize IPC must not run inside execute_on_main_thread"
        return {"status": "ok", "helper": "linear_programming"}

    def write(*_args, **_kwargs):
        assert inside["flag"], "sheet write must run on the main thread"

    mock_main_thread.side_effect = exec_main
    mock_run_trusted.side_effect = run
    mock_insert.side_effect = write

    tool = OptimizeDataTool()
    result = tool.execute(
        calc_ctx,
        helper="linear_programming",
        data_range="Sheet1.A1:D20",
        output_range="Sheet1.F1",
    )

    assert result["status"] == "ok"
    mock_run_trusted.assert_called_once()
    mock_insert.assert_called_once()
    mock_main_thread.assert_called_once()
