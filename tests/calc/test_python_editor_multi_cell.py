# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Monaco editor multi-cell reload (switch cell while editor stays open)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc import python_editor as pe


def test_open_second_cell_reuses_running_editor_and_sends_load():
    """Second open while the child is running should load the new cell, not block."""
    ctx = MagicMock()
    doc_b = MagicMock()
    cell_b = MagicMock()
    mock_proc = MagicMock()
    sent_messages: list[dict] = []

    existing_session = MagicMock()
    existing_session.is_running = True

    def fake_send(msg: dict) -> None:
        sent_messages.append(msg)

    with patch.object(pe, "get_active_session", return_value=existing_session):
        with patch.object(pe, "_get_active_calc_cell", return_value=(doc_b, cell_b, "")):
            with patch.object(pe, "_load_cell_editor_code", return_value=("print(2)", None, None)):
                with patch.object(pe, "resolve_editor_python", return_value=("/venv/bin/python", None)):
                    with patch.object(pe, "probe_webview_import", return_value=(True, "")):
                        with patch.object(pe, "_PERSISTENT_EDITOR") as mock_persistent:
                            mock_persistent.is_running = True
                            mock_persistent.proc = mock_proc
                            with patch("plugin.calc.python_editor_context_menu.install_calc_cell_context_menu"):
                                with patch.object(pe, "EditorSession") as mock_session_cls:
                                    new_session = MagicMock()
                                    new_session.is_running = True
                                    new_session.send = fake_send
                                    mock_session_cls.return_value = new_session

                                    pe.open_python_cell_editor(ctx)

    assert sent_messages, "expected a load message for the new cell"
    load_msg = sent_messages[-1]
    assert load_msg["type"] == "load"
    assert load_msg["code"] == "print(2)"
    assert load_msg.get("save_as_plain") is True
    mock_session_cls.assert_called_once()
    assert mock_session_cls.call_args.kwargs["on_save"] is not None
