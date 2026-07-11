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


def test_report_run_outcome_error_skips_status_label():
    ctx = MagicMock()
    lbl = MagicMock()
    outcome = {"ok": False, "message": "boom"}

    with patch.object(ui, "msgbox") as mock_msgbox:
        with patch.object(ui, "set_control_text") as mock_set_text:
            ui._report_run_outcome(ctx, lbl, outcome)

    mock_msgbox.assert_called_once()
    mock_set_text.assert_not_called()
