# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Calc rich HTML cell insert."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import DocumentDisposedError, ToolExecutionError
from plugin.tests.testing_utils import FakeUndoManager


@pytest.fixture(autouse=True)
def _reset_idle_writers(monkeypatch):
    import plugin.framework.scratch_writer as scratch_writer
    from plugin.calc import rich_html as rh

    monkeypatch.setattr(rh, "_idle_writers", [])
    monkeypatch.setattr(scratch_writer, "_scratch_uids", set())
    monkeypatch.setattr(scratch_writer, "_scratch_loads", 0)
    monkeypatch.setattr(scratch_writer, "_after_loads", None)


def _make_hidden_writer(uid: str = "901"):
    writer_ctrl = MagicMock()
    writer_ctrl.getTransferable.return_value = object()
    text_state = [""]
    body = MagicMock()
    body.getString.side_effect = lambda: text_state[0]
    body.setString.side_effect = lambda s: text_state.__setitem__(0, s)
    sel = MagicMock()
    body.createTextCursor.return_value = sel
    temp_doc = MagicMock()
    temp_doc.getRuntimeUID.return_value = uid
    temp_doc.getText.return_value = body
    temp_doc.getCurrentController.return_value = writer_ctrl
    temp_doc.getTextTables.return_value.getElementNames.return_value = ()
    temp_doc.getTextTables.return_value.getCount.return_value = 0
    temp_doc.getTextFrames.return_value.getElementNames.return_value = ()
    temp_doc.getTextFrames.return_value.getCount.return_value = 0
    temp_doc.getDrawPage.return_value.getCount.return_value = 0
    return temp_doc, writer_ctrl


def _cell_and_docs():
    """Minimal Calc + hidden Writer mocks for ``insert_cell_html_rich``."""
    cell = MagicMock()
    cell.getText.return_value = MagicMock()

    calc_ctrl = MagicMock()
    calc_doc = MagicMock()
    calc_doc.getCurrentController.return_value = calc_ctrl

    temp_doc, writer_ctrl = _make_hidden_writer()

    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = temp_doc
    return calc_doc, cell, desktop, temp_doc, calc_ctrl, writer_ctrl


def test_insert_cell_html_rich_keeps_temp_writer_registered_as_scratch():
    """After a successful paste, the retained temp Writer remains a registered scratch doc."""
    import plugin.framework.scratch_writer as scratch_writer
    from plugin.calc.rich_html import insert_cell_html_rich

    calc_doc, cell, desktop, temp_doc, _calc_ctrl, _writer_ctrl = _cell_and_docs()

    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.html_import") as mock_fmt,
    ):
        mock_bridge = mock_bridge_cls.return_value
        mock_bridge.get_cell_by_address.return_value = cell
        mock_fmt.ensure_html_linebreaks.side_effect = lambda html: html

        insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "Plain <b>BoldBit</b> tail")

    temp_doc.close.assert_not_called()
    assert scratch_writer.is_scratch_document(temp_doc) is True


def test_insert_cell_html_rich_steps_go_to_stderr(capsys):
    """GHA 33699746211 had no step line: file-only log.info never reached Actions."""
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("log.info/print stripped in release bundle")
    from plugin.calc.rich_html import insert_cell_html_rich

    calc_doc, cell, desktop, _temp_doc, _calc_ctrl, _writer_ctrl = _cell_and_docs()
    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.html_import") as mock_fmt,
    ):
        mock_bridge = mock_bridge_cls.return_value
        mock_bridge.get_cell_by_address.return_value = cell
        mock_fmt.ensure_html_linebreaks.side_effect = lambda html: html
        insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "Plain <b>BoldBit</b> tail")
    err = capsys.readouterr().err
    assert "insert_cell_html_rich: load hidden Writer" in err
    assert "insert_cell_html_rich: insert transferable" in err
    assert "insert_cell_html_rich: release clipboard" in err


def test_insert_cell_html_rich_error_names_the_last_step():
    from plugin.calc.rich_html import insert_cell_html_rich

    calc_doc, cell, desktop, _temp_doc, _calc_ctrl, writer_ctrl = _cell_and_docs()
    writer_ctrl.getTransferable.side_effect = RuntimeError("no transferable")

    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.html_import") as mock_fmt,
        pytest.raises(ToolExecutionError, match="get transferable"),
    ):
        mock_bridge = mock_bridge_cls.return_value
        mock_bridge.get_cell_by_address.return_value = cell
        mock_fmt.ensure_html_linebreaks.side_effect = lambda html: html
        insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "<b>x</b>")


def test_failed_paste_keeps_the_writer_for_the_next_one():
    """A failing paste keeps the Writer in the idle list and reuses it on the next paste."""
    from plugin.calc.rich_html import insert_cell_html_rich

    calc_doc, cell, desktop, temp_doc, _calc_ctrl, writer_ctrl = _cell_and_docs()

    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.html_import") as mock_fmt,
    ):
        mock_bridge = mock_bridge_cls.return_value
        mock_bridge.get_cell_by_address.return_value = cell
        mock_fmt.ensure_html_linebreaks.side_effect = lambda html: html

        writer_ctrl.getTransferable.side_effect = RuntimeError("no transferable")
        with pytest.raises(ToolExecutionError):
            insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "<b>x</b>")

        writer_ctrl.getTransferable.side_effect = None
        writer_ctrl.getTransferable.return_value = object()
        insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "<b>y</b>")

        assert desktop.loadComponentFromURL.call_count == 1
        temp_doc.close.assert_not_called()


def test_insert_cell_html_rich_raises_when_hidden_writer_stays_dirty():
    from plugin.calc.rich_html import insert_cell_html_rich

    calc_doc, cell, desktop, temp_doc, _calc_ctrl, _writer_ctrl = _cell_and_docs()

    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.html_import") as mock_fmt,
        patch("plugin.framework.scratch_writer.clear_writer_body", return_value=False),
        pytest.raises(ToolExecutionError, match="Could not create temporary Writer document"),
    ):
        mock_bridge = mock_bridge_cls.return_value
        mock_bridge.get_cell_by_address.return_value = cell
        mock_fmt.ensure_html_linebreaks.side_effect = lambda html: html
        insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "<b>x</b>")

    temp_doc.close.assert_called_once_with(True)


def test_second_paste_reuses_the_hidden_writer():
    from plugin.calc import rich_html as rh

    calc_doc, cell, desktop, _temp_doc, _calc_ctrl, _writer_ctrl = _cell_and_docs()
    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.html_import") as mock_fmt,
    ):
        mock_bridge_cls.return_value.get_cell_by_address.return_value = cell
        mock_fmt.ensure_html_linebreaks.side_effect = lambda html: html

        rh.insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "<b>one</b>")
        rh.insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "<b>two</b>")

    assert desktop.loadComponentFromURL.call_count == 1
    assert len(rh._idle_writers) == 1


def test_closed_kept_writer_is_replaced():
    from plugin.calc import rich_html as rh

    calc_doc, cell, desktop, doc1, _calc_ctrl, _writer_ctrl = _cell_and_docs()
    doc2, _ctrl2 = _make_hidden_writer("902")
    desktop.loadComponentFromURL.side_effect = [doc1, doc2]

    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.html_import") as mock_fmt,
    ):
        mock_bridge_cls.return_value.get_cell_by_address.return_value = cell
        mock_fmt.ensure_html_linebreaks.side_effect = lambda html: html

        rh.insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "<b>one</b>")
        doc1.getRuntimeUID.return_value = ""
        rh.insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "<b>two</b>")

    assert desktop.loadComponentFromURL.call_count == 2
    assert len(rh._idle_writers) == 1
    assert rh._idle_writers[0] is doc2


def test_nested_paste_gets_its_own_writer():
    from plugin.calc import rich_html as rh

    calc_doc, cell, desktop, doc1, _calc_ctrl, _writer_ctrl = _cell_and_docs()
    doc2, _ctrl2 = _make_hidden_writer("902")
    desktop.loadComponentFromURL.side_effect = [doc1, doc2]
    uno_ctx = MagicMock()
    entered = [False]

    def _nested_import(*_a, **_k):
        if not entered[0]:
            entered[0] = True
            rh.insert_cell_html_rich(calc_doc, uno_ctx, "Z98", "<b>inner</b>")

    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.html_import") as mock_fmt,
    ):
        mock_bridge_cls.return_value.get_cell_by_address.return_value = cell
        mock_fmt.ensure_html_linebreaks.side_effect = lambda html: html
        mock_fmt.insert_html_fragment_at_cursor.side_effect = _nested_import

        rh.insert_cell_html_rich(calc_doc, uno_ctx, "Z99", "<b>outer</b>")

    assert desktop.loadComponentFromURL.call_count == 2
    assert len(rh._idle_writers) == 2


def test_idle_writer_with_leftover_text_is_dropped():
    from plugin.calc import rich_html as rh

    calc_doc, cell, desktop, doc1, _calc_ctrl, _writer_ctrl = _cell_and_docs()
    doc2, _ctrl2 = _make_hidden_writer("902")
    desktop.loadComponentFromURL.side_effect = [doc1, doc2]

    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.html_import") as mock_fmt,
    ):
        mock_bridge_cls.return_value.get_cell_by_address.return_value = cell
        mock_fmt.ensure_html_linebreaks.side_effect = lambda html: html

        rh.insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "<b>one</b>")
        doc1.getText().getString.side_effect = lambda: "xleftover"
        rh.insert_cell_html_rich(calc_doc, MagicMock(), "Z99", "<b>two</b>")

    assert desktop.loadComponentFromURL.call_count == 2
    doc1.close.assert_not_called()
    assert rh._idle_writers == [doc2]


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

    _release_clipboard_if_holds(ctx, xfer)
    clip.setContents.assert_called_once_with(None, None)

    clip.reset_mock()
    clip.getContents.return_value = object()
    _release_clipboard_if_holds(ctx, xfer)
    clip.setContents.assert_not_called()


def test_insert_cell_html_rich_releases_clipboard_and_keeps_writer():
    """Drop SwTransferable from SystemClipboard and return the Writer to the idle list."""
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
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.html_import") as mock_fmt,
    ):
        mock_bridge = mock_bridge_cls.return_value
        mock_bridge.get_cell_by_address.return_value = cell
        mock_fmt.ensure_html_linebreaks.side_effect = lambda html: html
        insert_cell_html_rich(calc_doc, uno_ctx, "Z99", "<b>x</b>")

    assert order == ["setContents"]
    temp_doc.close.assert_not_called()


def test_insert_cell_html_rich_passes_sheet_prefix_to_bridge():
    from plugin.calc.rich_html import insert_cell_html_rich

    calc_doc, cell, desktop, _temp_doc, _calc_ctrl, _writer_ctrl = _cell_and_docs()
    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.html_import") as mock_fmt,
    ):
        mock_bridge = mock_bridge_cls.return_value
        mock_bridge.get_cell_by_address.return_value = cell
        mock_fmt.ensure_html_linebreaks.side_effect = lambda html: html
        mock_fmt.insert_html_fragment_at_cursor.return_value = None
        insert_cell_html_rich(calc_doc, MagicMock(), "Sheet2.A1", "<b>x</b>")
    mock_bridge.get_cell_by_address.assert_called_once_with("Sheet2.A1")


def test_failed_paste_undoes_the_clear():
    from plugin.calc.rich_html import insert_cell_html_rich

    calc_doc, cell, desktop, _temp_doc, calc_ctrl, _writer_ctrl = _cell_and_docs()
    fake_undo = FakeUndoManager()
    calc_doc.getUndoManager.return_value = fake_undo
    calc_ctrl.insertTransferable.side_effect = RuntimeError("paste boom")

    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.html_import") as mock_fmt,
    ):
        mock_bridge_cls.return_value.get_cell_by_address.return_value = cell
        mock_fmt.ensure_html_linebreaks.side_effect = lambda html: html
        with pytest.raises(ToolExecutionError, match="insert transferable"):
            insert_cell_html_rich(calc_doc, MagicMock(), "A1", "<b>x</b>")

    assert fake_undo.entered == ["WriterAgent: Insert HTML"]
    assert fake_undo.undone == 1


def test_disposed_document_mid_insert_is_document_disposed():
    from types import SimpleNamespace
    from plugin.calc.cells import InsertCellHtml
    from plugin.calc.rich_html import insert_cell_html_rich

    class DisposedException(Exception):
        pass

    calc_doc, cell, desktop, _temp_doc, calc_ctrl, _writer_ctrl = _cell_and_docs()
    fake_undo = FakeUndoManager()
    calc_doc.getUndoManager.return_value = fake_undo
    calc_ctrl.insertTransferable.side_effect = DisposedException("gone")

    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.calc.rich_html.CalcBridge") as mock_bridge_cls,
        patch("plugin.calc.rich_html.html_import") as mock_fmt,
    ):
        mock_bridge_cls.return_value.get_cell_by_address.return_value = cell
        mock_fmt.ensure_html_linebreaks.side_effect = lambda html: html
        with pytest.raises(DocumentDisposedError):
            insert_cell_html_rich(calc_doc, MagicMock(), "A1", "<b>x</b>")
        assert fake_undo.undone == 0

        ctx = SimpleNamespace(doc=calc_doc, ctx=MagicMock())
        res = InsertCellHtml().execute_safe(ctx, cell="A1", html="<b>x</b>")
        assert res["status"] == "error"
        assert res["code"] == "DOCUMENT_DISPOSED"
