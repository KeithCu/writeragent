# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for editor child ``closed`` lifecycle messaging."""

from __future__ import annotations

from unittest.mock import MagicMock
from plugin.scripting.venv import editor_main as em


def _reset_closed_state() -> None:
    em._closed_sent = False
    em._shutting_down = False


def test_send_closed_once_writes_single_message(monkeypatch):
    _reset_closed_state()
    messages: list[dict] = []
    monkeypatch.setattr(em, "_write_parent", messages.append)

    em._send_closed_once()
    em._send_closed_once()

    assert messages == [{"type": "closed"}]
    assert em._closed_sent is True
    assert em._shutting_down is False  # Process stays alive in background!


def test_notify_cancel_sends_closed_once_and_hides(monkeypatch):
    _reset_closed_state()
    messages: list[dict] = []
    monkeypatch.setattr(em, "_write_parent", messages.append)

    api = em.MonacoEditorApi()
    mock_window = MagicMock()
    api._window = mock_window
    api.notify_cancel()
    api.notify_cancel()

    assert messages == [{"type": "closed"}]
    mock_window.hide.assert_called()


def test_handle_window_closing_intercepts_and_hides(monkeypatch):
    _reset_closed_state()
    messages: list[dict] = []
    monkeypatch.setattr(em, "_write_parent", messages.append)

    mock_window = MagicMock()
    monkeypatch.setattr(em, "_window", mock_window)

    res = em._handle_window_closing()
    assert res is False  # Aborts standard window close
    assert messages == [{"type": "closed"}]
    mock_window.hide.assert_called_once()
