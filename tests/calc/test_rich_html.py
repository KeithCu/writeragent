# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc rich HTML cell insert."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import ToolExecutionError


def _cell_and_docs():
    """Minimal Calc + hidden Writer mocks for ``insert_cell_html_rich``."""
    cell = MagicMock()
    cell.getText.return_value = MagicMock()

    calc_ctrl = MagicMock()
    calc_doc = MagicMock()
    calc_doc.getCurrentController.return_value = calc_ctrl

    writer_ctrl = MagicMock()
    writer_ctrl.getTransferable.return_value = object()
    body = MagicMock()
    sel = MagicMock()
    body.createTextCursor.return_value = sel
    temp_doc = MagicMock()
    temp_doc.getText.return_value = body
    temp_doc.getCurrentController.return_value = writer_ctrl

    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = temp_doc
    return calc_doc, cell, desktop, temp_doc, calc_ctrl, writer_ctrl


def test_insert_cell_html_rich_loads_temp_writer_with_blank_target(caplog):
    """Temp Writer must use _blank so it does not reuse the Windows UNO keeper frame."""
    from plugin.calc.rich_html import insert_cell_html_rich

    calc_doc, cell, desktop, temp_doc, calc_ctrl, writer_ctrl = _cell_and_docs()
    hidden_prop = object()

    with (
        patch("plugin.calc.rich_html.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.format_support") as mock_fmt,
        caplog.at_level(logging.INFO, logger="writeragent.calc"),
    ):
        mock_bridge = mock_bridge_cls.return_value
        mock_bridge.get_active_sheet.return_value = MagicMock()
        mock_bridge.get_cell.return_value = cell
        mock_fmt._ensure_html_linebreaks.side_effect = lambda html: html
        mock_fmt.create_property_value.return_value = hidden_prop

        insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "Plain <b>BoldBit</b> tail")

    desktop.loadComponentFromURL.assert_called_once()
    args, _unused_kwargs = desktop.loadComponentFromURL.call_args
    assert args[0] == "private:factory/swriter"
    assert args[1] == "_blank"
    assert args[2] == 0
    assert args[3] == (hidden_prop,)
    mock_fmt.create_property_value.assert_called_once_with("Hidden", True)
    mock_fmt._insert_starwriter_html_at_cursor.assert_called_once()
    writer_ctrl.getTransferable.assert_called_once()
    calc_ctrl.select.assert_called_once_with(cell)
    calc_ctrl.insertTransferable.assert_called_once()
    temp_doc.close.assert_called_once_with(True)
    for step in (
        "loadComponentFromURL",
        "HTML insert",
        "getTransferable",
        "select cell",
        "insertTransferable",
        "close",
    ):
        assert f"insert_cell_html_rich: {step} start" in caplog.text
        assert f"insert_cell_html_rich: {step} done" in caplog.text
    assert "target=_blank" in caplog.text
    assert "reused_existing=" in caplog.text


def test_insert_cell_html_rich_steps_go_to_stderr(capsys):
    """GHA 33699746211 had no step line: file-only log.info never reached Actions."""
    from plugin.calc.rich_html import insert_cell_html_rich

    calc_doc, cell, desktop, _temp_doc, _calc_ctrl, _writer_ctrl = _cell_and_docs()
    with (
        patch("plugin.calc.rich_html.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.format_support") as mock_fmt,
    ):
        mock_bridge = mock_bridge_cls.return_value
        mock_bridge.get_active_sheet.return_value = MagicMock()
        mock_bridge.get_cell.return_value = cell
        mock_fmt._ensure_html_linebreaks.side_effect = lambda html: html
        mock_fmt.create_property_value.return_value = object()
        insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "Plain <b>BoldBit</b> tail")
    err = capsys.readouterr().err
    assert "insert_cell_html_rich: loadComponentFromURL start target=_blank" in err
    assert "insert_cell_html_rich: writers_open=" in err
    assert "insert_cell_html_rich: loadComponentFromURL done" in err
    assert "reused_existing=" in err
    assert "insert_cell_html_rich: insertTransferable start" in err
    assert "insert_cell_html_rich: close done" in err


def test_desktop_writer_uids_stops_on_mock_enumeration():
    """MagicMock.hasMoreElements() is truthy; must not spin."""
    from plugin.calc.rich_html import _desktop_writer_uids

    assert _desktop_writer_uids(MagicMock()) == []


def test_desktop_writer_uids_records_writer_runtime_uid():
    from plugin.calc.rich_html import _desktop_writer_uids

    writer = MagicMock()
    writer.supportsService.side_effect = lambda name: name == "com.sun.star.text.TextDocument"
    writer.RuntimeUID = "keeper-1"
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = writer
    desktop = MagicMock()
    desktop.getComponents.return_value.createEnumeration.return_value = enum
    assert _desktop_writer_uids(desktop) == ["keeper-1"]


def test_insert_cell_html_rich_empty_html_raises():
    from plugin.calc.rich_html import insert_cell_html_rich

    with pytest.raises(ToolExecutionError, match="HTML content is empty"):
        insert_cell_html_rich(MagicMock(), MagicMock(), "A1", "   ")
