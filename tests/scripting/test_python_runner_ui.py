# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script native dialog UI."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from plugin.scripting import python_runner_ui as ui


def test_report_run_outcome_sets_status_via_set_control_text():
    ctx = MagicMock()
    lbl = MagicMock()
    outcome = {"ok": True, "status_ok_text": "Script executed successfully. (took 0.1s)"}

    with patch.object(ui, "set_control_text") as mock_set_text:
        ui._report_run_outcome(ctx, lbl, outcome)

    mock_set_text.assert_called_once_with(lbl, "Script executed successfully. (took 0.1s)")


def test_report_script_dialog_error_shows_the_exception():
    ctx = MagicMock()
    dlg = MagicMock()
    lbl = MagicMock()
    dlg.getControl.return_value = lbl
    with (
        patch.object(ui, "set_control_text") as mock_set_text,
        patch.object(ui, "msgbox") as mock_box,
        patch("plugin.framework.errors.is_disposed_exception", return_value=False),
    ):
        ui._report_script_dialog_error(ctx, dlg, RuntimeError("disk full"), "Save")
    mock_set_text.assert_called_once_with(lbl, "disk full")
    mock_box.assert_not_called()


def test_report_script_dialog_error_disposed_document():
    ctx = MagicMock()
    dlg = MagicMock()
    dlg.getControl.side_effect = RuntimeError("no dialog")
    with (
        patch.object(ui, "msgbox") as mock_box,
        patch("plugin.framework.errors.is_disposed_exception", return_value=True),
    ):
        ui._report_script_dialog_error(ctx, dlg, RuntimeError("disposed"), "Delete")
    mock_box.assert_called_once()
    assert "no longer open" in mock_box.call_args.args[2]


def test_native_dialog_window_closing_does_not_dispose():
    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._closed = False
    dlg = MagicMock()
    inst._dlg = dlg
    inst.close(toolkit_teardown=True)
    dlg.dispose.assert_not_called()
    assert inst._closed is True
    assert inst._dlg is None
    inst.close(toolkit_teardown=True)
    dlg.dispose.assert_not_called()


def test_native_dialog_close_button_disposes_once():
    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._closed = False
    dlg = MagicMock()
    inst._dlg = dlg
    inst.close()
    dlg.dispose.assert_called_once()
    inst.close()
    dlg.dispose.assert_called_once()


def test_start_native_script_run_returns_before_venv_wait():
    """The caller schedules work; the venv IPC wait is not on this thread."""
    ctx = MagicMock()
    doc = MagicMock()
    completed = []
    scheduled = {}

    def _capture(func, *args, **kwargs):
        scheduled["func"] = func
        return MagicMock()

    with (
        patch.object(ui, "run_in_background", side_effect=_capture),
        patch("plugin.scripting.python_runner._run_prepared_rps") as mock_run,
    ):
        ui.start_native_script_run(ctx, doc, "result = 1", on_complete=completed.append)
    mock_run.assert_not_called()
    assert completed == []
    assert callable(scheduled["func"])


@contextmanager
def _deliver_posted_native_run():
    """Run the main-thread turn that delivers ``on_complete``.

    What was wrong: these tests inline the native-run worker and then read
    the listener immediately. ``seen`` stayed ``[]`` on CI.
    How it happened: LibreOffice has already created AsyncCallback, so
    ``post()`` enqueues (``marshal route=post_enqueue``) even on the logical
    main thread. ``execute()`` still inlines, which is why prepare / venv /
    finish ran and only the completion post was missing.
    Why this works: mark AsyncCallback ready (the CI route, not the
    no-service inline) and drain that queue from the poke. The product still
    posts the outcome; the harness plays the UI turn that would run it.
    """
    from plugin.framework import queue_executor as qe

    executor = qe.default_executor
    saved_init = executor._initialized
    saved_svc = executor._async_callback_service
    saved_cb = executor._callback_instance
    saved_poke = qe._test_poke_handler
    draining = False

    def _poke(ex: qe.QueueExecutor) -> None:
        nonlocal draining
        # process_queue pokes again while items remain. The flag keeps that
        # re-poke from nesting; the loop drains the rest.
        if draining:
            return
        draining = True
        try:
            while ex.pending_work_count():
                ex.process_queue()
        finally:
            draining = False

    # CI's real service takes this branch. A MagicMock is enough: the poke
    # handler replaces addCallback, and execute() still inlines on this thread.
    executor._initialized = True
    executor._async_callback_service = MagicMock()
    executor._callback_instance = MagicMock()
    qe.set_test_poke_handler(_poke)
    try:
        yield
    finally:
        _poke(executor)
        qe.set_test_poke_handler(saved_poke)
        executor._initialized = saved_init
        executor._async_callback_service = saved_svc
        executor._callback_instance = saved_cb


def test_start_native_script_run_worker_splits_main_and_venv():
    """Prep and insert run through the main-thread marshal; venv wait is in between."""
    ctx = MagicMock()
    doc = MagicMock()
    order = []
    seen = []

    def _prepare(passed_ctx, passed_doc, code, *, data_range=None):
        order.append("prepare")
        assert passed_ctx is ctx
        assert passed_doc is doc
        assert data_range is None
        return {
            "early_outcome": None,
            "ctx": passed_ctx,
            "doc": passed_doc,
            "code": code,
            "t0": 0.0,
            "exec_code": code,
            "py_data": None,
            "bindings": None,
            "session_id": "rps:doc",
        }

    def _run(prepared):
        order.append("venv")
        assert prepared["exec_code"] == "result = 1"
        return {"status": "ok", "result": 1, "stdout": ""}

    def _finish(prepared, response):
        order.append("finish")
        assert response["result"] == 1
        return {"ok": True, "status_ok_text": "done"}

    def _inline(func, *args, **kwargs):
        func()
        return MagicMock()

    with (
        _deliver_posted_native_run(),
        patch.object(ui, "run_in_background", side_effect=_inline),
        patch("plugin.scripting.python_runner._prepare_rps_execution", side_effect=_prepare),
        patch("plugin.scripting.python_runner._run_prepared_rps", side_effect=_run),
        patch("plugin.scripting.python_runner._finish_rps_execution", side_effect=_finish),
    ):
        ui.start_native_script_run(ctx, doc, "result = 1", on_complete=seen.append)

    assert order == ["prepare", "venv", "finish"]
    assert seen == [{"ok": True, "status_ok_text": "done"}]


def test_start_native_script_run_reports_venv_failure():
    ctx = MagicMock()
    doc = MagicMock()
    seen = []

    def _prepare(passed_ctx, passed_doc, code, *, data_range=None):
        return {
            "early_outcome": None,
            "ctx": passed_ctx,
            "doc": passed_doc,
            "code": code,
            "t0": 0.0,
            "exec_code": code,
            "py_data": None,
            "bindings": None,
            "session_id": None,
        }

    def _inline(func, *args, **kwargs):
        func()
        return MagicMock()

    with (
        _deliver_posted_native_run(),
        patch.object(ui, "run_in_background", side_effect=_inline),
        patch("plugin.scripting.python_runner._prepare_rps_execution", side_effect=_prepare),
        patch("plugin.scripting.python_runner._run_prepared_rps", side_effect=RuntimeError("venv down")),
        patch("plugin.scripting.python_runner._finish_rps_execution") as mock_finish,
    ):
        ui.start_native_script_run(ctx, doc, "result = 1", on_complete=seen.append)

    mock_finish.assert_not_called()
    assert seen and seen[0]["ok"] is False
    assert "venv down" in seen[0]["message"]


def test_report_run_outcome_error_skips_status_label():
    ctx = MagicMock()
    lbl = MagicMock()
    outcome = {"ok": False, "message": "boom"}

    with patch.object(ui, "msgbox") as mock_msgbox:
        with patch.object(ui, "set_control_text") as mock_set_text:
            ui._report_run_outcome(ctx, lbl, outcome)

    mock_msgbox.assert_called_once()
    mock_set_text.assert_not_called()
