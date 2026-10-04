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


def test_show_loads_eval_dialog_with_ctx() -> None:
    from plugin.chatbot.eval_dashboard_ui import EvalDashboard

    ctx = MagicMock()
    dash = EvalDashboard(ctx)
    dlg = MagicMock()
    with patch("plugin.chatbot.eval_dashboard_ui.load_writeragent_dialog", return_value=dlg) as mock_load, \
         patch.object(dash, "_populate"):
        dash.show()
    mock_load.assert_called_once_with("EvalDialog", ctx)
    dlg.execute.assert_called_once()
    dlg.dispose.assert_called_once()
    assert dash._closed is True
    assert dash._dlg is None


def test_show_skips_execute_when_dialog_missing() -> None:
    from plugin.chatbot.eval_dashboard_ui import EvalDashboard

    dash = EvalDashboard(MagicMock())
    with patch("plugin.chatbot.eval_dashboard_ui.load_writeragent_dialog", return_value=None) as mock_load, \
         patch.object(dash, "_populate") as mock_populate:
        dash.show()
    mock_load.assert_called_once()
    mock_populate.assert_not_called()
    assert dash._closed is True
    assert dash._dlg is None


def test_eval_populate_does_not_fetch_models_on_the_caller() -> None:
    from plugin.chatbot.eval_dashboard_ui import EvalDashboard

    dash = EvalDashboard(MagicMock())
    dialog = MagicMock()
    dash._dlg = dialog
    started: list[Any] = []
    posted: list[Any] = []

    def _capture_worker(fn: Any, **_kwargs: Any) -> None:
        started.append(fn)

    def _capture_post(fn: Any) -> None:
        posted.append(fn)

    with patch("plugin.chatbot.eval_dashboard_ui.populate_combobox_with_lru") as mock_pop, \
         patch("plugin.chatbot.eval_dashboard_ui.get_config_str", return_value="http://localhost:11434"), \
         patch("plugin.chatbot.eval_dashboard_ui.get_text_model", return_value="llama3"), \
         patch("plugin.framework.worker_pool.run_in_background", side_effect=_capture_worker), \
         patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_capture_post), \
         patch("plugin.framework.client.model_fetcher.fetch_available_models", return_value=["llama3"]) as mock_fetch:
        dash._populate()
        mock_fetch.assert_not_called()
        assert mock_pop.call_args.kwargs.get("skip_remote_fetch") is True
        assert len(started) == 1
        started[0]()
        mock_fetch.assert_called_once_with("http://localhost:11434")
        assert len(posted) == 1
        posted[0]()
        assert mock_pop.call_count == 2
        assert mock_pop.call_args.kwargs.get("remote_models") == ["llama3"]
        dash._closed = True
        posted[0]()
        assert mock_pop.call_count == 2


def test_opening_eval_does_not_fetch_a_catalog_already_in_memory() -> None:
    """A text-model list already cached is painted without fetch_available_models."""
    from plugin.chatbot.eval_dashboard_ui import EvalDashboard
    from plugin.framework.client import model_fetcher as cfg

    endpoint = "http://127.0.0.1:11434"
    cfg.clear_settings_catalog_cache(endpoint)
    try:
        with patch("plugin.framework.client.requests.sync_request", return_value={"data": [{"id": "llama3"}]}), \
             patch("plugin.framework.client.model_fetcher.get_config", return_value=""):
            cfg.fetch_available_models(endpoint)
        dash = EvalDashboard(MagicMock())
        dialog = MagicMock()
        dash._dlg = dialog
        with patch("plugin.chatbot.eval_dashboard_ui.populate_combobox_with_lru") as mock_pop, \
             patch("plugin.chatbot.eval_dashboard_ui.get_config_str", return_value=endpoint), \
             patch("plugin.chatbot.eval_dashboard_ui.get_text_model", return_value="llama3"), \
             patch("plugin.framework.client.model_fetcher.fetch_available_models") as mock_fetch, \
             patch("plugin.framework.client.requests.sync_request") as mock_sync:
            dash._populate()
        mock_fetch.assert_not_called()
        mock_sync.assert_not_called()
        assert mock_pop.call_args_list[0].kwargs.get("skip_remote_fetch") is True
        assert mock_pop.call_args.kwargs.get("remote_models") == ["llama3"]
    finally:
        cfg.clear_settings_catalog_cache(endpoint)


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
    # What was wrong: joining the suite worker was enough to see "Finished".
    # post() no longer runs inline on a tagged background task, so the worker
    # only queues _show_summary. The line stays "Running..." until this thread
    # drains that post. Force-marshal keeps the post instead of dropping it
    # when AsyncCallback is missing.
    from plugin.framework import queue_executor as qe

    qe.set_force_marshal_mode(True)
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
            assert controls["status"].text == "Running..."
            _drain_posted_main_thread()
    finally:
        release.set()
        qe.set_force_marshal_mode(False)
        _drain_posted_main_thread()
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
    from plugin.framework import queue_executor as qe

    qe.set_force_marshal_mode(True)
    try:
        with patch("plugin.framework.uno_context.process_events_to_idle"), patch(
            "plugin.chatbot.eval_dashboard_ui.get_active_document", return_value=MagicMock()
        ):
            listener.run_suite()
            assert listener._job is not None
            listener._job.join(timeout=2)
            # Same contract as a clean summary: the worker posts _show_failure
            # and leaves the line on Running... until the main thread paints it.
            assert controls["status"].text == "Running..."
            _drain_posted_main_thread()
    finally:
        qe.set_force_marshal_mode(False)
        _drain_posted_main_thread()
        sys.modules.pop("tests.eval_runner", None)
    assert controls["status"].text == "Finished"
    assert "suite blew up before the summary" in controls["log_area"].text


def test_tool_execution_hops_to_the_main_thread_from_the_suite_worker() -> None:
    # What was wrong: tests.eval_runner imports dev scripts (scripts.lib.pricing)
    # which are not present in stripped release verification trees.
    # Why: skip when running against stripped release bundle.
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("scripts/ not in stripped release tree")
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


def test_populate_locks_endpoint_so_edits_are_not_ignored() -> None:
    """The suite reads the saved endpoint. The dialog field must not look editable."""
    # What was wrong: extension/ directory is not at root in stripped release bundle.
    # Why: skip when running against stripped release bundle.
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("extension/ not in stripped release tree")
    from pathlib import Path

    from plugin.chatbot.eval_dashboard_ui import EvalDashboard

    xdl = Path("extension/Dialogs/EvalDialog.xdl").read_text(encoding="utf-8")
    endpoint_line = next(line for line in xdl.splitlines() if 'dlg:id="endpoint"' in line)
    assert 'dlg:readonly="true"' in endpoint_line

    dash = EvalDashboard(MagicMock())
    dialog = MagicMock()
    endpoint_model = MagicMock()
    endpoint = MagicMock()
    endpoint.getModel.return_value = endpoint_model

    def _control(name: str) -> Any:
        if name == "endpoint":
            return endpoint
        return MagicMock()

    dialog.getControl.side_effect = _control
    dash._dlg = dialog
    with patch("plugin.chatbot.eval_dashboard_ui.populate_combobox_with_lru"), \
         patch("plugin.chatbot.eval_dashboard_ui.get_config_str", return_value="http://127.0.0.1:11434"), \
         patch("plugin.chatbot.eval_dashboard_ui.get_text_model", return_value="llama3"), \
         patch("plugin.chatbot.eval_dashboard_ui.EvalDashboard._schedule_models_fetch"):
        dash._populate()
    assert endpoint_model.ReadOnly is True


def test_closed_dialog_skips_late_paint_and_clears_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close disposes the dialog while the suite can still post. Ignore that window."""
    from plugin.chatbot.eval_dashboard_ui import EvalDashboard

    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    dialog, controls = _dialog()
    dash = EvalDashboard(MagicMock())
    listener = EvalRunListener(MagicMock(), dialog, dash)
    summary = {
        "passed": 1,
        "failed": 0,
        "total_cost": 0.25,
        "results": [{"status": "OK", "name": "t", "latency": 0.1}],
    }
    listener.is_running = True
    listener._show_summary("model", summary)
    assert controls["status"].text == "Finished"
    assert "Benchmarks Complete" in controls["log_area"].text
    assert listener.is_running is False

    listener.is_running = True
    controls["status"].text = "Running..."
    controls["log_area"].text = "Starting...\n"
    dash._closed = True
    listener._show_summary("model", summary)
    listener._show_failure(RuntimeError("late failure"))
    listener._after_test({"status": "OK", "name": "late-test"})
    _drain_posted_main_thread()
    assert controls["status"].text == "Running..."
    assert controls["log_area"].text == "Starting...\n"
    assert "late-test" not in controls["log_area"].text
    assert listener.is_running is False


def test_disposed_dialog_summary_and_failure_clear_running() -> None:
    """A disposed window raises instead of painting. is_running still clears."""

    class DisposedException(Exception):
        pass

    dialog = MagicMock()
    dialog.getControl.side_effect = DisposedException("disposed")
    listener = EvalRunListener(MagicMock(), dialog)
    listener.is_running = True
    listener._show_summary(
        "model",
        {"passed": 0, "failed": 1, "total_cost": 0.0, "results": []},
    )
    assert listener.is_running is False

    listener.is_running = True
    listener._show_failure(RuntimeError("boom"))
    assert listener.is_running is False
    dialog.getControl.assert_called()


def _drain_posted_main_thread() -> None:
    """Run callbacks the eval worker posted for the main thread."""
    from plugin.framework.queue_executor import default_executor

    for _unused in range(8):
        if default_executor._work_queue.empty():
            return
        default_executor.process_queue()


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
