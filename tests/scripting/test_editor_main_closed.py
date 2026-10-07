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
    em._current_session_id = ""
    em._current_mode = ""
    em._current_target = {}


def test_send_closed_once_writes_single_message(monkeypatch):
    _reset_closed_state()
    messages: list[dict] = []
    monkeypatch.setattr(em, "_write_parent", messages.append)

    em._send_closed_once()
    em._send_closed_once()

    assert messages[0]["type"] == "closed"
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

    assert messages[0]["type"] == "closed"
    mock_window.hide.assert_called()


def test_handle_window_closing_intercepts_and_hides(monkeypatch):
    _reset_closed_state()
    messages: list[dict] = []
    monkeypatch.setattr(em, "_write_parent", messages.append)

    mock_window = MagicMock()
    monkeypatch.setattr(em, "_window", mock_window)

    res = em._handle_window_closing()
    assert res is False  # Aborts standard window close
    assert messages[0]["type"] == "closed"
    mock_window.hide.assert_called_once()


def test_handle_window_closing_returns_true_when_shutting_down():
    _reset_closed_state()
    em._shutting_down = True
    assert em._handle_window_closing() is True


def test_poll_messages_load_shows_window_and_updates_title():
    _reset_closed_state()
    api = em.MonacoEditorApi()
    mock_window = MagicMock()
    api._window = mock_window

    em._put_ui({"type": "load", "title": "My Custom Title", "code": "x = 1", "session_id": "s1"})
    msgs = api.poll_messages()

    assert len(msgs) == 1
    assert msgs[0]["title"] == "My Custom Title"
    assert mock_window.title == "My Custom Title"
    mock_window.show.assert_called_once()
    assert em._closed_sent is False


def test_notify_save_stamps_session_envelope(monkeypatch):
    _reset_closed_state()
    captured: list[dict] = []
    monkeypatch.setattr(em, "write_message", lambda _stream, msg: captured.append(msg))
    api = em.MonacoEditorApi()
    api.notify_save("print(1)", False, "", "cell_save", "sid-1", "calc_cell", {"cell_address": "A1"})
    assert captured[0]["type"] == "save"
    assert captured[0]["session_id"] == "sid-1"
    assert captured[0]["mode"] == "calc_cell"
    assert captured[0]["target"]["cell_address"] == "A1"
