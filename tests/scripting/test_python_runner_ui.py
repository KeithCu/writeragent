# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script native dialog UI."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework import queue_executor as qe
from plugin.scripting import python_runner as pr
from plugin.scripting import python_runner_ui as ui
from plugin.scripting.document_scripts import DocumentScriptError, DocumentScriptErrorCode


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
    ):
        ui._report_script_dialog_error(ctx, dlg, RuntimeError("disk full"), "Save")
    mock_set_text.assert_called_once_with(lbl, "disk full")
    mock_box.assert_not_called()


def test_report_script_dialog_error_disposed_document():
    ctx = MagicMock()
    dlg = MagicMock()
    dlg.getControl.return_value = None

    class MockDisposedException(Exception):
        pass

    MockDisposedException.__name__ = "DisposedException"

    with patch.object(ui, "msgbox") as mock_box:
        ui._report_script_dialog_error(ctx, dlg, MockDisposedException("disposed"), "Delete")
    mock_box.assert_called_once()
    assert "no longer open" in mock_box.call_args.args[2]


def test_report_script_dialog_error_runtime_exception_live_doc_vs_none_doc():
    ctx = MagicMock()
    dlg = MagicMock()
    lbl = MagicMock()
    dlg.getControl.return_value = lbl

    class MockRuntimeException(Exception):
        pass

    MockRuntimeException.__name__ = "RuntimeException"

    class LiveDoc:
        def getImplementationName(self):
            return "SwXTextDocument"

    # Live doc shows the exception text
    with patch.object(ui, "set_control_text") as mock_set_text:
        ui._report_script_dialog_error(ctx, dlg, MockRuntimeException("live error"), "Save", doc=LiveDoc())
    mock_set_text.assert_called_once_with(lbl, "live error")

    # doc=None is "no longer open"
    with patch.object(ui, "set_control_text") as mock_set_text:
        ui._report_script_dialog_error(ctx, dlg, MockRuntimeException("missing doc"), "Save", doc=None)
    mock_set_text.assert_called_once()
    assert "no longer open" in mock_set_text.call_args.args[1]


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


def test_native_dialog_close_disposes_when_set_visible_raises():
    class DisposedException(Exception):
        pass

    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._closed = False
    dlg = MagicMock()
    dlg.setVisible.side_effect = DisposedException("Peer disposed")
    inst._dlg = dlg
    inst.close()
    dlg.dispose.assert_called_once()
    assert inst._closed is True
    assert inst._dlg is None


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
        patch.object(pr, "run_in_background", side_effect=_capture),
        patch("plugin.scripting.python_runner._run_prepared_rps") as mock_run,
    ):
        pr.start_native_script_run(ctx, doc, "result = 1", on_complete=completed.append)
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

    def _prepare(passed_ctx, passed_doc, code, *, data_range=None, pin_token=None):
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
            "py_data": None,
            "bindings": None,
            "session_id": "rps:doc",
        }

    def _run(prepared):
        order.append("venv")
        assert prepared["code"] == "result = 1" and prepared["bindings"] is None
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
        patch.object(pr, "run_in_background", side_effect=_inline),
        patch("plugin.scripting.python_runner._prepare_rps_execution", side_effect=_prepare),
        patch("plugin.scripting.python_runner._run_prepared_rps", side_effect=_run),
        patch("plugin.scripting.python_runner._finish_rps_execution", side_effect=_finish),
    ):
        pr.start_native_script_run(ctx, doc, "result = 1", on_complete=seen.append)

    assert order == ["prepare", "venv", "finish"]
    assert seen == [{"ok": True, "status_ok_text": "done"}]


def test_start_native_script_run_forwards_data_range():
    """Monaco's data-binding text is the range passed into prepare."""
    ctx = MagicMock()
    doc = MagicMock()
    seen = []

    def _prepare(passed_ctx, passed_doc, code, *, data_range=None, pin_token=None):
        assert passed_ctx is ctx
        assert passed_doc is doc
        assert code == "result = 1"
        assert data_range == "A1:B2"
        return {"early_outcome": {"ok": True, "status_ok_text": "bound"}}

    def _inline(func, *args, **kwargs):
        func()
        return MagicMock()

    with (
        _deliver_posted_native_run(),
        patch.object(pr, "run_in_background", side_effect=_inline),
        patch("plugin.scripting.python_runner._prepare_rps_execution", side_effect=_prepare),
        patch("plugin.scripting.python_runner._run_prepared_rps") as mock_run,
    ):
        pr.start_native_script_run(
            ctx, doc, "result = 1", on_complete=seen.append, data_range="A1:B2"
        )

    mock_run.assert_not_called()
    assert seen == [{"ok": True, "status_ok_text": "bound"}]


def test_start_native_script_run_reports_venv_failure():
    ctx = MagicMock()
    doc = MagicMock()
    seen = []

    def _prepare(passed_ctx, passed_doc, code, *, data_range=None, pin_token=None):
        return {
            "early_outcome": None,
            "ctx": passed_ctx,
            "doc": passed_doc,
            "code": code,
            "t0": 0.0,
            "py_data": None,
            "bindings": None,
            "session_id": None,
        }

    def _inline(func, *args, **kwargs):
        func()
        return MagicMock()

    with (
        _deliver_posted_native_run(),
        patch.object(pr, "run_in_background", side_effect=_inline),
        patch("plugin.scripting.python_runner._prepare_rps_execution", side_effect=_prepare),
        patch("plugin.scripting.python_runner._run_prepared_rps", side_effect=RuntimeError("venv down")),
        patch("plugin.scripting.python_runner._finish_rps_execution") as mock_finish,
    ):
        pr.start_native_script_run(ctx, doc, "result = 1", on_complete=seen.append)

    mock_finish.assert_not_called()
    assert seen and seen[0]["ok"] is False
    assert "venv down" in seen[0]["message"]


def test_start_native_script_run_scheduling_failure_raises_without_delivering():
    ctx = MagicMock()
    doc = MagicMock()
    seen = []

    with (
        _deliver_posted_native_run(),
        patch.object(pr, "run_in_background", side_effect=RuntimeError("thread pool full")),
    ):
        with pytest.raises(RuntimeError, match="thread pool full"):
            pr.start_native_script_run(ctx, doc, "result = 1", on_complete=seen.append)

    assert seen == []


def test_start_native_script_run_releases_pin_when_finish_is_not_run():
    ctx = MagicMock()
    doc = MagicMock()
    seen = []

    def _inline(func, *args, **kwargs):
        func()
        return MagicMock()

    prepared = {"early_outcome": None, "script_session_id": "doc:leak"}

    with (
        _deliver_posted_native_run(),
        patch.object(pr, "run_in_background", side_effect=_inline),
        patch("plugin.scripting.session_manager.pin_script_document", return_value="doc:leak"),
        patch(
            "plugin.framework.queue_executor.execute_on_main_thread",
            side_effect=[
                prepared,
                TimeoutError("Main-thread execution of _finish_rps_execution timed out after 30.0s"),
            ],
        ),
        patch("plugin.scripting.python_runner._run_prepared_rps", return_value={"status": "ok", "result": 1}),
        patch("plugin.scripting.session_manager.release_script_document") as mock_release,
    ):
        pr.start_native_script_run(ctx, doc, "result = 1", on_complete=seen.append)

    mock_release.assert_called_once_with("doc:leak")
    assert seen and seen[0]["ok"] is False
    assert "timed out" in seen[0]["message"]


def test_start_native_script_run_prepare_timeout_releases_pin():
    ctx = MagicMock()
    doc = MagicMock()
    seen = []

    def _inline(func, *args, **kwargs):
        func()
        return MagicMock()

    with (
        _deliver_posted_native_run(),
        patch.object(pr, "run_in_background", side_effect=_inline),
        patch("plugin.scripting.session_manager.pin_script_document", return_value="doc:tok"),
        patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=TimeoutError("prepare")),
        patch("plugin.scripting.session_manager.release_script_document") as mock_release,
    ):
        pr.start_native_script_run(ctx, doc, "result = 1", on_complete=seen.append)

    mock_release.assert_called_once_with("doc:tok")
    assert seen and seen[0]["ok"] is False


def test_start_native_script_run_nondict_prepare_delivers_error():
    ctx = MagicMock()
    doc = MagicMock()
    seen = []

    def _inline(func, *args, **kwargs):
        func()
        return MagicMock()

    with (
        _deliver_posted_native_run(),
        patch.object(pr, "run_in_background", side_effect=_inline),
        patch("plugin.scripting.session_manager.pin_script_document", return_value="doc:tok"),
        patch("plugin.framework.queue_executor.execute_on_main_thread", return_value=None),
        patch("plugin.scripting.session_manager.release_script_document") as mock_release,
    ):
        pr.start_native_script_run(ctx, doc, "result = 1", on_complete=seen.append)

    mock_release.assert_called_once_with("doc:tok")
    assert len(seen) == 1
    assert seen[0]["ok"] is False
    assert seen[0]["message"] == "Script preparation failed."



def test_report_run_outcome_shows_stdout_alongside_result():
    """A returned value is inserted; printed text still belongs in the Output box."""
    ctx = MagicMock()
    lbl = MagicMock()
    outcome = {
        "ok": True,
        "status_ok_text": "Script executed successfully.",
        "result": 1,
        "stdout": "printed line",
    }

    with (
        patch.object(ui, "msgbox") as mock_msgbox,
        patch.object(ui, "set_control_text") as mock_set_text,
    ):
        ui._report_run_outcome(ctx, lbl, outcome)

    mock_msgbox.assert_called_once()
    assert mock_msgbox.call_args.args[0] is ctx
    assert mock_msgbox.call_args.args[2] == "printed line"
    mock_set_text.assert_called_once_with(lbl, "Script executed successfully.")


def test_native_dialog_open_warms_venv_on_shared_pool():
    """Warmup is finite. It must not take a dedicated thread."""
    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._ctx = MagicMock()
    inst._doc = MagicMock()
    inst._modeless = False
    inst._closed = False
    inst._dlg = None
    dlg = MagicMock()
    calls: dict = {}

    def _capture(func, *args, **kwargs):
        calls["func"] = func
        calls["args"] = args
        calls["kwargs"] = kwargs
        return MagicMock()

    with (
        patch.object(ui, "load_writeragent_dialog_detail", return_value=(dlg, None)),
        patch.object(ui, "run_in_background", side_effect=_capture),
        patch.object(ui, "build_xdl_script_picker_state", return_value=([], {}, {})),
        patch.object(ui, "get_user_scripts", return_value={}),
        patch.object(inst, "_refresh_script_dropdown"),
        patch.object(inst, "_wire_listeners"),
    ):
        assert inst._open() is True

    assert calls["func"] is ui.warm_venv_worker
    assert calls["args"] == (inst._ctx,)
    assert calls["kwargs"].get("name") == "warm-venv-worker"
    assert calls["kwargs"].get("dedicated") is False


def test_report_run_outcome_error_skips_status_label():
    ctx = MagicMock()
    lbl = MagicMock()
    outcome = {"ok": False, "message": "boom"}

    with patch.object(ui, "msgbox") as mock_msgbox:
        with patch.object(ui, "set_control_text") as mock_set_text:
            ui._report_run_outcome(ctx, lbl, outcome)

    mock_msgbox.assert_called_once()
    mock_set_text.assert_not_called()

def test_save_current_script_fallback_collision():
    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._select_ctrl = MagicMock()
    inst._doc = MagicMock()
    inst._current_scripts = {}
    inst._script_origin_map = {}
    doc_err = DocumentScriptError(
        "Document is read-only or properties cannot be written.",
        DocumentScriptErrorCode.READONLY,
    )

    with (
        patch.object(ui, "_picker_selected_name", return_value="MyDocScript (in Document)"),
        patch.object(ui, "resolve_script_picker_entry", return_value=("MyDocScript", ui.SCRIPT_ORIGIN_DOCUMENT)),
        patch.object(ui, "save_document_script", return_value=doc_err),
        patch.object(ui, "get_user_scripts", return_value={"MyDocScript": "existing code"}),
        patch.object(ui, "save_user_script") as mock_save_user,
        patch.object(inst, "_refresh_script_dropdown") as mock_refresh,
    ):
        res = inst._save_current_script("new code")

    assert res.channel == "modal"
    assert (
        res.text
        == "Could not save 'MyDocScript' to this document (Document is read-only or properties cannot be written.). A script named 'MyDocScript' already exists in My Scripts."
    )
    mock_save_user.assert_not_called()
    mock_refresh.assert_not_called()


def test_save_current_script_fallback_success():
    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._select_ctrl = MagicMock()
    inst._doc = MagicMock()
    inst._current_scripts = {}
    inst._script_origin_map = {}
    doc_err = DocumentScriptError(
        "Document is read-only or properties cannot be written.",
        DocumentScriptErrorCode.READONLY,
    )

    with (
        patch.object(ui, "_picker_selected_name", return_value="MyDocScript (in Document)"),
        patch.object(ui, "resolve_script_picker_entry", return_value=("MyDocScript", ui.SCRIPT_ORIGIN_DOCUMENT)),
        patch.object(ui, "save_document_script", return_value=doc_err),
        patch.object(ui, "get_user_scripts", return_value={}),
        patch.object(ui, "save_user_script", return_value=None) as mock_save_user,
        patch.object(inst, "_refresh_script_dropdown") as mock_refresh,
    ):
        res = inst._save_current_script("new code")

    assert res.channel == "modal"
    assert (
        res.text
        == "Could not save 'MyDocScript' to this document (Document is read-only or properties cannot be written.). Saved it to My Scripts instead."
    )
    mock_save_user.assert_called_once_with("MyDocScript", "new code")
    mock_refresh.assert_called_once_with(select_display="MyDocScript")


def test_save_current_script_non_readonly_does_not_fall_back():
    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._select_ctrl = MagicMock()
    inst._doc = MagicMock()
    inst._current_scripts = {}
    inst._script_origin_map = {}
    doc_err = DocumentScriptError("too big", DocumentScriptErrorCode.TOO_LARGE)

    with (
        patch.object(ui, "_picker_selected_name", return_value="MyDocScript (in Document)"),
        patch.object(ui, "resolve_script_picker_entry", return_value=("MyDocScript", ui.SCRIPT_ORIGIN_DOCUMENT)),
        patch.object(ui, "save_document_script", return_value=doc_err),
        patch.object(ui, "save_user_script") as mock_save_user,
        patch.object(inst, "_refresh_script_dropdown") as mock_refresh,
    ):
        res = inst._save_current_script("new code")

    assert res.channel == "modal"
    assert res.text == "too big"
    mock_save_user.assert_not_called()
    mock_refresh.assert_not_called()


def test_save_current_script_builtin_channel():
    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._select_ctrl = MagicMock()
    inst._doc = MagicMock()
    inst._current_scripts = {}
    inst._script_origin_map = {}

    with (
        patch.object(ui, "_picker_selected_name", return_value="[Vision] extract_text"),
        patch.object(ui, "resolve_script_picker_entry", return_value=("extract_text", "vision")),
        patch.object(ui, "save_document_script") as mock_save_doc,
        patch.object(ui, "save_user_script") as mock_save_user,
    ):
        res = inst._save_current_script("new code")

    assert res.channel == "builtin"
    assert res.text is not None
    assert "Save As" in res.text
    assert "Copy to My Scripts" not in res.text
    mock_save_doc.assert_not_called()
    mock_save_user.assert_not_called()


def test_save_current_script_builtin_does_not_enter_catalog():
    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._select_ctrl = MagicMock()
    inst._doc = MagicMock()
    inst._current_scripts = {}
    inst._script_origin_map = {}

    with (
        patch.object(ui, "_picker_selected_name", return_value="[Vision] extract_text"),
        patch.object(ui, "resolve_script_picker_entry", return_value=("extract_text", "vision")),
        patch.object(ui, "save_document_script") as mock_save_doc,
        patch.object(ui, "save_user_script") as mock_save_user,
    ):
        res = inst._save_current_script("new code")

    assert res.channel == "builtin"
    assert res.text is not None
    assert "Save As" in res.text
    assert "Copy to My Scripts" not in res.text
    mock_save_doc.assert_not_called()
    mock_save_user.assert_not_called()
    assert inst._current_scripts == {}


def test_save_current_script_failure_leaves_catalog_unchanged():
    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._select_ctrl = MagicMock()
    inst._doc = MagicMock()
    inst._current_scripts = {"[Doc] foo": "old"}
    inst._script_origin_map = {}

    err = DocumentScriptError("too big", DocumentScriptErrorCode.TOO_LARGE)
    with (
        patch.object(ui, "_picker_selected_name", return_value="[Doc] foo"),
        patch.object(ui, "resolve_script_picker_entry", return_value=("foo", ui.SCRIPT_ORIGIN_DOCUMENT)),
        patch.object(ui, "save_document_script", return_value=err),
    ):
        res = inst._save_current_script("rejected")

    assert res.channel == "modal"
    assert inst._current_scripts == {"[Doc] foo": "old"}


def test_save_current_script_user_success_is_ok():
    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._select_ctrl = MagicMock()
    inst._doc = MagicMock()
    inst._current_scripts = {}
    inst._script_origin_map = {}

    with (
        patch.object(ui, "_picker_selected_name", return_value="Mine"),
        patch.object(ui, "resolve_script_picker_entry", return_value=("Mine", ui.SCRIPT_ORIGIN_USER)),
        patch.object(ui, "save_user_script", return_value=None) as mock_save_user,
    ):
        res = inst._save_current_script("new code")

    assert res.channel == "ok"
    assert res.text == "Script 'Mine' saved successfully."
    mock_save_user.assert_called_once_with("Mine", "new code")


def test_save_current_script_user_success_updates_catalog():
    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._select_ctrl = MagicMock()
    inst._doc = MagicMock()
    inst._current_scripts = {}
    inst._script_origin_map = {}

    with (
        patch.object(ui, "_picker_selected_name", return_value="Mine"),
        patch.object(ui, "resolve_script_picker_entry", return_value=("Mine", ui.SCRIPT_ORIGIN_USER)),
        patch.object(ui, "save_user_script", return_value=None) as mock_save_user,
    ):
        res = inst._save_current_script("new code")

    assert res.channel == "ok"
    assert res.text == "Script 'Mine' saved successfully."
    mock_save_user.assert_called_once_with("Mine", "new code")
    assert inst._current_scripts == {"Mine": "new code"}


def test_save_current_script_reports_user_script_error():
    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._select_ctrl = MagicMock()
    inst._doc = MagicMock()
    inst._current_scripts = {}
    inst._script_origin_map = {}

    err_msg = DocumentScriptError(
        "'[Doc] foo' starts with a reserved prefix.",
        DocumentScriptErrorCode.RESERVED_NAME,
    )
    with (
        patch.object(ui, "_picker_selected_name", return_value="[Doc] foo"),
        patch.object(ui, "resolve_script_picker_entry", return_value=("[Doc] foo", ui.SCRIPT_ORIGIN_USER)),
        patch.object(ui, "save_user_script", return_value=err_msg) as mock_save_user,
    ):
        res = inst._save_current_script("new code")

    assert res.channel == "modal"
    assert res.text == str(err_msg)
    mock_save_user.assert_called_once_with("[Doc] foo", "new code")


def test_store_script_reports_user_script_error():
    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._ctx = MagicMock()
    inst._doc = None
    lbl_ctrl = MagicMock()
    dlg = MagicMock()
    dlg.getControl.return_value = lbl_ctrl
    inst._dlg = dlg

    err_msg = DocumentScriptError(
        "'[Doc] foo' starts with a reserved prefix.",
        DocumentScriptErrorCode.RESERVED_NAME,
    )
    with (
        patch.object(ui, "get_user_scripts", return_value={}),
        patch.object(ui, "save_user_script", return_value=err_msg),
        patch.object(ui, "set_control_text") as mock_set_text,
    ):
        ret = inst._store_script(
            "[Doc] foo",
            "x",
            attach_to_document=False,
            dialog_title="t",
            doc_success_msg="%s",
            user_success_msg="%s",
        )

    assert ret is False
    mock_set_text.assert_called_once_with(lbl_ctrl, err_msg)


def _setup_dialog_and_listeners(doc=None, code="result = 1"):
    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._ctx = MagicMock()
    inst._doc = doc
    inst._modeless = False
    inst._name_config_key = "scripting.last_python_script_name"
    inst._current_scripts = {}
    inst._script_origin_map = {}
    inst._closed = False

    dlg = MagicMock()
    code_ctrl = MagicMock()
    code_ctrl.getModel.return_value.Text = code
    lbl_ctrl = MagicMock()
    btn_run = MagicMock()
    btn_save = MagicMock()
    btn_new = MagicMock()
    btn_save_as = MagicMock()
    btn_delete = MagicMock()
    btn_cancel = MagicMock()
    script_select = MagicMock()

    controls = {
        "CodeEdit": code_ctrl,
        "InstructionLbl": lbl_ctrl,
        "BtnRun": btn_run,
        "BtnSave": btn_save,
        "BtnNew": btn_new,
        "BtnSaveAs": btn_save_as,
        "BtnDelete": btn_delete,
        "BtnCancel": btn_cancel,
        "ScriptSelect": script_select,
    }
    dlg.getControl.side_effect = lambda name: controls.get(name)
    inst._dlg = dlg
    inst._select_ctrl = script_select

    action_listeners: dict[str, Any] = {}
    for name in ("BtnRun", "BtnSave", "BtnNew", "BtnSaveAs", "BtnDelete", "BtnCancel"):
        mock_btn = controls[name]

        def _make_adder(btn_mock=mock_btn, button_name=name):
            def _add(listener):
                action_listeners[button_name] = listener
            btn_mock.addActionListener.side_effect = _add

        _make_adder()

    inst._wire_listeners(dlg, script_select)

    def _trigger_action(btn_name: str):
        for name, listener in action_listeners.items():
            if name == btn_name:
                listener.actionPerformed(MagicMock())
                return
        raise AssertionError(f"No action listener matching {btn_name}")

    return inst, controls, _trigger_action


def test_run_ok_save_shows_running_and_no_save_box():
    inst, controls, trigger = _setup_dialog_and_listeners()
    save_result = ui._ScriptSaveResult("Script 'Mine' saved successfully.", "ok")
    lbl = controls["InstructionLbl"]

    with (
        patch.object(inst, "_save_current_script", return_value=save_result),
        patch.object(pr, "start_native_script_run") as mock_start_run,
        patch.object(ui, "msgbox") as mock_msgbox,
    ):
        trigger("BtnRun")

    mock_msgbox.assert_not_called()
    mock_start_run.assert_called_once()
    lbl.setText.assert_called_once_with("Running...")
    assert "Script 'Mine' saved successfully." not in [call.args[0] for call in lbl.setText.call_args_list]


def test_run_builtin_keeps_warning_on_the_label():
    warning_text = ui.read_only_helper_message()
    save_result = ui._ScriptSaveResult(warning_text, "builtin")

    # Case 1: success restores builtin warning
    inst, controls, trigger = _setup_dialog_and_listeners()
    lbl = controls["InstructionLbl"]
    captured_on_complete = {}

    def _capture_run(ctx, doc, code, *, on_complete, **kwargs):
        captured_on_complete["callback"] = on_complete

    def _fake_report_outcome(ctx, lbl_ctrl, outcome):
        lbl_ctrl.setText(outcome.get("status_ok_text"))

    with (
        patch.object(inst, "_save_current_script", return_value=save_result),
        patch.object(pr, "start_native_script_run", side_effect=_capture_run) as mock_start_run,
        patch.object(ui, "msgbox") as mock_msgbox,
        patch.object(ui, "_report_run_outcome", side_effect=_fake_report_outcome),
    ):
        trigger("BtnRun")
        mock_msgbox.assert_not_called()
        assert "Running..." not in [call.args[0] for call in lbl.setText.call_args_list]
        mock_start_run.assert_called_once()
        assert "callback" in captured_on_complete
        captured_on_complete["callback"]({"ok": True, "status_ok_text": "Script executed successfully."})

    assert lbl.setText.call_args.args[0] == warning_text

    # Case 2: failure leaves error on label
    inst2, controls2, trigger2 = _setup_dialog_and_listeners()
    lbl2 = controls2["InstructionLbl"]
    captured_on_complete2 = {}

    def _capture_run2(ctx, doc, code, *, on_complete, **kwargs):
        captured_on_complete2["callback"] = on_complete

    with (
        patch.object(inst2, "_save_current_script", return_value=save_result),
        patch.object(pr, "start_native_script_run", side_effect=_capture_run2),
        patch.object(ui, "msgbox") as mock_msgbox,
        patch.object(ui, "_report_run_outcome"),
    ):
        trigger2("BtnRun")
        captured_on_complete2["callback"]({"ok": False, "message": "boom"})

    assert lbl2.setText.call_args.args[0] == "boom"


def test_run_modal_save_boxes_then_runs():
    inst, controls, trigger = _setup_dialog_and_listeners()
    lbl = controls["InstructionLbl"]
    modal_text = "Could not save to this document."
    save_result = ui._ScriptSaveResult(modal_text, "modal")
    order = []

    def _fake_msgbox(ctx, title, text):
        order.append(("msgbox", title, text))

    def _fake_start_run(ctx, doc, code, *, on_complete, **kwargs):
        order.append("start_run")

    with (
        patch.object(inst, "_save_current_script", return_value=save_result),
        patch.object(ui, "msgbox", side_effect=_fake_msgbox),
        patch.object(pr, "start_native_script_run", side_effect=_fake_start_run),
    ):
        trigger("BtnRun")

    assert order == [("msgbox", "Save", modal_text), "start_run"]
    lbl.setText.assert_called_with("Running...")


def test_run_completion_after_close_touches_nothing():
    inst, controls, trigger = _setup_dialog_and_listeners()
    captured_on_complete = {}

    def _capture_run(ctx, doc, code, *, on_complete, **kwargs):
        captured_on_complete["callback"] = on_complete

    with (
        patch.object(inst, "_save_current_script", return_value=ui._ScriptSaveResult(None, "none")),
        patch.object(pr, "start_native_script_run", side_effect=_capture_run),
    ):
        trigger("BtnRun")

    inst._closed = True
    with (
        patch.object(ui, "msgbox") as mock_msgbox,
        patch.object(ui, "set_control_text") as mock_set_text,
    ):
        captured_on_complete["callback"]({"ok": False, "message": "boom"})

    mock_msgbox.assert_not_called()
    mock_set_text.assert_not_called()


def test_run_unchanged_text_skips_the_save():
    inst, controls, trigger = _setup_dialog_and_listeners(code="result = 1")
    inst._current_scripts = {"Mine": "result = 1"}
    lbl = controls["InstructionLbl"]

    with (
        patch.object(ui, "_picker_selected_name", return_value="Mine"),
        patch.object(ui, "resolve_script_picker_entry", return_value=("Mine", ui.SCRIPT_ORIGIN_USER)),
        patch.object(ui, "save_user_script", return_value=None) as mock_save_user,
        patch.object(pr, "start_native_script_run") as mock_start_run,
        patch.object(ui, "msgbox") as mock_msgbox,
    ):
        trigger("BtnRun")

    mock_save_user.assert_not_called()
    mock_msgbox.assert_not_called()
    mock_start_run.assert_called_once()
    lbl.setText.assert_called_with("Running...")


def test_run_changed_text_saves_once():
    inst, controls, trigger = _setup_dialog_and_listeners(code="result = 2")
    inst._current_scripts = {"Mine": "result = 1"}
    lbl = controls["InstructionLbl"]

    with (
        patch.object(ui, "_picker_selected_name", return_value="Mine"),
        patch.object(ui, "resolve_script_picker_entry", return_value=("Mine", ui.SCRIPT_ORIGIN_USER)),
        patch.object(ui, "save_user_script", return_value=None) as mock_save_user,
        patch.object(pr, "start_native_script_run") as mock_start_run,
        patch.object(ui, "msgbox") as mock_msgbox,
    ):
        trigger("BtnRun")

    mock_save_user.assert_called_once_with("Mine", "result = 2")
    mock_msgbox.assert_not_called()
    mock_start_run.assert_called_once()
    lbl.setText.assert_called_with("Running...")


def test_save_button_always_saves_unchanged_text():
    inst, controls, trigger = _setup_dialog_and_listeners(code="result = 1")
    inst._current_scripts = {"Mine": "result = 1"}

    with (
        patch.object(ui, "_picker_selected_name", return_value="Mine"),
        patch.object(ui, "resolve_script_picker_entry", return_value=("Mine", ui.SCRIPT_ORIGIN_USER)),
        patch.object(ui, "save_user_script") as mock_save_user,
        patch.object(ui, "msgbox") as mock_msgbox,
    ):
        trigger("BtnSave")

    mock_save_user.assert_called_once_with("Mine", "result = 1")
    mock_msgbox.assert_not_called()


def test_delete_user_script_failure_does_not_claim_success():
    inst, controls, trigger = _setup_dialog_and_listeners()
    lbl = controls["InstructionLbl"]
    del_err = DocumentScriptError(
        "Script 'Mine' does not exist in My Scripts.",
        DocumentScriptErrorCode.NOT_FOUND,
    )

    with (
        patch.object(ui, "_picker_selected_name", return_value="Mine"),
        patch.object(ui, "resolve_script_picker_entry", return_value=("Mine", ui.SCRIPT_ORIGIN_USER)),
        patch.object(ui, "show_approval_dialog", return_value=True),
        patch.object(ui, "delete_user_script", return_value=del_err),
        patch.object(inst, "_refresh_script_dropdown") as mock_refresh,
    ):
        trigger("BtnDelete")

    mock_refresh.assert_not_called()
    assert lbl.setText.call_args.args[0] == del_err
    assert "Script 'Mine' deleted." not in [call.args[0] for call in lbl.setText.call_args_list]


def test_delete_user_script_success_refreshes():
    inst, controls, trigger = _setup_dialog_and_listeners()
    lbl = controls["InstructionLbl"]

    with (
        patch.object(ui, "_picker_selected_name", return_value="Mine"),
        patch.object(ui, "resolve_script_picker_entry", return_value=("Mine", ui.SCRIPT_ORIGIN_USER)),
        patch.object(ui, "show_approval_dialog", return_value=True),
        patch.object(ui, "delete_user_script", return_value=None),
        patch.object(inst, "_refresh_script_dropdown") as mock_refresh,
    ):
        trigger("BtnDelete")

    mock_refresh.assert_called_once_with()
    assert lbl.setText.call_args.args[0] == "Script 'Mine' deleted."


def test_native_dialog_open_survives_warm_schedule_failure():
    inst = ui.NativePythonScriptDialog.__new__(ui.NativePythonScriptDialog)
    inst._ctx = MagicMock()
    inst._doc = MagicMock()
    inst._modeless = False
    inst._closed = False
    inst._dlg = None
    dlg = MagicMock()

    with (
        patch.object(ui, "load_writeragent_dialog_detail", return_value=(dlg, None)),
        patch.object(ui, "run_in_background", side_effect=RuntimeError("pool shut down")),
        patch.object(ui, "build_xdl_script_picker_state", return_value=([], {}, {})),
        patch.object(ui, "get_user_scripts", return_value={}),
        patch.object(inst, "_refresh_script_dropdown"),
        patch.object(inst, "_wire_listeners"),
    ):
        assert inst._open() is True

    assert inst._open_failure_detail is None
    dlg.execute.assert_called_once()


def test_start_native_script_run_uses_dedicated_worker():
    # What was wrong: Script execution ran on the shared background worker pool (dedicated=False).
    # Long-running scripts blocked the pool from handling other tasks.
    # Why this change: AGENTS.md requires long jobs to run with dedicated=True.
    captured = {}

    def _capture_run(func, *args, **kwargs):
        captured["dedicated"] = kwargs.get("dedicated")
        return MagicMock()

    ctx = MagicMock()
    with patch.object(pr, "run_in_background", side_effect=_capture_run):
        pr.start_native_script_run(ctx, None, "print(1)", on_complete=lambda _o: None)

    assert captured.get("dedicated") is True

