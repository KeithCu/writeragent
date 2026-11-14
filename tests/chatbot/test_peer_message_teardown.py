# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the peer-message UNO teardown harness."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import plugin.testing_runner as tr
import tests.chatbot.test_peer_message_uno as peer
from plugin.tests.testing_utils import TestingFactory


def _patch_teardown_env(monkeypatch: pytest.MonkeyPatch, *, raw_close: bool):
    order: list[object] = []
    recycle_calls: list[str] = []
    monkeypatch.setattr(peer, "_draw_family_raw_close", lambda: raw_close)
    monkeypatch.setattr(peer, "_close", lambda doc: order.append(("close_doc", doc)) or None)
    monkeypatch.setattr(
        peer, "close_draw_family_doc", lambda doc: order.append(("close_draw_family", doc))
    )
    monkeypatch.setattr(
        peer, "settle_after_draw_family_close", lambda: order.append("post_settle")
    )
    monkeypatch.setattr(
        peer,
        "_reactivate_writer_after_impress",
        lambda ctx, doc: order.append(("reactivate", ctx, doc)),
    )
    monkeypatch.setattr(
        tr.current_office(), "request_recycle", lambda: recycle_calls.append("recycle")
    )
    return order, recycle_calls


def test_teardown_peer_pair_closes_writer_before_impress(monkeypatch: pytest.MonkeyPatch) -> None:
    order, _recycle = _patch_teardown_env(monkeypatch, raw_close=False)
    writer = MagicMock(name="writer")
    impress = MagicMock(name="impress")
    out_writer, out_impress = peer._teardown_peer_pair(writer, impress)
    assert out_writer is None and out_impress is None
    assert order == [
        ("close_doc", writer),
        ("close_draw_family", impress),
        "post_settle",
    ]


def test_teardown_peer_pair_windows_closes_impress_skips_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer._windows_impress_raw_closed = False
    order, recycle_calls = _patch_teardown_env(monkeypatch, raw_close=True)
    writer = MagicMock(name="writer")
    impress = MagicMock(name="impress")
    ctx = MagicMock(name="ctx")
    try:
        out_writer, out_impress = peer._teardown_peer_pair(writer, impress, ctx)
        assert out_writer is None and out_impress is None
        assert order == [
            ("close_draw_family", impress),
            "post_settle",
            ("reactivate", ctx, writer),
        ]
        assert recycle_calls == ["recycle"]
        assert peer._windows_impress_raw_closed is True
    finally:
        peer._windows_impress_raw_closed = False


def test_teardown_peer_pair_windows_skips_second_impress_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer._windows_impress_raw_closed = False
    order, recycle_calls = _patch_teardown_env(monkeypatch, raw_close=True)
    ctx = MagicMock(name="ctx")
    impress1 = MagicMock(name="impress1")
    impress2 = MagicMock(name="impress2")
    try:
        peer._teardown_peer_pair(MagicMock(name="writer1"), impress1, ctx)
        peer._teardown_peer_pair(MagicMock(name="writer2"), impress2, ctx)
        closed_impress = [item[1] for item in order if isinstance(item, tuple) and item[0] == "close_draw_family"]
        assert closed_impress == [impress1]
        assert recycle_calls == ["recycle", "recycle"]
    finally:
        peer._windows_impress_raw_closed = False


def test_teardown_peer_pair_windows_no_impress_just_closes_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order, _recycle = _patch_teardown_env(monkeypatch, raw_close=True)
    writer = MagicMock(name="writer")
    out_writer, out_impress = peer._teardown_peer_pair(writer, None, MagicMock())
    assert out_writer is None and out_impress is None
    assert order == [("close_doc", writer)]


def test_reactivate_writer_after_impress_sets_active_frame() -> None:
    frame = MagicMock()
    writer = MagicMock()
    writer.getCurrentController.return_value.getFrame.return_value = frame
    desktop = MagicMock()
    ctx = MagicMock()
    with patch("tests.chatbot.test_peer_message_uno.get_desktop", return_value=desktop) as get_desktop:
        peer._reactivate_writer_after_impress(ctx, writer)
    get_desktop.assert_called_once_with(ctx)
    desktop.setActiveFrame.assert_called_once_with(frame)
    frame.getContainerWindow.return_value.toFront.assert_called_once_with()


def test_close_skips_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = MagicMock()
    doc.RuntimeUID = "uid-later"
    calls: list[str] = []
    recycle_calls: list[str] = []
    monkeypatch.setattr(peer, "_windows_skip_doc_close", lambda: True)
    monkeypatch.setattr(TestingFactory, "close_doc", lambda _doc: calls.append("close_doc"))
    monkeypatch.setattr(
        tr.current_office(), "request_recycle", lambda: recycle_calls.append("recycle")
    )
    assert peer._close(doc) is None
    assert calls == []
    assert recycle_calls == ["recycle"]
