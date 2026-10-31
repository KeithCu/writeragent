# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc cell editor when Monaco is unavailable."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import plugin.calc.python.editor as pe


def test_open_python_cell_editor_native_when_force_internal():
    ctx = MagicMock()
    doc = MagicMock()
    cell = MagicMock()

    with patch.object(pe, "get_active_session", return_value=None):
        with patch.object(pe, "_get_active_calc_cell", return_value=(doc, cell)):
            with patch.object(pe, "_load_cell_editor_code", return_value=("print(1)", None, None)):
                with patch.object(pe, "monaco_editor_available", return_value=(None, False)):
                    with patch("plugin.calc.python.editor_context_menu.install_calc_cell_context_menu"):
                        with patch.object(pe, "launch_monaco_editor") as mock_launch:
                            with patch.object(pe, "msgbox") as mock_msgbox:
                                with patch(
                                    "plugin.calc.python.cell_editor_ui.show_native_python_cell_editor",
                                    return_value=(True, None),
                                ) as mock_native:
                                    pe.open_python_cell_editor(ctx)

    mock_native.assert_called_once()
    assert mock_native.call_args.kwargs["initial_code"] == "print(1)"
    mock_launch.assert_not_called()
    mock_msgbox.assert_not_called()


def test_open_python_cell_editor_native_when_webview_missing():
    ctx = MagicMock()
    doc = MagicMock()
    cell = MagicMock()

    with patch.object(pe, "get_active_session", return_value=None):
        with patch.object(pe, "_get_active_calc_cell", return_value=(doc, cell)):
            with patch.object(pe, "_load_cell_editor_code", return_value=("", None, None)):
                with patch.object(
                    pe, "monaco_editor_available", return_value=("/venv/bin/python", False)
                ):
                    with patch("plugin.calc.python.editor_context_menu.install_calc_cell_context_menu"):
                        with patch.object(pe, "launch_monaco_editor") as mock_launch:
                            with patch(
                                "plugin.calc.python.cell_editor_ui.show_native_python_cell_editor",
                                return_value=(True, None),
                            ) as mock_native:
                                pe.open_python_cell_editor(ctx)

    mock_native.assert_called_once()
    mock_launch.assert_not_called()


def test_open_python_cell_editor_native_failure_msgbox():
    ctx = MagicMock()
    doc = MagicMock()
    cell = MagicMock()

    with patch.object(pe, "get_active_session", return_value=None):
        with patch.object(pe, "_get_active_calc_cell", return_value=(doc, cell)):
            with patch.object(pe, "_load_cell_editor_code", return_value=("x", None, None)):
                with patch.object(pe, "monaco_editor_available", return_value=(None, False)):
                    with patch("plugin.calc.python.editor_context_menu.install_calc_cell_context_menu"):
                        with patch.object(pe, "msgbox") as mock_msgbox:
                            with patch(
                                "plugin.calc.python.cell_editor_ui.show_native_python_cell_editor",
                                return_value=(False, "XDL missing"),
                            ):
                                pe.open_python_cell_editor(ctx)

    mock_msgbox.assert_called_once()
    assert "XDL missing" in mock_msgbox.call_args.args[2]


def test_open_python_cell_editor_launches_when_monaco_available():
    ctx = MagicMock()
    doc = MagicMock()
    cell = MagicMock()

    with patch.object(pe, "get_active_session", return_value=None):
        with patch.object(pe, "_get_active_calc_cell", return_value=(doc, cell)):
            with patch.object(pe, "_load_cell_editor_code", return_value=("print(1)", None, None)):
                with patch.object(pe, "monaco_editor_available", return_value=("/venv/bin/python", True)):
                    with patch("plugin.calc.python.editor_context_menu.install_calc_cell_context_menu"):
                        with patch.object(pe, "launch_monaco_editor", return_value=True) as mock_launch:
                            pe.open_python_cell_editor(ctx)

    mock_launch.assert_called_once()
    assert mock_launch.call_args.kwargs["exe"] == "/venv/bin/python"
