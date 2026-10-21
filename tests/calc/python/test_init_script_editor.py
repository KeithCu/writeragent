# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Calc initialization script editor launcher."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc.python.init_script_editor import open_init_script_editor


def test_open_init_script_editor_no_calc_doc():
    ctx = MagicMock()
    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.calc.python.init_script_editor.get_calc_document_from_ctx", return_value=None),
        patch("plugin.calc.python.init_script_editor.msgbox") as mock_box,
    ):
        open_init_script_editor(ctx)
        mock_box.assert_called_once()
        assert "spreadsheet" in mock_box.call_args[0][2].lower()


def test_open_init_script_editor_force_internal():
    ctx = MagicMock()
    doc = MagicMock()
    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.calc.python.init_script_editor.get_calc_document_from_ctx", return_value=doc),
        patch("plugin.calc.python.init_script_editor.monaco_editor_available", return_value=(None, False)),
        patch("plugin.calc.python.init_script_editor.get_config", side_effect=lambda k: k == "scripting.force_internal_script_editor"),
        patch("plugin.calc.python.init_script_editor.msgbox") as mock_box,
    ):
        open_init_script_editor(ctx)
        mock_box.assert_called_once()
        assert "disabled" in mock_box.call_args[0][2].lower()


def test_open_init_script_editor_no_venv():
    ctx = MagicMock()
    doc = MagicMock()
    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.calc.python.init_script_editor.get_calc_document_from_ctx", return_value=doc),
        patch("plugin.calc.python.init_script_editor.monaco_editor_available", return_value=(None, False)),
        patch("plugin.calc.python.init_script_editor.get_config", return_value=False),
        patch("plugin.calc.python.init_script_editor.resolve_editor_python", return_value=(None, "No python")),
        patch("plugin.calc.python.init_script_editor.msgbox") as mock_box,
    ):
        open_init_script_editor(ctx)
        mock_box.assert_called_once()
        assert mock_box.call_args[0][2] == "No python"


def test_open_init_script_editor_webview_failure():
    ctx = MagicMock()
    doc = MagicMock()
    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.calc.python.init_script_editor.get_calc_document_from_ctx", return_value=doc),
        patch("plugin.calc.python.init_script_editor.monaco_editor_available", return_value=("/bin/python", False)),
        patch("plugin.calc.python.init_script_editor.get_config", return_value=False),
        patch("plugin.calc.python.init_script_editor.probe_webview_import", return_value=(False, "No webview module")),
        patch("plugin.calc.python.init_script_editor.msgbox_with_report") as mock_report,
    ):
        open_init_script_editor(ctx)
        mock_report.assert_called_once()
        assert "webview" in mock_report.call_args[0][2].lower()


def test_open_init_script_editor_success_and_on_save():
    ctx = MagicMock()
    doc = MagicMock()
    saved_cb = None

    def capture_launch(uno_ctx, *, exe, load_message, on_save):
        nonlocal saved_cb
        saved_cb = on_save
        return True

    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.calc.python.init_script_editor.get_calc_document_from_ctx", return_value=doc),
        patch("plugin.calc.python.init_script_editor.monaco_editor_available", return_value=("/bin/python", True)),
        patch("plugin.calc.python.init_script_editor.get_calc_init_script", return_value="# init script"),
        patch("plugin.calc.python.init_script_editor.document_scripts_identity", return_value="doc_1"),
        patch("plugin.calc.python.init_script_editor.launch_monaco_editor", side_effect=capture_launch),
        patch("plugin.calc.python.init_script_editor.set_calc_init_script") as mock_set,
        patch("plugin.calc.python.init_script_editor.reset_workbook_python_session") as mock_reset,
    ):
        open_init_script_editor(ctx)
        assert saved_cb is not None

        # Test on_save error branch
        mock_set.return_value = "write error"
        res_err = saved_cb("bad code", True, None, "save")
        assert res_err == {"type": "error", "message": "write error"}
        mock_reset.assert_not_called()

        # Test on_save success branch
        mock_set.return_value = None
        res_ok = saved_cb("good code", True, None, "save")
        assert res_ok.get("type") == "saved"
        assert res_ok.get("ok") is True
        mock_reset.assert_called_once_with(ctx)
