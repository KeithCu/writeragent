# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script native dialog UI."""

from __future__ import annotations

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


def test_report_run_outcome_error_skips_status_label():
    ctx = MagicMock()
    lbl = MagicMock()
    outcome = {"ok": False, "message": "boom"}

    with patch.object(ui, "msgbox") as mock_msgbox:
        with patch.object(ui, "set_control_text") as mock_set_text:
            ui._report_run_outcome(ctx, lbl, outcome)

    mock_msgbox.assert_called_once()
    mock_set_text.assert_not_called()
