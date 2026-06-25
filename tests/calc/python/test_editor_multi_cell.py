# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Monaco editor multi-cell reload (switch cell while editor stays open)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import plugin.calc.python.editor as pe


def test_open_second_cell_reuses_running_editor_and_sends_load():
    """Second open while the child is running should load the new cell, not block."""
    ctx = MagicMock()
    doc_b = MagicMock()
    cell_b = MagicMock()

    existing_session = MagicMock()
    existing_session.is_running = True

    captured: dict = {}

    def fake_launch(_ctx, *, exe, load_message, on_save, on_closed=None):
        captured["load_message"] = load_message
        captured["on_save"] = on_save
        return True

    with patch.object(pe, "get_active_session", return_value=existing_session):
        with patch.object(pe, "_get_active_calc_cell", return_value=(doc_b, cell_b, "")):
            with patch.object(pe, "_load_cell_editor_code", return_value=("print(2)", None, None)):
                with patch.object(pe, "resolve_editor_python", return_value=("/venv/bin/python", None)):
                    with patch.object(pe, "probe_webview_import", return_value=(True, "")):
                        with patch("plugin.calc.python.editor_context_menu.install_calc_cell_context_menu"):
                            with patch.object(pe, "launch_monaco_editor", side_effect=fake_launch):
                                pe.open_python_cell_editor(ctx)

    load_msg = captured.get("load_message")
    assert load_msg is not None, "expected a load message for the new cell"
    assert load_msg["type"] == "load"
    assert load_msg["code"] == "print(2)"
    assert load_msg.get("mode") == "calc_cell"
    assert load_msg.get("show_plain_text") is True
    assert load_msg.get("show_data_binding") is True
    assert load_msg.get("save_as_plain") is True
    assert captured.get("on_save") is not None
