# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for LibrePy Python sidebar helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.librepy.python_sidebar import format_runtime_status, workbook_key_for_doc


def test_format_runtime_status_isolated_embedded():
    ctx = MagicMock()
    with (
        patch("plugin.librepy.python_sidebar.python_session_mode", return_value="isolated"),
        patch("plugin.librepy.python_sidebar.get_config_str", return_value=""),
    ):
        text = format_runtime_status(ctx, None)
    assert "Isolated" in text
    assert "embedded" in text.lower() or "LibreOffice" in text


def test_format_runtime_status_shared_with_venv():
    ctx = MagicMock()
    with (
        patch("plugin.librepy.python_sidebar.python_session_mode", return_value="shared"),
        patch("plugin.librepy.python_sidebar.get_config_str", return_value="/tmp/myvenv"),
        patch("plugin.librepy.python_sidebar.resolve_venv_python", return_value="/tmp/myvenv/bin/python"),
    ):
        text = format_runtime_status(ctx, None)
    assert "Shared" in text
    assert "/tmp/myvenv" in text


def test_workbook_key_for_doc_uses_session_id():
    doc = MagicMock()
    with patch(
        "plugin.librepy.python_sidebar.calc_workbook_base_session_id",
        return_value="calc:file:///tmp/a.ods",
    ):
        assert workbook_key_for_doc(doc) == "calc:file:///tmp/a.ods"


def test_workbook_key_unknown_on_none():
    assert workbook_key_for_doc(None) == "unknown"


def test_python_sidebar_xdl_uses_menulist_not_listbox():
    """LibreOffice dialog.dtd has dlg:menulist only; dlg:listbox breaks createContainerWindow
    and aborts soffice with 'pure virtual method called' when the Calc sidebar opens."""
    from pathlib import Path

    xdl = Path(__file__).resolve().parents[2] / "extension" / "Dialogs" / "PythonSidebarDialog.xdl"
    text = xdl.read_text(encoding="utf-8")
    assert "dlg:listbox" not in text
    assert 'dlg:id="cells_list"' in text and "dlg:menulist" in text
    assert 'dlg:id="diag_list"' in text
