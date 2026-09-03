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


def test_insert_cell_html_rich_loads_temp_writer_on_named_frame(caplog):
    """Temp Writer must not share testing_runner's Windows keeper target (_blank)."""
    from plugin.calc.rich_html import (
        _HTML_WRITER_SEARCH_FLAGS,
        _HTML_WRITER_TARGET,
        insert_cell_html_rich,
    )

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
    assert args[1] == "_wa_calc_html"
    assert args[1] == _HTML_WRITER_TARGET
    assert args[1] not in ("_blank", "_default")
    assert args[2] == _HTML_WRITER_SEARCH_FLAGS
    assert args[3] == (hidden_prop,)
    mock_fmt.create_property_value.assert_called_once_with("Hidden", True)
    mock_fmt._insert_starwriter_html_at_cursor.assert_called_once()
    writer_ctrl.getTransferable.assert_called_once()
    calc_ctrl.select.assert_called_once_with(cell)
    calc_ctrl.insertTransferable.assert_called_once()
    # GHA 33771766524: close after a live paste hung the next desktop enum.
    temp_doc.close.assert_not_called()
    temp_doc.getText.return_value.setString.assert_called_with("")
    for step in (
        "loadComponentFromURL",
        "HTML insert",
        "getTransferable",
        "select cell",
        "insertTransferable",
        "clipboard release",
    ):
        assert f"insert_cell_html_rich: {step} start" in caplog.text
        assert f"insert_cell_html_rich: {step} done" in caplog.text
    assert "target=_wa_calc_html" in caplog.text
    assert "reused_existing=" in caplog.text
    assert "close_temp=" in caplog.text
    assert "close skipped pasted=True" in caplog.text
    assert "xfer_id=" in caplog.text


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
    assert "insert_cell_html_rich: loadComponentFromURL start target=_wa_calc_html" in err
    assert "insert_cell_html_rich: writers_open=" in err
    assert "insert_cell_html_rich: loadComponentFromURL done" in err
    assert "reused_existing=" in err
    assert "insert_cell_html_rich: insertTransferable start" in err
    assert "insert_cell_html_rich: clipboard release start" in err
    assert "insert_cell_html_rich: clipboard release done" in err
    assert "insert_cell_html_rich: close skipped pasted=True" in err
    assert "insert_cell_html_rich: close done" not in err


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


def test_insert_cell_html_rich_skips_close_when_uid_matches_open_writer():
    """If the load handed back the keeper, closing it would kill the UNO bridge."""
    from plugin.calc.rich_html import insert_cell_html_rich

    calc_doc, cell, desktop, temp_doc, _calc_ctrl, _writer_ctrl = _cell_and_docs()
    temp_doc.RuntimeUID = "keeper-1"
    writer = MagicMock()
    writer.supportsService.side_effect = lambda name: name == "com.sun.star.text.TextDocument"
    writer.RuntimeUID = "keeper-1"
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = writer
    desktop.getComponents.return_value.createEnumeration.return_value = enum

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
        insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "<b>x</b>")
    temp_doc.close.assert_not_called()


def test_insert_cell_html_rich_empty_html_raises():
    from plugin.calc.rich_html import insert_cell_html_rich

    with pytest.raises(ToolExecutionError, match="HTML content is empty"):
        insert_cell_html_rich(MagicMock(), MagicMock(), "A1", "   ")


def test_release_clipboard_if_holds_only_when_ours():
    from plugin.calc.rich_html import _release_clipboard_if_holds

    xfer = object()
    clip = MagicMock()
    clip.getContents.return_value = xfer
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = clip
    ctx = MagicMock()
    ctx.getServiceManager.return_value = smgr
    ctx.ServiceManager = smgr

    assert "released" in _release_clipboard_if_holds(ctx, xfer)
    clip.setContents.assert_called_once_with(None, None)

    clip.reset_mock()
    clip.getContents.return_value = object()
    assert _release_clipboard_if_holds(ctx, xfer).startswith("not_ours")
    clip.setContents.assert_not_called()


def test_insert_cell_html_rich_enumerates_desktop_only_before_load():
    """Product must not call _desktop_writer_uids after closing a post-paste Writer.

    GHA 33772063173: writers_after_close returned in ~10ms, then teardown
    getComponents ~450ms later hung forever on Windows. Enumeration after close
    is unsafe; success path should only count writers before loadComponentFromURL.
    """
    from plugin.calc import rich_html as rh

    calc_doc, cell, desktop, temp_doc, _calc_ctrl, _writer_ctrl = _cell_and_docs()
    enum_calls: list[str] = []
    real_enum = rh._desktop_writer_uids

    def _counting_enum(desk):
        enum_calls.append("enum")
        return real_enum(desk)

    with (
        patch.object(rh, "_desktop_writer_uids", side_effect=_counting_enum),
        patch("plugin.calc.rich_html.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.format_support") as mock_fmt,
    ):
        mock_bridge = mock_bridge_cls.return_value
        mock_bridge.get_active_sheet.return_value = MagicMock()
        mock_bridge.get_cell.return_value = cell
        mock_fmt._ensure_html_linebreaks.side_effect = lambda html: html
        mock_fmt.create_property_value.return_value = object()
        rh.insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "<b>x</b>")

    assert enum_calls == ["enum"]
    temp_doc.close.assert_not_called()


def test_insert_cell_html_rich_releases_clipboard_and_keeps_writer():
    """#572: drop SwTransferable from SystemClipboard. #572-followup: do not close after paste.

    GHA 33771766524: clipboard was empty (release no-op) and close() returned, then
    teardown hung at getComponents. Keep the release; reuse _wa_calc_html instead.
    """
    from plugin.calc.rich_html import insert_cell_html_rich

    calc_doc, cell, desktop, temp_doc, _calc_ctrl, writer_ctrl = _cell_and_docs()
    xfer = object()
    writer_ctrl.getTransferable.return_value = xfer
    order: list[str] = []
    temp_doc.close.side_effect = lambda *_a, **_k: order.append("close")
    clip = MagicMock()
    clip.getContents.return_value = xfer
    clip.setContents.side_effect = lambda *_a, **_k: order.append("setContents")
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = clip
    uno_ctx = MagicMock()
    uno_ctx.getServiceManager.return_value = smgr
    uno_ctx.ServiceManager = smgr

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
        insert_cell_html_rich(calc_doc, uno_ctx, "Z99", "<b>x</b>")

    assert order == ["setContents"]
    temp_doc.close.assert_not_called()


def test_insert_cell_html_rich_closes_writer_when_paste_never_happened():
    """Failed insert before paste may close — Calc does not hold a SwTransferable."""
    from plugin.calc.rich_html import insert_cell_html_rich
    from plugin.framework.errors import ToolExecutionError as ToolErr

    calc_doc, cell, desktop, temp_doc, _calc_ctrl, writer_ctrl = _cell_and_docs()
    writer_ctrl.getTransferable.side_effect = RuntimeError("no transferable")

    with (
        patch("plugin.calc.rich_html.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.format_support") as mock_fmt,
        pytest.raises(ToolErr, match="Failed to insert HTML"),
    ):
        mock_bridge = mock_bridge_cls.return_value
        mock_bridge.get_active_sheet.return_value = MagicMock()
        mock_bridge.get_cell.return_value = cell
        mock_fmt._ensure_html_linebreaks.side_effect = lambda html: html
        mock_fmt.create_property_value.return_value = object()
        insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "<b>x</b>")

    temp_doc.close.assert_called_once_with(True)


def _portion_cell(blocks):
    """Build a cell whose text enumeration yields *blocks* (each a portion or nest)."""
    top = MagicMock()
    top.hasMoreElements.side_effect = [True] * len(blocks) + [False]
    top.nextElement.side_effect = list(blocks)
    text = MagicMock()
    text.createEnumeration.return_value = top
    cell = MagicMock()
    cell.getText.return_value = text
    return cell


def test_iter_cell_text_portions_logs_enumeration(capsys):
    """GHA 33703959362: hang after execute-done may be createEnumeration / nextElement."""
    from tests.calc.test_rich_html_uno import _iter_cell_text_portions_for_test

    portion = MagicMock()
    inner = MagicMock()
    inner.hasMoreElements.side_effect = [True, False]
    inner.nextElement.return_value = portion
    block = MagicMock()
    block.createEnumeration.return_value = inner
    cell = _portion_cell([block])

    assert list(_iter_cell_text_portions_for_test(cell)) == [portion]
    err = capsys.readouterr().err
    assert "insert_cell_html: portions createEnumeration start" in err
    assert "insert_cell_html: portions createEnumeration done" in err
    assert "insert_cell_html: portions hasMoreElements block=0" in err
    assert "insert_cell_html: portions nextElement start block=0" in err
    assert "insert_cell_html: portions nextElement done block=1" in err
    assert "insert_cell_html: portions inner createEnumeration start block=1" in err
    assert "insert_cell_html: portions inner nextElement done block=1 inner=0" in err
    assert "insert_cell_html: portions enum exit yielded=1" in err


def test_insert_cell_html_post_execute_breadcrumbs_present():
    """GHA 33703959362: next Windows timeout must name the post-execute hang site."""
    import inspect

    from tests.calc import test_rich_html_uno as mod

    src = inspect.getsource(inspect.unwrap(mod.test_insert_cell_html))
    for needle in (
        "status assert start",
        "status assert done",
        "getCellByPosition start",
        "getCellByPosition done",
        "getString start",
        "getString done",
        "portion loop start",
        "portion loop done",
        "bold assert start",
        "bold assert done",
        "assertions done",
        "getDocumentProperties probe start",
        "getDocumentProperties probe done",
    ):
        assert needle in src, needle


def test_diagnose_insert_cell_html_bold_logs_enter_exit(capsys):
    from tests.calc.test_rich_html_uno import _diagnose_insert_cell_html_bold

    portion = MagicMock()
    portion.getString.return_value = "BoldBit"
    portion.getPropertyValue.side_effect = lambda name: 150.0 if name == "CharWeight" else "Text"
    inner = MagicMock()
    inner.hasMoreElements.side_effect = [True, False]
    inner.nextElement.return_value = portion
    block = MagicMock()
    block.createEnumeration.return_value = inner

    dump = _diagnose_insert_cell_html_bold(_portion_cell([block]))
    err = capsys.readouterr().err
    assert "insert_cell_html: diagnose start" in err
    assert "insert_cell_html: diagnose done portions=1" in err
    assert "BoldBit" in dump
