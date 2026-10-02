# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Eval dashboard: lazy tests.eval_runner import (Pyright must not follow it)."""

from __future__ import annotations

import sys
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.eval_dashboard_ui import EvalRunListener


def test_importing_eval_dashboard_ui_does_not_load_eval_runner() -> None:
    sys.modules.pop("tests.eval_runner", None)
    import plugin.chatbot.eval_dashboard_ui as mod

    assert mod is not None
    assert "tests.eval_runner" not in sys.modules


def test_run_suite_imports_eval_runner_at_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    # Posted status runs inline so the worker can finish the dialog update.
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    sys.modules.pop("tests.eval_runner", None)
    fake_mod = MagicMock()
    fake_mod.run_benchmark_suite.return_value = {
        "passed": 1,
        "failed": 0,
        "total_cost": 0.0,
        "results": [{"status": "ok", "name": "t", "latency": 0.1}],
    }
    sys.modules["tests.eval_runner"] = fake_mod

    dialog, _controls = _dialog()
    listener = EvalRunListener(MagicMock(), dialog)
    with patch("plugin.framework.uno_context.process_events_to_idle"), patch(
        "plugin.chatbot.eval_dashboard_ui.get_active_document", return_value=MagicMock()
    ):
        listener.run_suite()
        assert listener._job is not None
        listener._job.join(timeout=2)
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


def test_run_returns_without_waiting_on_fake_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    sys.modules.pop("tests.eval_runner", None)
    release = threading.Event()
    entered = threading.Event()

    def _block(*_args: object, **_kwargs: object) -> dict[str, object]:
        entered.set()
        assert release.wait(timeout=2)
        return {"passed": 0, "failed": 0, "total_cost": 0.0, "results": []}

    fake_mod = MagicMock()
    fake_mod.run_benchmark_suite.side_effect = _block
    sys.modules["tests.eval_runner"] = fake_mod
    dialog, controls = _dialog()
    listener = EvalRunListener(MagicMock(), dialog)
    try:
        with patch("plugin.framework.uno_context.process_events_to_idle"), patch(
            "plugin.chatbot.eval_dashboard_ui.get_active_document", return_value=MagicMock()
        ):
            started = time.monotonic()
            listener.run_suite()
            elapsed = time.monotonic() - started
            assert elapsed < 0.5
            assert entered.wait(timeout=1)
            assert controls["status"].text == "Running..."
            release.set()
            assert listener._job is not None
            listener._job.join(timeout=2)
    finally:
        release.set()
        sys.modules.pop("tests.eval_runner", None)
    assert controls["status"].text == "Finished"


def test_raised_suite_sets_finished_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    sys.modules.pop("tests.eval_runner", None)

    def _boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("suite blew up before the summary")

    fake_mod = MagicMock()
    fake_mod.run_benchmark_suite.side_effect = _boom
    sys.modules["tests.eval_runner"] = fake_mod
    dialog, controls = _dialog()
    listener = EvalRunListener(MagicMock(), dialog)
    try:
        with patch("plugin.framework.uno_context.process_events_to_idle"), patch(
            "plugin.chatbot.eval_dashboard_ui.get_active_document", return_value=MagicMock()
        ):
            listener.run_suite()
            assert listener._job is not None
            listener._job.join(timeout=2)
    finally:
        sys.modules.pop("tests.eval_runner", None)
    assert controls["status"].text == "Finished"
    assert "suite blew up before the summary" in controls["log_area"].text


def test_tool_execution_hops_to_the_main_thread_from_the_suite_worker() -> None:
    from tests.eval_runner import _on_main_thread

    ran: list[str] = []

    def _marker() -> str:
        ran.append("marker")
        return "ok"

    def _hop(fn: Any, *a: Any, timeout: int = 120, **k: Any) -> Any:
        assert timeout == 120
        return fn(*a, **k)

    with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=_hop) as hop:
        with patch("plugin.framework.thread_guard.on_main_thread", return_value=True):
            assert _on_main_thread(_marker) == "ok"
        hop.assert_not_called()

        seen: list[bool] = []

        def _from_worker() -> None:
            with patch("plugin.framework.thread_guard.on_main_thread", return_value=False):
                seen.append(_on_main_thread(_marker) == "ok")

        worker = threading.Thread(target=_from_worker)
        worker.start()
        worker.join(timeout=2)

    assert seen == [True]
    hop.assert_called_once()
    assert ran == ["marker", "marker"]


class _Ctrl:
    def __init__(self) -> None:
        self.text = ""

    def getText(self) -> str:
        return self.text

    def getState(self) -> int:
        return 0

    def setText(self, text: str) -> None:
        self.text = text


def _dialog() -> tuple[MagicMock, dict[str, _Ctrl]]:
    controls: dict[str, _Ctrl] = {}

    def _control(name: str) -> _Ctrl:
        ctrl = controls.get(name)
        if ctrl is None:
            ctrl = _Ctrl()
            if name == "models":
                ctrl.text = "model"
            controls[name] = ctrl
        return ctrl

    dialog = MagicMock()
    dialog.getControl.side_effect = _control
    return dialog, controls
