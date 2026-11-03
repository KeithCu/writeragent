from __future__ import annotations
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc trusted analysis runner."""


from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import DocumentDisposedError
from plugin.scripting.helper_domain import run_domain


def test_run_trusted_analysis_sheet_disposal_propagates():
    class DisposedException(Exception):
        pass

    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch("plugin.calc.calc_addin_data._resolve_python_data", return_value=([[1]], None)),
        patch("plugin.calc.bridge.CalcBridge") as mock_bridge,
        patch("plugin.scripting.client.run_analysis") as mock_run_analysis,
    ):
        mock_bridge.return_value.get_active_sheet.side_effect = DisposedException("disposed")
        with pytest.raises(DocumentDisposedError):
            run_domain(
                "analysis",
                MagicMock(),
                MagicMock(),
                helper="describe_data",
                data=[[1]],
            )
        mock_run_analysis.assert_not_called()


def test_run_trusted_analysis_sheet_name_failure_is_best_effort():
    with (
        patch("plugin.framework.queue_executor.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch("plugin.calc.calc_addin_data._resolve_python_data", return_value=([[1]], None)),
        patch("plugin.calc.bridge.CalcBridge") as mock_bridge,
        patch("plugin.scripting.client.run_analysis", return_value={"status": "ok"}) as mock_run_analysis,
    ):
        mock_bridge.return_value.get_active_sheet.side_effect = RuntimeError("no sheet")
        res = run_domain(
            "analysis",
            MagicMock(),
            MagicMock(),
            helper="describe_data",
            data=[[1]],
        )
        assert res == {"status": "ok"}
        mock_run_analysis.assert_called_once()


def test_run_trusted_analysis_dummy_thread_routes_to_main_thread():
    """Even when is_current() is True (e.g. Dummy-N thread), force_queue routes via main.call."""
    called = {"call": False}

    def fake_execute(fn, *a, **kw):
        called["call"] = True
        return fn(*a, **kw)

    with (
        patch("plugin.framework.queue_executor.main.is_current", return_value=True),
        patch("plugin.framework.queue_executor.main.call", side_effect=fake_execute),
        patch("plugin.calc.calc_addin_data._resolve_python_data", return_value=([[1]], None)),
        patch("plugin.calc.bridge.CalcBridge") as mock_bridge,
        patch("plugin.scripting.client.run_analysis", return_value={"status": "ok"}),
    ):
        mock_bridge.return_value.get_active_sheet.return_value.getName.return_value = "Sheet1"
        res = run_domain(
            "analysis",
            MagicMock(),
            MagicMock(),
            helper="describe_data",
            data=[[1]],
        )
        assert res == {"status": "ok"}
        assert called["call"] is True, "Dummy-N thread sheet read must route to main.call"
