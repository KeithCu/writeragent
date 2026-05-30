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
    assert sent_messages == [{"type": "load", "code": "print(1)"}]
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
    session.send.assert_called_once_with({"type": "load", "mode": "run_script"})
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


def test_monaco_vs_pruned_for_python_only_editor():
    vs = os.path.join(_ASSETS_DIR, "vs")
    assert os.path.isdir(os.path.join(vs, "basic-languages", "python"))
    assert not os.path.isdir(os.path.join(vs, "language"))
    assert not os.path.isfile(os.path.join(vs, "nls.messages.de.js"))
    assert not os.path.isdir(os.path.join(vs, "basic-languages", "typescript"))


def test_monaco_index_html_lives_under_assets_not_scripting_dir():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    scripting_dir = os.path.join(repo_root, "plugin", "scripting")
    wrong = os.path.join(scripting_dir, "index.html")
    right = os.path.join(_ASSETS_DIR, "index.html")
    assert not os.path.isfile(wrong)
    assert os.path.isfile(right)
