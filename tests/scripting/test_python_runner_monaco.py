# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script Monaco integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting import python_runner as pr


def test_run_python_dialog_uses_monaco_when_available():
    ctx = MagicMock()
    doc = MagicMock()

    with patch.object(pr, "get_ctx", return_value=ctx):
        with patch.object(pr, "get_desktop") as mock_desktop:
            mock_desktop.return_value.getCurrentComponent.return_value = doc
            with patch.object(pr, "is_writer", return_value=True):
                with patch.object(pr, "is_calc", return_value=False):
                    with patch.object(pr, "is_draw", return_value=False):
                        with patch.object(pr, "get_config_str", return_value="print('hi')"):
                            with patch.object(pr, "monaco_editor_available", return_value=("/venv/bin/python", True)):
                                with patch.object(pr, "_run_python_monaco", return_value=True) as mock_monaco:
                                    with patch.object(pr, "show_python_input_dialog") as mock_native:
                                        pr.run_python_dialog()

    mock_monaco.assert_called_once()
    assert mock_monaco.call_args.kwargs["config_key"] == "last_python_script_writer"
    mock_native.assert_not_called()


def test_run_python_dialog_falls_back_to_native_dialog():
    ctx = MagicMock()
    doc = MagicMock()

    with patch.object(pr, "get_ctx", return_value=ctx):
        with patch.object(pr, "get_desktop") as mock_desktop:
            mock_desktop.return_value.getCurrentComponent.return_value = doc
            with patch.object(pr, "is_writer", return_value=True):
                with patch.object(pr, "is_calc", return_value=False):
                    with patch.object(pr, "is_draw", return_value=False):
                        with patch.object(pr, "get_config_str", return_value="x = 1"):
                            with patch.object(pr, "monaco_editor_available", return_value=(None, False)):
                                with patch.object(pr, "_run_python_monaco") as mock_monaco:
                                    with patch.object(pr, "show_python_input_dialog", return_value="x = 2") as mock_native:
                                        with patch.object(pr, "set_config") as mock_set:
                                            with patch.object(pr, "execute_and_insert_result", return_value={"ok": True}):
                                                pr.run_python_dialog()

    mock_monaco.assert_not_called()
    mock_native.assert_called_once()
    mock_set.assert_called_with(ctx, "last_python_script_writer", "x = 2")


def test_run_python_monaco_on_save_persists_and_executes():
    ctx = MagicMock()
    doc = MagicMock()
    captured: dict = {}

    def fake_launch(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured["exe"] = exe
        captured["load_message"] = load_message
        captured["on_save"] = on_save
        return True

    with patch.object(pr, "launch_monaco_editor", side_effect=fake_launch):
        with patch.object(pr, "set_config") as mock_set:
            with patch.object(pr, "execute_and_insert_result", return_value={"ok": True, "status_ok_text": "done"}):
                ok = pr._run_python_monaco(
                    ctx,
                    doc,
                    config_key="last_python_script_writer",
                    initial_code="result = 1",
                    exe="/venv/bin/python",
                )

                assert ok is True
                load = captured["load_message"]
                assert load["mode"] == "run_script"
                assert load["run_label"] is not None
                assert load["save_label"] is not None
                assert load["close_label"] is not None
                assert load["show_plain_text"] is False
                assert load["show_data_binding"] is False

                response = captured["on_save"]("result = 2", False, None, "run")
                mock_set.assert_called_with(ctx, "last_python_script_writer", "result = 2")
                assert response == {"type": "saved", "ok": True, "status_ok_text": "done"}

                save_response = captured["on_save"]("result = 3", False, None, "save")
                assert save_response == {"type": "saved", "ok": True, "status_ok_text": "Script saved."}
                assert mock_set.call_count == 2


def test_execute_and_insert_result_returns_error_on_failure():
    ctx = MagicMock()
    with patch.object(pr, "run_code_in_user_venv", return_value={"status": "error", "message": "boom"}):
        outcome = pr.execute_and_insert_result(ctx, MagicMock(), "bad()")
    assert outcome["ok"] is False
    assert outcome["message"] == "boom"
