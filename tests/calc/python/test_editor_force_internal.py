# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc cell editor when scripting.force_internal_script_editor is set."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import plugin.calc.python.editor as pe


def test_open_python_cell_editor_msgbox_when_force_internal():
    ctx = MagicMock()
    doc = MagicMock()
    cell = MagicMock()

    with patch.object(pe, "get_active_session", return_value=None):
        with patch.object(pe, "_get_active_calc_cell", return_value=(doc, cell, "")):
            with patch.object(pe, "_load_cell_editor_code", return_value=("print(1)", None, None)):
                with patch.object(pe, "monaco_editor_available", return_value=(None, False)):
                    with patch("plugin.calc.python.editor_context_menu.install_calc_cell_context_menu"):
                        with patch.object(pe, "get_config", return_value=True) as mock_get:
                            with patch.object(pe, "msgbox") as mock_msgbox:
                                with patch.object(pe, "launch_monaco_editor") as mock_launch:
                                    pe.open_python_cell_editor(ctx)

    mock_get.assert_called_with("scripting.force_internal_script_editor")
    mock_msgbox.assert_called_once()
    assert "force_internal_script_editor" in mock_msgbox.call_args.args[2]
    mock_launch.assert_not_called()


def test_open_python_cell_editor_launches_when_monaco_available():
    ctx = MagicMock()
    doc = MagicMock()
    cell = MagicMock()

    with patch.object(pe, "get_active_session", return_value=None):
        with patch.object(pe, "_get_active_calc_cell", return_value=(doc, cell, "")):
            with patch.object(pe, "_load_cell_editor_code", return_value=("print(1)", None, None)):
                with patch.object(pe, "monaco_editor_available", return_value=("/venv/bin/python", True)):
                    with patch("plugin.calc.python.editor_context_menu.install_calc_cell_context_menu"):
                        with patch.object(pe, "launch_monaco_editor", return_value=True) as mock_launch:
                            pe.open_python_cell_editor(ctx)

    mock_launch.assert_called_once()
    assert mock_launch.call_args.kwargs["exe"] == "/venv/bin/python"
