# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Eval dashboard: lazy tests.eval_runner import (Pyright must not follow it)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from plugin.chatbot.eval_dashboard_ui import EvalRunListener


def test_importing_eval_dashboard_ui_does_not_load_eval_runner() -> None:
    sys.modules.pop("tests.eval_runner", None)
    import plugin.chatbot.eval_dashboard_ui as mod

    assert mod is not None
    assert "tests.eval_runner" not in sys.modules


def test_run_suite_imports_eval_runner_at_runtime() -> None:
    sys.modules.pop("tests.eval_runner", None)
    fake_mod = MagicMock()
    fake_mod.run_benchmark_suite.return_value = {
        "passed": 1,
        "failed": 0,
        "total_cost": 0.0,
        "results": [{"status": "ok", "name": "t", "latency": 0.1}],
    }
    sys.modules["tests.eval_runner"] = fake_mod

    class _Ctrl:
        def __init__(self) -> None:
            self.text = ""

        def getText(self) -> str:
            return "model"

        def getState(self) -> int:
            return 0

        def setText(self, text: str) -> None:
            self.text = text

    dialog = MagicMock()
    dialog.getControl.side_effect = lambda _name: _Ctrl()
    listener = EvalRunListener(MagicMock(), dialog)
    with patch("plugin.framework.uno_context.process_events_to_idle"), patch(
        "plugin.chatbot.eval_dashboard_ui.get_active_document", return_value=MagicMock()
    ):
        listener.run_suite()
    fake_mod.run_benchmark_suite.assert_called_once()
    sys.modules.pop("tests.eval_runner", None)


def test_run_suite_reports_when_eval_runner_is_missing() -> None:
    """Release builds omit tests/. Run must say so instead of raising ImportError."""
    saved = sys.modules.get("tests.eval_runner")
    sys.modules["tests.eval_runner"] = None  # type: ignore[assignment]

    class _Ctrl:
        def __init__(self) -> None:
            self.text = ""

        def setText(self, text: str) -> None:
            self.text = text

    log_area = _Ctrl()
    status = _Ctrl()
    dialog = MagicMock()

    def _control(name: str) -> _Ctrl:
        if name == "log_area":
            return log_area
        if name == "status":
            return status
        return _Ctrl()

    dialog.getControl.side_effect = _control
    listener = EvalRunListener(MagicMock(), dialog)
    try:
        listener.run_suite()
    finally:
        if saved is None:
            sys.modules.pop("tests.eval_runner", None)
        else:
            sys.modules["tests.eval_runner"] = saved
    assert status.text == "Unavailable"
    assert "not in this extension build" in log_area.text
    assert "run_eval.py" in log_area.text
