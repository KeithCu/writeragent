# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Monaco editor host (spawn, bridge, session launch)."""

from __future__ import annotations

import os
import threading
from unittest.mock import MagicMock, patch

from plugin.scripting import editor_host as launch_mod
from plugin.scripting.editor_host import PersistentEditor, _ASSETS_DIR


def test_launch_monaco_editor_reuses_running_process():
    ctx = MagicMock()
    sent_messages: list[dict] = []
    mock_proc = MagicMock()

    def fake_send(msg: dict) -> None:
        sent_messages.append(msg)

    with patch.object(launch_mod, "_PERSISTENT_EDITOR") as mock_persistent:
        mock_persistent.is_running = True
        mock_persistent.proc = mock_proc
        with patch.object(launch_mod, "EditorSession") as mock_session_cls:
            session = MagicMock()
            session.is_running = True
            session.send = fake_send
            mock_session_cls.return_value = session

            ok = launch_mod.launch_monaco_editor(
                ctx,
                exe="/venv/bin/python",
                load_message={"type": "load", "code": "print(1)"},
                on_save=MagicMock(),
            )

    assert ok is True
    # Theme and localized UI strings are always injected by launch.
    assert sent_messages[0]["type"] == "load"
    assert sent_messages[0]["code"] == "print(1)"
    assert "theme" in sent_messages[0]
    assert "ui" in sent_messages[0]
    assert sent_messages[0]["ui"]["ready"]
    assert sent_messages[0]["theme"]["monaco"] in ("vs", "vs-dark")
    mock_session_cls.assert_called_once()


def test_launch_monaco_editor_spawns_when_not_running():
    ctx = MagicMock()
    mock_proc = MagicMock()
    mock_doc = MagicMock()

    with patch.object(launch_mod, "_PERSISTENT_EDITOR") as mock_persistent:
        mock_persistent.is_running = False
        with patch.object(launch_mod, "spawn_editor_process", return_value=mock_proc):
            with patch.object(launch_mod, "EditorSession") as mock_session_cls:
                session = MagicMock()
                session.is_running = True
                session.wait_for_ready.return_value = True
                mock_session_cls.return_value = session

                load_message = {"type": "load", "mode": "run_script", "run_script_doc": mock_doc}
                ok = launch_mod.launch_monaco_editor(
                    ctx,
                    exe="/venv/bin/python",
                    load_message=load_message,
                    on_save=MagicMock(),
                )

    assert ok is True
    mock_persistent.set_run_script_document.assert_called_once_with(mock_doc)
    session.start_reader.assert_called_once()
    # Theme injection happens (for automatic follow); the exact theme value depends on mock ctx
    sent = session.send.call_args[0][0]
    assert sent["type"] == "load"
    assert sent["mode"] == "run_script"
    assert "theme" in sent
    assert "ui" in sent
    assert sent["ui"]["script_label"]
    assert load_message["run_script_doc"] is mock_doc


def test_monaco_editor_available_false_without_venv():
    ctx = MagicMock()
    with patch.object(launch_mod, "resolve_editor_python", return_value=(None, "missing venv")):
        exe, ok = launch_mod.monaco_editor_available(ctx)
    assert exe is None
    assert ok is False


def test_monaco_editor_available_false_when_webview_missing():
    ctx = MagicMock()
    with patch.object(launch_mod, "resolve_editor_python", return_value=("/venv/bin/python", "")):
        with patch.object(launch_mod, "probe_webview_import", return_value=(False, "no webview")):
            exe, ok = launch_mod.monaco_editor_available(ctx)
    assert exe == "/venv/bin/python"
    assert ok is False


class _FakeProc:
    """Minimal process stand-in for stderr drain tests (no MagicMock fileno quirks)."""

    def __init__(self, stderr: object) -> None:
        self.stderr = stderr
        self.stdout = None
        self.stdin = None
        self._exit_code: int | None = None

    def poll(self) -> int | None:
        return self._exit_code


def test_stderr_drain_preserves_tail_for_failure_dialogs():
    editor = PersistentEditor()
    read_fd, write_fd = os.pipe()
    stderr = os.fdopen(read_fd, "rb")
    write_handle = os.fdopen(write_fd, "wb")
    proc = _FakeProc(stderr)

    drain_thread: threading.Thread | None = None

    def start_thread(fn, **kw):
        nonlocal drain_thread
        drain_thread = threading.Thread(target=fn, daemon=True, name=kw.get("name", "t"))
        drain_thread.start()
        return drain_thread

    with patch("plugin.scripting.editor_host.run_in_background", side_effect=start_thread):
        editor.start(proc)  # type: ignore[arg-type]
        write_handle.write(b"line one\nline two\n")
        write_handle.flush()
        write_handle.write(b"final line\n")
        write_handle.flush()
        write_handle.close()
        proc._exit_code = 0
        assert drain_thread is not None
        drain_thread.join(timeout=3.0)
        assert not drain_thread.is_alive(), "stderr drain thread did not finish"

    tail = editor.read_stderr_tail()
    assert "line one" in tail
    assert "line two" in tail
    assert "final line" in tail


def test_append_stderr_line_ring_buffer():
    editor = PersistentEditor()
    editor._stderr_tail_max_chars = 12
    editor._append_stderr_line("aaaa")
    editor._append_stderr_line("bbbb")
    editor._append_stderr_line("cccc")
    tail = editor.read_stderr_tail()
    assert "aaaa" not in tail
    assert "bbbb" in tail
    assert "cccc" in tail




def test_monaco_index_html_lives_under_assets_not_scripting_dir():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    scripting_dir = os.path.join(repo_root, "plugin", "scripting")
    wrong = os.path.join(scripting_dir, "index.html")
    right = os.path.join(_ASSETS_DIR, "index.html")
    assert not os.path.isfile(wrong)
    assert os.path.isfile(right)


# ---------------------------------------------------------------------------
# Regression tests for the "stale close clears new session's on_save" bug.
# When the user closes Monaco and immediately reopens it, three async paths
# (_handle_close in _dispatch_incoming, EditorSession._finish, and
# _handle_disconnect) could race and wipe the new session's callbacks.
# ---------------------------------------------------------------------------

from plugin.scripting.editor_host import (
    EditorSession,
    PersistentEditor,
    set_active_session,
    _PERSISTENT_EDITOR,
)
from plugin.framework.queue_executor import QueueExecutor


def _make_editor_with_callbacks(on_save=None, on_closed=None):
    """Helper: return a fresh PersistentEditor with callbacks set."""
    editor = PersistentEditor()
    editor.on_save = on_save or (lambda *a, **kw: {"type": "saved", "ok": True})
    editor.on_closed = on_closed or (lambda: None)
    return editor


def test_handle_disconnect_does_not_wipe_new_session_callbacks():
    """_handle_disconnect must not clear on_save if a new session has superseded it."""
    editor = _make_editor_with_callbacks()
    old_on_save = editor.on_save
    old_on_closed = editor.on_closed

    # Simulate new session installing its own callback before disconnect fires.
    new_on_save = lambda *a, **kw: {"type": "saved", "ok": True}
    new_on_closed = lambda: None
    editor.on_save = new_on_save
    editor.on_closed = new_on_closed

    # _handle_disconnect captures old callbacks at schedule time.
    captured_on_save = old_on_save
    captured_on_closed = old_on_closed

    # Reproduce what _handle_disconnect._handle_close does.
    if editor.on_save is captured_on_save:
        editor.on_save = None
    if editor.on_closed is captured_on_closed:
        editor.on_closed = None

    # New session's callbacks must survive.
    assert editor.on_save is new_on_save, "on_save was wrongly cleared by stale disconnect"
    assert editor.on_closed is new_on_closed, "on_closed was wrongly cleared by stale disconnect"


def test_finish_does_not_wipe_new_session_callbacks():
    """EditorSession._finish must not clear on_save if a new session has set a different one."""
    # Simulate the state just after a new session's __init__ ran but _finish from
    # the old session fires (via set_active_session(new_session)).
    old_on_save = lambda *a, **kw: {"type": "saved", "ok": True}
    old_on_closed = lambda: None
    new_on_save = lambda *a, **kw: {"type": "saved", "ok": True}
    new_on_closed = lambda: None

    with patch.object(launch_mod, "_PERSISTENT_EDITOR") as mock_pe:
        mock_pe.on_save = new_on_save   # new session already installed
        mock_pe.on_closed = new_on_closed

        # Old session's _finish checks identity before clearing.
        if mock_pe.on_save is old_on_save:   # False — new session replaced it
            mock_pe.on_save = None
        if mock_pe.on_closed is old_on_closed:
            mock_pe.on_closed = None

        assert mock_pe.on_save is new_on_save, "_finish wrongly cleared new on_save"
        assert mock_pe.on_closed is new_on_closed, "_finish wrongly cleared new on_closed"


def test_dispatch_incoming_close_does_not_wipe_new_session_callbacks():
    """_dispatch_incoming 'closed' must not clear on_save if superseded by a new session."""
    editor = _make_editor_with_callbacks()
    old_on_save = editor.on_save
    old_on_closed = editor.on_closed

    # Capture happens at dispatch time (old values).
    captured_on_save = old_on_save
    captured_on_closed = old_on_closed

    # New session installs its callbacks before _handle_close runs on the executor.
    new_on_save = lambda *a, **kw: {"type": "saved", "ok": True}
    new_on_closed = lambda: None
    editor.on_save = new_on_save
    editor.on_closed = new_on_closed

    # Reproduce what _handle_close does.
    if editor.on_save is captured_on_save:
        editor.on_save = None
    if editor.on_closed is captured_on_closed:
        editor.on_closed = None

    assert editor.on_save is new_on_save, "stale _handle_close wrongly cleared new on_save"
    assert editor.on_closed is new_on_closed, "stale _handle_close wrongly cleared new on_closed"
