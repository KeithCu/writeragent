# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for editor child ``closed`` lifecycle messaging."""

from __future__ import annotations

import queue
import threading
from unittest.mock import MagicMock
from plugin.scripting.venv import editor_main as em


def _reset_closed_state() -> None:
    em._closed_sent = False
    em._closed_session_id = ""
    em._shutting_down = False
    em._gui_loop_entered = False
    em._current_session_id = ""
    em._current_mode = ""
    em._current_target = {}
    while True:
        try:
            em._ui_queue.get_nowait()
        except queue.Empty:
            break


def test_send_closed_once_writes_single_message(monkeypatch):
    _reset_closed_state()
    messages: list[dict] = []
    monkeypatch.setattr(em, "_emit", messages.append)

    em._send_closed_once()
    em._send_closed_once()

    assert messages[0]["type"] == "closed"
    assert em._closed_sent is True
    assert em._shutting_down is False  # Process stays alive in background!


def test_notify_cancel_sends_closed_once_and_hides(monkeypatch):
    _reset_closed_state()
    messages: list[dict] = []
    monkeypatch.setattr(em, "_emit", messages.append)

    api = em.MonacoEditorApi()
    mock_window = MagicMock()
    em._window = mock_window
    api.notify_cancel()
    api.notify_cancel()

    assert messages[0]["type"] == "closed"
    mock_window.hide.assert_called()


def test_handle_window_closing_intercepts_and_hides(monkeypatch):
    _reset_closed_state()
    messages: list[dict] = []
    monkeypatch.setattr(em, "_emit", messages.append)

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
    em._window = mock_window

    em._closed_sent = True
    em._current_session_id = "stale"

    em._ui_queue.put({"type": "load", "title": "My Custom Title", "code": "x = 1", "session_id": "s1"})
    msgs = api.poll_messages()

    assert len(msgs) == 1
    assert msgs[0]["title"] == "My Custom Title"
    assert mock_window.title == "My Custom Title"
    mock_window.show.assert_called_once()
    assert em._closed_sent is False
    assert em._current_session_id == "s1"


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


def test_closed_stamp_ignores_a_load_that_arrives_during_the_write(monkeypatch):
    _reset_closed_state()
    em._remember_session({"session_id": "old", "mode": "calc_cell", "target": {"cell_address": "A1"}})

    ev1 = threading.Event()
    ev2 = threading.Event()
    messages: list[dict] = []

    def slow_emit(msg):
        ev1.set()
        ev2.wait(timeout=5.0)
        messages.append(msg)

    monkeypatch.setattr(em, "_emit", slow_emit)

    t = threading.Thread(target=em._send_closed_once)
    t.start()

    assert ev1.wait(timeout=5.0)
    em._note_load({"type": "load", "session_id": "new", "mode": "calc_cell", "target": {"cell_address": "B2"}})
    ev2.set()
    t.join(timeout=5.0)

    assert len(messages) == 1
    assert messages[0]["session_id"] == "old"
    assert messages[0]["target"]["cell_address"] == "A1"

    monkeypatch.setattr(em, "_emit", messages.append)
    em._send_closed_once()
    assert len(messages) == 2
    assert messages[1]["session_id"] == "new"
    assert messages[1]["target"]["cell_address"] == "B2"


def test_write_parent_stamps_a_whole_session_pair(monkeypatch):
    _reset_closed_state()
    messages: list[dict] = []
    monkeypatch.setattr(em, "write_message", lambda _stream, msg: messages.append(msg))

    stop_event = threading.Event()

    def remember_loop():
        toggle = False
        while not stop_event.is_set():
            if toggle:
                em._remember_session({"session_id": "a", "mode": "calc_cell", "target": {"cell_address": "A1"}})
            else:
                em._remember_session({"session_id": "b", "mode": "run_script", "target": {"script_name": "n"}})
            toggle = not toggle

    def write_loop():
        for _ in range(400):
            em._write_parent({"type": "dirty", "dirty": True})
        stop_event.set()

    t_remember = threading.Thread(target=remember_loop)
    t_write = threading.Thread(target=write_loop)

    t_remember.start()
    t_write.start()

    t_write.join(timeout=5.0)
    t_remember.join(timeout=5.0)

    assert len(messages) == 400
    for m in messages:
        if m["session_id"] == "a":
            assert m["mode"] == "calc_cell"
            assert m["target"] == {"cell_address": "A1"}
        elif m["session_id"] == "b":
            assert m["mode"] == "run_script"
            assert m["target"] == {"script_name": "n"}
        else:
            raise AssertionError(f"Unexpected session_id: {m}")
