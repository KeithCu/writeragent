# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.calc.init_script_editor import handle_init_script_editor_action
from plugin.tests.testing_utils import setup_uno_mocks
from tests.writer.test_document_helpers import _DocWithUserDefinedProperties, _UserDefinedProperties

setup_uno_mocks()


def test_save_persists_without_running_worker():
    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    with (
        patch("plugin.calc.init_script_editor.set_calc_init_script", return_value=None) as mock_set,
        patch("plugin.calc.init_script_editor.reset_python_session") as mock_reset,
        patch("plugin.calc.init_script_editor.run_code_in_user_venv") as mock_run,
    ):
        out = handle_init_script_editor_action(ctx, doc, "x = 1", action="save")
    assert out["ok"] is True
    mock_set.assert_called_once()
    mock_reset.assert_called_once()
    mock_run.assert_not_called()


def test_run_persists_and_executes_init():
    ctx = MagicMock()
    props = _UserDefinedProperties()
    doc = _DocWithUserDefinedProperties(props)
    with (
        patch("plugin.calc.init_script_editor.set_calc_init_script", return_value=None),
        patch("plugin.calc.init_script_editor.reset_python_session"),
        patch("plugin.calc.init_script_editor.workbook_session_id", return_value=None),
        patch("plugin.calc.init_script_editor.calc_init_session_id", return_value="calc:wb:init"),
        patch("plugin.calc.init_script_editor.init_script_hash", return_value="abc"),
        patch("plugin.calc.init_script_editor.run_code_in_user_venv", return_value={"status": "ok", "stdout": ""}) as mock_run,
    ):
        out = handle_init_script_editor_action(ctx, doc, "HELPER = 1", action="run")
    assert out["ok"] is True
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs.get("init_script") == "HELPER = 1"
