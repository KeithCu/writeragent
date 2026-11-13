# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc cell editor when Monaco is unavailable."""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import plugin.calc.python.editor as pe


def _open_editor(*, monaco, native=(True, None), code=("print(1)", None, None)):
    doc = MagicMock()
    cell = MagicMock()
    with ExitStack() as stack:
        stack.enter_context(patch.object(pe, "_get_active_calc_cell", return_value=(doc, cell)))
        stack.enter_context(patch.object(pe, "_load_cell_editor_code", return_value=code))
        stack.enter_context(patch.object(pe, "monaco_editor_available", return_value=monaco))
        launch = stack.enter_context(patch.object(pe, "launch_monaco_editor", return_value=True))
        msgbox = stack.enter_context(patch.object(pe, "msgbox"))
        stack.enter_context(patch("plugin.calc.python.editor.install_calc_cell_context_menu"))
        native_mock = stack.enter_context(
            patch(
                "plugin.calc.python.cell_editor_ui.show_native_python_cell_editor",
                return_value=native,
            )
        )
        pe.open_python_cell_editor(MagicMock())
    return SimpleNamespace(native=native_mock, launch=launch, msgbox=msgbox)


@pytest.mark.parametrize("monaco", [(None, False), ("/venv/bin/python", False)])
def test_open_python_cell_editor_falls_back_to_native(monaco):
    res = _open_editor(monaco=monaco)
    res.native.assert_called_once()
    res.launch.assert_not_called()
    if monaco == (None, False):
        assert res.native.call_args.kwargs["initial_code"] == "print(1)"
        res.msgbox.assert_not_called()


def test_open_python_cell_editor_native_failure_msgbox():
    res = _open_editor(monaco=(None, False), native=(False, "XDL missing"), code=("x", None, None))
    res.msgbox.assert_called_once()
    assert "XDL missing" in res.msgbox.call_args.args[2]


def test_open_python_cell_editor_launches_when_monaco_available():
    res = _open_editor(monaco=("/venv/bin/python", True))
    res.launch.assert_called_once()
    assert res.launch.call_args.kwargs["exe"] == "/venv/bin/python"
