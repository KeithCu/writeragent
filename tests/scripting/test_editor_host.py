# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Monaco editor host (spawn, bridge, session launch)."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.managed_subprocess import _instances, _unregister_instance
from plugin.framework.send_cancellation import SendCancelled
from plugin.scripting import editor_host as launch_mod
from plugin.scripting.document_scripts import SCRIPT_PICKER_MESSAGE_TYPES
from plugin.scripting.editor_host import PersistentEditor
from plugin.scripting.editor_ipc import CHILD_TO_PARENT_TYPES


def test_editor_save_timeout_clears_the_token_and_reports():
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.call.side_effect = TimeoutError()
    state = launch_mod.EditorSessionState(session_id="s1", mode="calc_cell", target={"cell": "A1"})
    state.pending_load = {"code": "y = 2"}
    state.pending_on_save = MagicMock()
    state.pending_on_closed = MagicMock()
    pe.register_session(state)
    sent: list[dict] = []

    def _send(message, *, session=None):
        sent.append(message)

    pe.send = _send  # type: ignore[method-assign]
    with patch("plugin.scripting.editor_host.python_exec_timeout", return_value=30):
        pe._dispatch_incoming({"type": "save", "code": "x = 1", "session_id": "s1"})
    assert pe._save_token is None
    assert state.pending_load is None
    assert state.pending_on_save is None
    assert state.pending_on_closed is None
    assert sent and "timed out" in sent[0]["message"]
    assert pe.executor.call.call_args.kwargs["timeout"] == 35.0
    assert pe.executor.call.call_args.kwargs["bound_scope"] is None


def test_editor_script_picker_uses_marshal_timeout():
    """A hung script picker must use the same UI deadline as save and close."""
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.call.side_effect = TimeoutError()
    sent: list[dict] = []

    def _send(message, *, session=None):
        sent.append(message)

    pe.send = _send  # type: ignore[method-assign]
    with patch("plugin.scripting.editor_host.python_exec_timeout", return_value=30):
        pe._dispatch_incoming({"type": "request_scripts"})
    assert pe.executor.call.call_args.kwargs["timeout"] == 35.0
    assert pe.executor.call.call_args.kwargs["bound_scope"] is None
    assert sent and "timed out" in sent[0]["message"]


def test_editor_script_picker_exception_sends_error_frame():
    """A picker failure must be an error frame, not a dead reader."""
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.call.side_effect = lambda fn, *args, **kwargs: fn()
    sent: list[dict] = []

    def _send(message, *, session=None):
        sent.append(dict(message))

    pe.send = _send  # type: ignore[method-assign]
    with patch(
        "plugin.scripting.editor_host.handle_editor_script_message",
        side_effect=ValueError("payload exceeds 16MB"),
    ):
        pe._dispatch_incoming({"type": "request_scripts"})
    assert sent and sent[0]["type"] == "error"
    assert "payload exceeds 16MB" in sent[0]["message"]
    assert "ValueError" in sent[0]["traceback"]


def test_editor_script_picker_error_send_failure_stays_in_dispatch():
    """If the error frame itself cannot be sent, the reader must still survive."""
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.call.side_effect = lambda fn, *args, **kwargs: fn()

    def _send(message, *, session=None):
        raise ValueError("payload exceeds 16MB")

    pe.send = _send  # type: ignore[method-assign]
    with patch(
        "plugin.scripting.editor_host.handle_editor_script_message",
        side_effect=RuntimeError("document disposed"),
    ):
        pe._dispatch_incoming({"type": "request_scripts"})
    assert pe.executor.call.called


def test_editor_save_exception_sends_error_frame():
    """A save failure must still be reported when the error frame can be sent."""
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.call.side_effect = lambda fn, *args, **kwargs: fn()
    state = launch_mod.EditorSessionState(
        session_id="s1",
        mode="calc_cell",
        target={"cell": "A1"},
        on_save=MagicMock(side_effect=RuntimeError("document disposed")),
    )
    pending_load = {"code": "y = 2"}
    pending_on_save = MagicMock()
    pending_on_closed = MagicMock()
    state.pending_load = pending_load
    state.pending_on_save = pending_on_save
    state.pending_on_closed = pending_on_closed
    pe.register_session(state)
    sent: list[dict] = []

    def _send(message, *, session=None):
        sent.append(dict(message))

    pe.send = _send  # type: ignore[method-assign]
    pe._dispatch_incoming({"type": "save", "code": "x = 1", "session_id": "s1"})
    assert sent and sent[0]["type"] == "error"
    assert "document disposed" in sent[0]["message"]
    assert "RuntimeError" in sent[0]["traceback"]
    assert state.pending_load is pending_load
    assert state.pending_on_save is pending_on_save
    assert state.pending_on_closed is pending_on_closed


def test_editor_save_error_send_failure_stays_in_dispatch():
    """A failed save error frame must not escape and tear down the reader."""
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.call.side_effect = lambda fn, *args, **kwargs: fn()
    pe.register_session(
        launch_mod.EditorSessionState(
            session_id="s1",
            mode="calc_cell",
            target={"cell": "A1"},
            on_save=MagicMock(side_effect=RuntimeError("document disposed")),
        )
    )

    def _send(message, *, session=None):
        raise ValueError("payload exceeds 16MB")

    pe.send = _send  # type: ignore[method-assign]
    pe._dispatch_incoming({"type": "save", "code": "x = 1", "session_id": "s1"})
    assert pe.executor.call.called


def test_editor_script_picker_timeout_send_failure_stays_in_dispatch():
    """If the timeout error frame fails to send, do not raise out of dispatch."""
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.call.side_effect = TimeoutError()

    def _send(message, *, session=None):
        raise BrokenPipeError("pipe closed")

    pe.send = _send  # type: ignore[method-assign]
    pe._dispatch_incoming({"type": "request_scripts"})


def test_editor_save_timeout_send_failure_stays_in_dispatch():
    """If the save timeout frame fails to send, do not raise out of dispatch."""
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.call.side_effect = TimeoutError()
    pe.register_session(
        launch_mod.EditorSessionState(
            session_id="s1",
            mode="calc_cell",
            target={"cell": "A1"},
            on_save=MagicMock(),
        )
    )

    def _send(message, *, session=None):
        raise BrokenPipeError("pipe closed")

    pe.send = _send  # type: ignore[method-assign]
    pe._dispatch_incoming({"type": "save", "code": "x = 1", "session_id": "s1"})


def test_editor_save_timeout_prevents_late_save():
    """A save that completes on the UI thread after timeout must not invoke on_save."""
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    queued_task = []
    pe.executor.call.side_effect = lambda fn, *args, **kwargs: (queued_task.append(fn), (_ for _ in ()).throw(TimeoutError()))[1]
    mock_on_save = MagicMock()
    state = launch_mod.EditorSessionState(
        session_id="s1",
        mode="calc_cell",
        target={"cell": "A1"},
        on_save=mock_on_save,
    )
    state.dirty = True
    pe.register_session(state)
    pe.send = MagicMock()

    pe._dispatch_incoming({"type": "save", "code": "x = 1", "session_id": "s1"})
    assert pe._save_token is None
    assert len(queued_task) == 1

    # Simulate UI thread running the queued task after timeout expired:
    queued_task[0]()
    mock_on_save.assert_not_called()


def test_editor_save_clears_session_dirty():
    """Successful save must set session.dirty to False."""
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.call.side_effect = lambda fn, *args, **kwargs: fn()
    state = launch_mod.EditorSessionState(
        session_id="s1",
        mode="calc_cell",
        target={"cell": "A1"},
        on_save=MagicMock(return_value={"type": "save_result", "ok": True}),
    )
    state.dirty = True
    pe.register_session(state)
    pe.send = MagicMock()

    pe._dispatch_incoming({"type": "save", "code": "x = 1", "session_id": "s1"})
    assert state.dirty is False


def test_send_save_releases_save_lock_before_terminate():
    _reset_persistent_editor()
    pe = launch_mod._PERSISTENT_EDITOR
    acquired: list[bool] = []

    def on_closed() -> None:
        ok = pe._save_lock.acquire(timeout=0.5)
        acquired.append(ok)
        if ok:
            pe._save_lock.release()

    def on_save(*_args: object, **_kwargs: object) -> dict:
        return {"type": "saved", "ok": True}

    state = launch_mod.EditorSessionState(
        session_id="s1",
        mode="calc_cell",
        target={"cell": "A1"},
        on_save=on_save,
        on_closed=on_closed,
    )
    state.dirty = True
    pe.register_session(state)
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.call.side_effect = lambda fn, *args, **kwargs: fn()

    def fake_send(message: dict, *, session=None) -> None:
        launch_mod.terminate_persistent_editor()

    pe.send = fake_send  # type: ignore[method-assign]

    try:
        pe._dispatch_incoming({"type": "save", "code": "x = 1", "session_id": "s1"})
        assert acquired == [True]
    finally:
        _reset_persistent_editor()


def test_launch_monaco_editor_reuses_running_process():
    ctx = MagicMock()
    sent_messages: list[dict] = []
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    pe = launch_mod._PERSISTENT_EDITOR
    pe.sessions.clear()
    pe.focused_id = None

    def fake_send(msg: dict) -> None:
        sent_messages.append(msg)

    pe.send = fake_send  # type: ignore[method-assign]
    try:
        with patch.object(type(pe), "is_running", new=property(lambda self: True)):
            pe._proc = mock_proc
            ok = launch_mod.launch_monaco_editor(
                ctx,
                exe="/venv/bin/python",
                load_message={"type": "load", "code": "print(1)", "mode": "calc_cell", "cell_address": "A1"},
                on_save=MagicMock(),
            )

        assert ok is True
        assert sent_messages[0]["type"] == "load"
        assert sent_messages[0]["code"] == "print(1)"
        assert sent_messages[0]["session_id"]
        assert sent_messages[0]["mode"] == "calc_cell"
        assert sent_messages[0]["target"]["cell_address"] == "A1"
        assert "theme" in sent_messages[0]
        assert "ui" in sent_messages[0]
        assert sent_messages[0]["ui"]["ready"]
        assert sent_messages[0]["theme"]["monaco"] in ("vs", "vs-dark")
    finally:
        _reset_persistent_editor()


def test_launch_monaco_editor_spawns_when_not_running():
    ctx = MagicMock()
    mock_proc = MagicMock()
    mock_doc = MagicMock()
    pe = launch_mod._PERSISTENT_EDITOR
    pe.sessions.clear()
    pe.focused_id = None
    pe.send = MagicMock()

    try:
        with patch.object(type(pe), "is_running", new=property(lambda self: self._proc is mock_proc)):
            with patch.object(launch_mod, "spawn_editor_process", return_value=mock_proc):
                with patch.object(launch_mod, "run_in_background") as mock_bg:
                    with patch.object(pe, "wait_for_ready", return_value=True):
                        with patch.object(pe, "start", side_effect=lambda p: setattr(pe, "_proc", p)) as mock_start:
                            load_message = {"type": "load", "mode": "run_script", "run_script_doc": mock_doc, "script_name": "demo"}
                            ok = launch_mod.launch_monaco_editor(
                                ctx,
                                exe="/venv/bin/python",
                                load_message=load_message,
                                on_save=MagicMock(),
                            )

        assert ok is True
        mock_bg.assert_called_once_with(launch_mod.warm_venv_worker, name="warm-venv-worker", dedicated=False)
        assert pe.run_script_doc is mock_doc
        mock_start.assert_called_once_with(mock_proc)
        sent = pe.send.call_args[0][0]
        assert sent["type"] == "load"
        assert sent["mode"] == "run_script"
        assert sent["session_id"]
        assert sent["target"]["script_name"] == "demo"
        assert "run_script_doc" not in sent
        assert "theme" in sent
        assert "ui" in sent
        assert sent["ui"]["script_label"]
        assert load_message["run_script_doc"] is mock_doc
    finally:
        _reset_persistent_editor()


def test_monaco_editor_available_false_without_venv():
    with patch.object(launch_mod, "resolve_editor_python", return_value=(None, "missing venv")):
        exe, ok = launch_mod.monaco_editor_available()
    assert exe is None
    assert ok is False


def test_probe_webview_import_timeout_returns_diagnostic():
    """A probe timeout must return the traceback Monaco shows, not raise under deal."""
    exe = "/tmp/writeragent-probe-timeout-python"
    launch_mod.reset_webview_probe_state_for_tests()
    timeout = subprocess.TimeoutExpired(cmd=[exe, "-c", "import webview"], timeout=30)
    try:
        with patch("plugin.framework.managed_subprocess.subprocess.run", side_effect=timeout) as run:
            ok, detail = launch_mod.probe_webview_import(exe, apply_generation=0)
            ok2, detail2 = launch_mod.probe_webview_import(exe, apply_generation=0)
        assert ok is False
        assert ok2 is False
        assert detail2 == detail
        assert "TimeoutExpired" in detail
        assert exe not in launch_mod._PROBE_CACHE
        assert exe in launch_mod._PROBE_FAILURE_CACHE
        assert run.call_count == 1
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_probe_webview_import_oserror_returns_diagnostic():
    exe = "/tmp/writeragent-probe-oserror-python"
    launch_mod.reset_webview_probe_state_for_tests()
    try:
        with patch("plugin.framework.managed_subprocess.subprocess.run", side_effect=OSError("boom")):
            ok, detail = launch_mod.probe_webview_import(exe, apply_generation=0)
        assert ok is False
        assert "OSError" in detail
        assert "boom" in detail
        assert exe not in launch_mod._PROBE_CACHE
        assert exe in launch_mod._PROBE_FAILURE_CACHE
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_probe_webview_import_failure_expires_after_ttl():
    """A cached failure must be re-probed once the short TTL has passed."""
    exe = "/tmp/writeragent-probe-ttl-python"
    launch_mod.reset_webview_probe_state_for_tests()
    calls = {"n": 0}

    def boom(*_args, **_kwargs):
        calls["n"] += 1
        raise OSError("boom")

    try:
        with patch("plugin.framework.managed_subprocess.subprocess.run", side_effect=boom):
            with patch("plugin.scripting.editor_host.time.monotonic", return_value=1000.0):
                first_ok, _first_detail = launch_mod.probe_webview_import(exe, apply_generation=0)
                second_ok, _second_detail = launch_mod.probe_webview_import(exe, apply_generation=0)
            assert first_ok is False
            assert second_ok is False
            assert calls["n"] == 1
            with patch(
                "plugin.scripting.editor_host.time.monotonic",
                return_value=1000.0 + launch_mod._PROBE_FAILURE_TTL_SEC,
            ):
                launch_mod.probe_webview_import(exe, apply_generation=0)
            assert calls["n"] == 2
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_probe_webview_import_nonzero_exit_is_cached_failure():
    exe = "/tmp/writeragent-probe-exit-python"
    launch_mod.reset_webview_probe_state_for_tests()
    completed = subprocess.CompletedProcess(args=[exe], returncode=1, stdout="", stderr="no webview")
    try:
        with patch("plugin.framework.managed_subprocess.subprocess.run", return_value=completed) as run:
            ok, detail = launch_mod.probe_webview_import(exe, apply_generation=0)
            ok2, _detail2 = launch_mod.probe_webview_import(exe, apply_generation=0)
        assert ok is False
        assert ok2 is False
        assert "no webview" in detail
        assert exe not in launch_mod._PROBE_CACHE
        assert run.call_count == 1
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_monaco_editor_available_false_when_webview_missing():
    exe = "/venv/bin/python"
    try:
        launch_mod._PROBE_FAILURE_CACHE[exe] = (time.monotonic() + 30.0, (False, "no webview"))
        with patch.object(launch_mod, "resolve_editor_python", return_value=(exe, "")):
            with patch.object(launch_mod.ManagedSubprocess, "run") as mock_run:
                returned_exe, ok = launch_mod.monaco_editor_available()
                mock_run.assert_not_called()
        assert returned_exe == exe
        assert ok is False
    finally:
        launch_mod._PROBE_FAILURE_CACHE.pop(exe, None)


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


@pytest.mark.parametrize(
    ("max_chars", "lines", "expected_present", "expected_absent"),
    [
        (12, ["aaaa", "bbbb", "cccc"], ["bbbb", "cccc"], ["aaaa"]),
        (50, [f"line-{i:02d}-xxxx" for i in range(40)], ["line-39-xxxx"], ["line-00-xxxx"]),
    ],
)
def test_append_stderr_line_ring_buffer_respects_budget(
    max_chars: int,
    lines: list[str],
    expected_present: list[str],
    expected_absent: list[str],
) -> None:
    editor = PersistentEditor()
    editor._stderr_tail_max_chars = max_chars
    for line in lines:
        editor._append_stderr_line(line, editor._stderr_generation)
    tail = editor.read_stderr_tail()
    assert len(tail) <= max_chars
    for token in expected_present:
        assert token in tail
    for token in expected_absent:
        assert token not in tail


def test_start_clears_stderr_char_count():
    editor = PersistentEditor()
    editor._append_stderr_line("hello", editor._stderr_generation)
    assert editor._stderr_tail_chars == len("hello") + 1
    proc = _FakeProc(stderr=None)
    with patch("plugin.scripting.editor_host.run_in_background", return_value=MagicMock()):
        editor.start(proc)  # type: ignore[arg-type]
    assert editor._stderr_tail_chars == 0
    assert editor.read_stderr_tail() == ""


def test_save_routes_to_matching_session_only():
    from plugin.scripting.editor_host import EditorSessionState

    editor = PersistentEditor()
    seen: list[str] = []

    def save_a(*_a, **_k):
        seen.append("a")
        return {"type": "saved", "ok": True}

    def save_b(*_a, **_k):
        seen.append("b")
        return {"type": "saved", "ok": True}

    editor.register_session(EditorSessionState("sid-a", "calc_cell", {"cell_address": "A1"}, on_save=save_a))
    editor.register_session(EditorSessionState("sid-b", "calc_cell", {"cell_address": "B1"}, on_save=save_b))
    editor.executor = MagicMock()
    editor.executor.call.side_effect = lambda fn, *args, **kwargs: fn()
    sent: list[tuple] = []

    def fake_send(msg, session=None):
        sent.append((msg, session))

    editor.send = fake_send  # type: ignore[method-assign]

    editor._dispatch_incoming({"type": "save", "session_id": "sid-b", "code": "x", "save_as_plain": False})
    assert seen == ["b"]
    assert sent[0][0]["type"] == "saved"
    assert sent[0][1] is not None
    assert sent[0][1].session_id == "sid-b"


def test_unknown_session_save_is_ignored():
    from plugin.scripting.editor_host import EditorSessionState

    editor = PersistentEditor()
    called = []
    editor.register_session(
        EditorSessionState("sid-a", "calc_cell", {"cell_address": "A1"}, on_save=lambda *_a, **_k: called.append("a") or {"type": "saved", "ok": True})
    )
    editor.executor = MagicMock()
    editor.executor.call.side_effect = lambda fn, *args, **kwargs: fn()
    editor._dispatch_incoming({"type": "save", "session_id": "nope", "code": "x"})
    assert called == []


def test_same_target_reuses_session_id():
    pe = launch_mod._PERSISTENT_EDITOR
    pe.sessions.clear()
    pe.focused_id = None
    on_save = MagicMock(return_value={"type": "saved", "ok": True})
    load = {"type": "load", "mode": "calc_cell", "cell_address": "A1", "code": "1"}
    a, stamped_a = launch_mod._register_load_session(load, on_save, lambda: None)
    b, stamped_b = launch_mod._register_load_session({"type": "load", "mode": "calc_cell", "cell_address": "A1", "code": "2"}, on_save, lambda: None)
    assert a.session_id == b.session_id
    assert stamped_a["session_id"] == stamped_b["session_id"]
    assert len(pe.sessions) == 1
    pe.sessions.clear()
    pe.focused_id = None


def test_same_target_reuse_clears_pending_save_then_load():
    pe = launch_mod._PERSISTENT_EDITOR
    pe.sessions.clear()
    pe.focused_id = None
    on_save = MagicMock(return_value={"type": "saved", "ok": True})
    try:
        state, _stamped = launch_mod._register_load_session(
            {"type": "load", "mode": "calc_cell", "cell_address": "A1", "code": "1"},
            on_save,
            lambda: None,
        )
        state.dirty = True
        state.extra["replace_confirmed"] = True
        state.pending_load = {"type": "load", "code": "stale"}
        state.pending_on_save = MagicMock()
        state.pending_on_closed = MagicMock()
        reused, _stamped_again = launch_mod._register_load_session(
            {"type": "load", "mode": "calc_cell", "cell_address": "A1", "code": "2"},
            on_save,
            lambda: None,
        )
        assert reused is state
        assert reused.dirty is False
        assert reused.pending_load is None
        assert reused.pending_on_save is None
        assert reused.pending_on_closed is None
    finally:
        pe.sessions.clear()
        pe.focused_id = None


def _reset_persistent_editor() -> None:
    pe = launch_mod._PERSISTENT_EDITOR
    pe._save_token = None
    pe.sessions.clear()
    pe.focused_id = None
    pe._proc = None
    pe.run_script_doc = None
    pe.run_script_doc_url = None
    # Tests assign pe.send. That instance attribute would shadow the real
    # method for every later test that shares this singleton.
    if "send" in pe.__dict__:
        del pe.send


def test_monaco_session_needs_flush_any_mode():
    pe = launch_mod._PERSISTENT_EDITOR
    _reset_persistent_editor()
    proc = MagicMock()
    proc.poll.return_value = None
    pe._proc = proc
    try:
        for mode in ("calc_cell", "run_script", "init_script", "latex"):
            pe.sessions.clear()
            state = launch_mod.EditorSessionState("sid", mode, {"resource": "buf"})
            state.dirty = True
            pe.register_session(state)
            assert launch_mod.monaco_session_needs_flush() is True
            state.dirty = False
            assert launch_mod.monaco_session_needs_flush() is False
    finally:
        _reset_persistent_editor()


def test_register_refuses_dirty_buffer_until_discard():
    pe = launch_mod._PERSISTENT_EDITOR
    _reset_persistent_editor()
    proc = MagicMock()
    proc.poll.return_value = None
    pe._proc = proc
    on_save = MagicMock(return_value={"type": "saved", "ok": True})
    try:
        state, _stamped = launch_mod._register_load_session(
            {"type": "load", "mode": "run_script", "script_name": "Demo"},
            on_save,
            lambda: None,
        )
        state.dirty = True
        state.pending_load = {"type": "load", "code": "keep"}
        try:
            launch_mod._register_load_session(
                {"type": "load", "mode": "calc_cell", "cell_address": "B2", "code": "new"},
                on_save,
                lambda: None,
            )
            raised = False
        except launch_mod.DirtyBufferError:
            raised = True
        assert raised is True
        assert pe.focused() is state
        assert state.dirty is True
        assert state.pending_load == {"type": "load", "code": "keep"}
        assert len(pe.sessions) == 1
    finally:
        _reset_persistent_editor()


def test_launch_cancel_keeps_dirty_run_script_buffer():
    """Opening a cell must not drop unsaved Run Script edits when the user cancels."""
    pe = launch_mod._PERSISTENT_EDITOR
    _reset_persistent_editor()
    proc = MagicMock()
    proc.poll.return_value = None
    pe._proc = proc
    state = launch_mod.EditorSessionState(
        "sid-run",
        "run_script",
        {"script_name": "Demo", "resource": "run_script"},
        on_save=MagicMock(),
    )
    state.dirty = True
    pe.register_session(state)
    sent: list[dict] = []

    def _send(message: dict, session: object = None) -> None:
        sent.append(message)

    pe.send = _send  # type: ignore[method-assign]
    try:
        with patch.object(launch_mod, "confirm_unsaved_monaco_edit", return_value="cancel") as confirm:
            ok = launch_mod.launch_monaco_editor(
                MagicMock(),
                exe="/venv/bin/python",
                load_message={"type": "load", "mode": "calc_cell", "cell_address": "A1", "code": "x = 1"},
                on_save=MagicMock(),
            )
        confirm.assert_called_once()
        assert "Demo" in confirm.call_args.args[1]
        assert ok is True
        assert sent == []
        assert pe.focused() is state
        assert state.dirty is True
        assert "replace_confirmed" not in state.extra
    finally:
        _reset_persistent_editor()


def test_launch_save_queues_request_save_for_dirty_cell():
    """Run Python Script must flush a dirty cell instead of replacing it."""
    pe = launch_mod._PERSISTENT_EDITOR
    _reset_persistent_editor()
    proc = MagicMock()
    proc.poll.return_value = None
    pe._proc = proc
    state = launch_mod.EditorSessionState(
        "sid-cell",
        "calc_cell",
        {"cell_address": "C3"},
        on_save=MagicMock(),
    )
    state.dirty = True
    pe.register_session(state)
    sent: list[dict] = []

    def _send(message: dict, session: object = None) -> None:
        sent.append(dict(message))

    pe.send = _send  # type: ignore[method-assign]
    try:
        with patch.object(launch_mod, "confirm_unsaved_monaco_edit", return_value="save"):
            ok = launch_mod.launch_monaco_editor(
                MagicMock(),
                exe="/venv/bin/python",
                load_message={
                    "type": "load",
                    "mode": "run_script",
                    "script_name": "Demo",
                    "code": "print(1)",
                    "run_script_doc": object(),
                },
                on_save=MagicMock(),
            )
        assert ok is True
        assert [msg["type"] for msg in sent] == ["request_save"]
        assert state.pending_load is not None
        assert state.pending_load["mode"] == "run_script"
        assert "run_script_doc" in state.pending_load
        assert state.dirty is True
        assert pe.focused() is state
        assert pe.run_script_doc is None
    finally:
        _reset_persistent_editor()


def test_launch_discard_replaces_dirty_init_script():
    pe = launch_mod._PERSISTENT_EDITOR
    _reset_persistent_editor()
    proc = MagicMock()
    proc.poll.return_value = None
    pe._proc = proc
    closed: list[str] = []
    state = launch_mod.EditorSessionState(
        "sid-init",
        "init_script",
        {"resource": "init"},
        on_save=MagicMock(),
        on_closed=lambda: closed.append("init"),
    )
    state.dirty = True
    pe.register_session(state)
    pe.send = MagicMock()
    try:
        with patch.object(launch_mod, "confirm_unsaved_monaco_edit", return_value="discard"):
            ok = launch_mod.launch_monaco_editor(
                MagicMock(),
                exe="/venv/bin/python",
                load_message={"type": "load", "mode": "latex", "resource": "insert", "code": "x"},
                on_save=MagicMock(),
            )
        assert ok is True
        assert closed == ["init"]
        assert "sid-init" not in pe.sessions
        sent = pe.send.call_args[0][0]
        assert sent["mode"] == "latex"
        assert sent["type"] == "load"
    finally:
        _reset_persistent_editor()


def test_launch_keeps_dirty_session_while_child_runs():
    pe = launch_mod._PERSISTENT_EDITOR
    _reset_persistent_editor()
    proc = MagicMock()
    proc.poll.return_value = None
    pe._proc = proc

    def on_save(*_args: object, **_kwargs: object) -> dict:
        return {"type": "saved", "ok": True}

    state = launch_mod.EditorSessionState("sid", "latex", {"resource": "F1"}, on_save=on_save)
    state.dirty = True
    pe.register_session(state)
    try:
        with patch.object(launch_mod, "confirm_unsaved_monaco_edit", return_value="cancel"):
            launch_mod.launch_monaco_editor(
                MagicMock(),
                exe="/venv/bin/python",
                load_message={"type": "load", "mode": "calc_cell", "cell_address": "B2", "code": "y"},
                on_save=MagicMock(),
            )
        assert pe.lookup("sid") is state
        assert state.dirty is True
    finally:
        _reset_persistent_editor()


def test_launch_while_child_runs_preserves_clean_session_for_same_target():
    """A running child skips the dead-child purge and reuses the existing session for the same target."""
    pe = launch_mod._PERSISTENT_EDITOR
    _reset_persistent_editor()
    proc = MagicMock()
    proc.poll.return_value = None
    pe._proc = proc

    closed: list[str] = []
    state = launch_mod.EditorSessionState(
        "sid",
        "latex",
        {"resource": "F1"},
        on_save=MagicMock(),
        on_closed=lambda: closed.append("sid"),
    )
    pe.register_session(state)
    sent_messages: list[dict] = []
    pe.send = MagicMock(side_effect=sent_messages.append)
    try:
        ok = launch_mod.launch_monaco_editor(
            MagicMock(),
            exe="/venv/bin/python",
            load_message={"type": "load", "mode": "latex", "resource": "F1", "code": "y"},
            on_save=MagicMock(),
        )
        assert ok is True
        assert closed == []
        assert pe.lookup("sid") is state
        assert len(sent_messages) == 1
        assert sent_messages[0]["session_id"] == "sid"
    finally:
        _reset_persistent_editor()


def test_deferred_run_sends_result_later_and_keeps_pending_load():
    """Monaco Run must not block the save handler, and must not consume a queued switch."""
    editor = PersistentEditor()
    delivered: dict = {}

    def on_save(code: str, save_as_plain: bool, data_binding: str | None, action: str):
        del code, save_as_plain, data_binding, action

        def _start(deliver):
            delivered["deliver"] = deliver

        return launch_mod.DeferredEditorResult(_start)

    state = launch_mod.EditorSessionState("sid", "run_script", {"script_name": "Demo"}, on_save=on_save)
    state.dirty = True
    state.pending_load = {"type": "load", "mode": "calc_cell", "code": "next"}
    editor.register_session(state)
    editor.executor = MagicMock()
    editor.executor.call.side_effect = lambda fn, *args, **kwargs: fn()
    sent: list[dict] = []

    def _send(message, *, session=None):
        sent.append(dict(message))

    editor.send = _send  # type: ignore[method-assign]
    editor._dispatch_incoming({"type": "save", "session_id": "sid", "code": "print(1)", "action": "run"})
    assert sent == []
    assert state.dirty is True
    assert state.pending_load is not None
    assert "deliver" in delivered
    delivered["deliver"]({"type": "saved", "ok": True, "status_ok_text": "done"})
    assert sent[0]["type"] == "saved"
    assert sent[0]["status_ok_text"] == "done"
    assert state.dirty is False
    assert state.pending_load is not None
    assert "sid" in editor.sessions


def test_deferred_stale_deliver_does_not_clear_dirty():
    """A Run that finishes after a newer save must not mark the buffer clean."""
    editor = PersistentEditor()
    delivered: dict = {}

    def on_save(*_args: object, **_kwargs: object):
        def _start(deliver):
            delivered["deliver"] = deliver

        return launch_mod.DeferredEditorResult(_start)

    state = launch_mod.EditorSessionState("sid", "run_script", {"script_name": "Demo"}, on_save=on_save)
    state.dirty = True
    editor.register_session(state)
    editor.executor = MagicMock()
    editor.executor.call.side_effect = lambda fn, *args, **kwargs: fn()
    sent: list[dict] = []
    editor.send = lambda message, session=None: sent.append(dict(message))  # type: ignore[method-assign]
    editor._dispatch_incoming({"type": "save", "session_id": "sid", "code": "print(1)", "action": "run"})
    # A later save replaces the token. The late frame must be a no-op.
    editor._save_token = object()
    state.dirty = True
    delivered["deliver"]({"type": "saved", "ok": True, "status_ok_text": "late"})
    assert sent == []
    assert state.dirty is True


def test_deferred_start_failure_sends_error_and_keeps_pending_load():
    editor = PersistentEditor()

    def on_save(*_args: object, **_kwargs: object):
        def _start(_deliver):
            raise RuntimeError("venv missing")

        return launch_mod.DeferredEditorResult(_start)

    state = launch_mod.EditorSessionState("sid", "run_script", {"script_name": "Demo"}, on_save=on_save)
    state.dirty = True
    state.pending_load = {"type": "load", "mode": "calc_cell", "code": "next"}
    editor.register_session(state)
    editor.executor = MagicMock()
    editor.executor.call.side_effect = lambda fn, *args, **kwargs: fn()
    sent: list[dict] = []
    editor.send = lambda message, session=None: sent.append(dict(message))  # type: ignore[method-assign]
    editor._dispatch_incoming({"type": "save", "session_id": "sid", "code": "print(1)", "action": "run"})
    assert sent and sent[0]["type"] == "error"
    assert "venv missing" in sent[0]["message"]
    assert state.dirty is True
    assert state.pending_load is not None


def test_queue_save_send_failure_clears_pending():
    pe = launch_mod._PERSISTENT_EDITOR
    _reset_persistent_editor()
    proc = MagicMock()
    proc.poll.return_value = None
    pe._proc = proc
    state = launch_mod.EditorSessionState("sid", "calc_cell", {"cell_address": "A1"})
    state.dirty = True
    pe.register_session(state)

    def _send(_message: dict, session: object = None) -> None:
        del session
        raise RuntimeError("pipe closed")

    pe.send = _send  # type: ignore[method-assign]
    try:
        raised = False
        try:
            launch_mod.queue_save_then_load(
                {"type": "load", "mode": "run_script", "script_name": "Demo", "code": "print(1)"},
                MagicMock(),
                lambda: None,
            )
        except RuntimeError:
            raised = True
        assert raised is True
        assert state.pending_load is None
        assert state.pending_on_save is None
        assert state.pending_on_closed is None
        assert state.dirty is True
    finally:
        _reset_persistent_editor()


def test_dead_editor_does_not_block_a_dirty_replace():
    pe = launch_mod._PERSISTENT_EDITOR
    _reset_persistent_editor()
    proc = MagicMock()
    proc.poll.return_value = 1
    pe._proc = proc
    try:
        state, _stamped = launch_mod._register_load_session(
            {"type": "load", "mode": "run_script", "script_name": "Demo"},
            MagicMock(),
            lambda: None,
        )
        state.dirty = True
        replacement, _again = launch_mod._register_load_session(
            {"type": "load", "mode": "calc_cell", "cell_address": "B2", "code": "x = 1"},
            MagicMock(),
            lambda: None,
        )
        assert replacement is not state
        assert state.session_id not in pe.sessions
        assert pe.focused() is replacement
        assert replacement.mode == "calc_cell"
    finally:
        _reset_persistent_editor()


def test_different_target_replaces_focused_session():
    pe = launch_mod._PERSISTENT_EDITOR
    pe.sessions.clear()
    pe.focused_id = None
    closed: list[str] = []
    launch_mod._register_load_session(
        {"type": "load", "mode": "calc_cell", "cell_address": "A1"},
        MagicMock(),
        lambda: closed.append("a"),
    )
    launch_mod._register_load_session(
        {"type": "load", "mode": "calc_cell", "cell_address": "B1"},
        MagicMock(),
        lambda: None,
    )
    assert closed == ["a"]
    assert len(pe.sessions) == 1
    assert pe.focused().target["cell_address"] == "B1"  # type: ignore[union-attr]
    pe.sessions.clear()
    pe.focused_id = None


def test_target_switch_with_active_session_calls_on_closed():
    """Switching targets via _register_load_session calls on_closed on old session."""
    pe = launch_mod._PERSISTENT_EDITOR
    pe.sessions.clear()
    pe.focused_id = None
    closed: list[str] = []

    try:
        launch_mod._register_load_session(
            {"type": "load", "mode": "calc_cell", "cell_address": "A1"},
            MagicMock(),
            lambda: closed.append("session1_closed"),
        )

        launch_mod._register_load_session(
            {"type": "load", "mode": "calc_cell", "cell_address": "B1"},
            MagicMock(),
            lambda: closed.append("session2_closed"),
        )

        assert "session1_closed" in closed
        assert pe.focused().target["cell_address"] == "B1"  # type: ignore[union-attr]
    finally:
        _reset_persistent_editor()


def test_resolve_editor_python_missing_venv_mentions_settings():
    with patch("plugin.scripting.editor_host.get_config", return_value=""):
        exe, err = launch_mod.resolve_editor_python()
    assert exe is None
    assert "Settings → Python" in err
    assert "WriterAgent Settings" not in err


def test_launch_monaco_spawn_oserror_uses_product_display_name():
    ctx = MagicMock()
    with patch.object(launch_mod, "_PERSISTENT_EDITOR") as mock_persistent:
        mock_persistent.is_running = False
        with patch.object(launch_mod, "spawn_editor_process", side_effect=OSError("boom")):
            with patch("plugin.scripting.editor_host.msgbox_with_report") as box:
                with patch("plugin.scripting.editor_host.product_display_name", return_value="LibrePy"):
                    ok = launch_mod.launch_monaco_editor(
                        ctx,
                        exe="/venv/bin/python",
                        load_message={"type": "load", "code": "print(1)"},
                        on_save=MagicMock(),
                    )
    assert ok is False
    box.assert_called_once()
    assert box.call_args[0][1] == "LibrePy"
    assert box.call_args.kwargs.get("report_title") == "Python editor spawn failed"


def test_mode_switch_from_run_script_to_calc_cell_dispatches_save():
    from plugin.scripting.editor_host import EditorSessionState

    editor = PersistentEditor()
    cell_saved: list[dict] = []

    def on_cell_save(code: str, save_as_plain: bool, data_binding: str | None, action: str):
        cell_saved.append({"code": code, "plain": save_as_plain, "binding": data_binding, "action": action})
        return {"type": "saved", "ok": True, "status_ok_text": "Saved."}

    # Simulate run_script session registering then ending (closing)
    run_state = EditorSessionState("sid-run", "run_script", {"resource": "run_script"})
    editor.register_session(run_state)
    editor.end_session("sid-run", call_closed=True)

    # Now open calc_cell session
    cell_state = EditorSessionState("sid-cell", "calc_cell", {"cell_address": "A1"}, on_save=on_cell_save)
    editor.register_session(cell_state)
    editor.executor = MagicMock()
    editor.executor.call.side_effect = lambda fn, *args, **kwargs: fn()

    sent: list[tuple] = []
    editor.send = lambda msg, session=None: sent.append((msg, session))  # type: ignore[method-assign]

    # Dispatch incoming save from cell editor
    editor._dispatch_incoming({
        "type": "save",
        "session_id": "sid-cell",
        "code": "result = 42",
        "save_as_plain": False,
        "data_binding": "",
        "action": "cell_save",
    })

    assert len(cell_saved) == 1
    assert cell_saved[0]["code"] == "result = 42"
    assert cell_saved[0]["action"] == "cell_save"
    assert len(sent) == 1
    assert sent[0][0]["type"] == "saved"
    assert sent[0][0]["status_ok_text"] == "Saved."


def _reader_method_name() -> str:
    return "_read_incoming"


def test_reader_clean_eof_terminates_live_child():
    """Stdout EOF with the child still alive must not leave a reader-less process."""
    editor = PersistentEditor()
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdout = MagicMock()
    editor._proc = proc
    order: list[str] = []

    def _disconnect() -> None:
        order.append("disconnect")

    def _terminate() -> None:
        order.append("terminate")

    with patch.object(editor, "_handle_disconnect", side_effect=_disconnect):
        with patch.object(editor, "terminate", side_effect=_terminate):
            with patch.object(editor, _reader_method_name(), return_value=None):
                editor._read_loop()
    assert order == ["disconnect", "terminate"]


def test_reader_exception_terminates_live_child():
    editor = PersistentEditor()
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdout = MagicMock()
    editor._proc = proc
    with patch.object(editor, "_handle_disconnect"):
        with patch.object(editor, "terminate") as terminate:
            with patch.object(editor, _reader_method_name(), side_effect=ValueError("bad frame")):
                editor._read_loop()
    terminate.assert_called_once()


def test_reader_finished_leaves_exited_child():
    editor = PersistentEditor()
    proc = MagicMock()
    proc.poll.return_value = 0
    proc.stdout = MagicMock()
    editor._proc = proc
    with patch.object(editor, "_handle_disconnect") as disconnect:
        with patch.object(editor, "terminate") as terminate:
            with patch.object(editor, _reader_method_name(), return_value=None):
                editor._read_loop()
    disconnect.assert_called_once()
    terminate.assert_not_called()


def test_reader_finished_ignores_superseded_process():
    editor = PersistentEditor()
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdout = MagicMock()
    editor._proc = proc
    replacement = MagicMock()

    def _swap(_proc, _stdout) -> None:
        editor._proc = replacement

    with patch.object(editor, "_handle_disconnect") as disconnect:
        with patch.object(editor, "terminate") as terminate:
            with patch.object(editor, _reader_method_name(), side_effect=_swap):
                editor._read_loop()
    disconnect.assert_not_called()
    terminate.assert_not_called()
    assert editor._proc is replacement


def test_spawn_editor_process_scrubs_env_and_does_not_register(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    mock_popen = MagicMock()
    mock_popen.poll.return_value = None
    captured_calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured_calls.append((cmd, kwargs["env"]))
        return mock_popen

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)
    drain_mock = MagicMock(side_effect=AssertionError("drain"))
    monkeypatch.setattr(ms, "start_stderr_drain", drain_mock)
    monkeypatch.setattr("plugin.scripting.sandbox.detect_sandbox", lambda: "flatpak")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/lo/lib")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PYTHONHOME", "/usr/lib/libreoffice/program")

    instances_before = set(_instances)

    try:
        proc = launch_mod.spawn_editor_process("/venv/bin/python")
        assert proc is mock_popen
        assert len(captured_calls) == 1
        argv, env = captured_calls[0]
        assert argv[:2] == ["flatpak-spawn", "--host"]
        assert argv[-1] == launch_mod._EDITOR_MAIN
        assert env["WRITERAGENT_EDITOR_ASSETS"] == launch_mod._ASSETS_DIR
        assert env["LD_LIBRARY_PATH"] == "/opt/lo/lib"
        assert "OPENAI_API_KEY" not in env
        assert "PYTHONHOME" not in env
        assert _instances == instances_before
        drain_mock.assert_not_called()
    finally:
        for inst in list(_instances):
            if inst not in instances_before:
                _unregister_instance(inst)


def test_spawn_editor_process_oserror_is_not_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugin.framework.managed_subprocess as ms

    monkeypatch.setattr(ms.subprocess, "Popen", MagicMock(side_effect=OSError("exec failed")))
    instances_before = set(_instances)

    with pytest.raises(OSError, match=r"^exec failed$"):
        launch_mod.spawn_editor_process("/venv/bin/python")

    assert _instances == instances_before


def test_persistent_editor_terminate_signals_live_child_without_managed_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = PersistentEditor()
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()
    editor._proc = proc

    killed_procs: list[Any] = []
    monkeypatch.setattr(launch_mod, "kill_process_tree", lambda p: killed_procs.append(p))
    monkeypatch.setattr(launch_mod.ManagedSubprocess, "close", MagicMock(side_effect=AssertionError("close called")), raising=False)
    monkeypatch.setattr(launch_mod.ManagedSubprocess, "kill_tree", MagicMock(side_effect=AssertionError("kill_tree called")))

    editor.terminate()

    assert killed_procs == [proc]
    proc.wait.assert_called_once()
    proc.kill.assert_not_called()
    assert editor._proc is None


def test_persistent_editor_terminate_kills_only_when_wait_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = PersistentEditor()
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()
    proc.wait.side_effect = subprocess.TimeoutExpired(cmd="editor", timeout=2)
    editor._proc = proc

    killed_procs: list[Any] = []
    monkeypatch.setattr(launch_mod, "kill_process_tree", lambda p: killed_procs.append(p))
    monkeypatch.setattr(launch_mod.ManagedSubprocess, "close", MagicMock(side_effect=AssertionError("close called")), raising=False)
    monkeypatch.setattr(launch_mod.ManagedSubprocess, "kill_tree", MagicMock(side_effect=AssertionError("kill_tree called")))

    editor.terminate()

    assert killed_procs == [proc]
    proc.wait.assert_called_once()
    proc.kill.assert_called_once()
    assert editor._proc is None


def test_persistent_editor_terminate_kills_tree_when_leader_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reaped leader still gets kill_process_tree; wait and proc.kill stay off."""
    editor = PersistentEditor()
    proc = MagicMock()
    proc.poll.return_value = 0
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()
    editor._proc = proc

    killed_procs: list[Any] = []
    monkeypatch.setattr(launch_mod, "kill_process_tree", lambda p: killed_procs.append(p))

    editor.terminate()

    assert killed_procs == [proc]
    proc.wait.assert_not_called()
    proc.kill.assert_not_called()
    assert editor._proc is None


def test_persistent_editor_terminate_swallows_kill_oserror_and_still_closes_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = PersistentEditor()
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()
    editor._proc = proc

    monkeypatch.setattr(launch_mod, "kill_process_tree", MagicMock(side_effect=OSError("gone")))

    editor.terminate()

    proc.stdin.close.assert_called_once()
    proc.stdout.close.assert_called_once()
    proc.stderr.close.assert_called_once()
    assert editor._proc is None


def test_terminate_persistent_editor_resets_run_script_doc():
    pe = launch_mod._PERSISTENT_EDITOR
    pe.sessions.clear()
    pe.focused_id = None
    pe.run_script_doc = None
    pe.run_script_doc_url = None
    pe.sessions["sid"] = launch_mod.EditorSessionState("sid", "run_script", {"script_name": "demo"})
    pe.focused_id = "sid"
    pe.run_script_doc = object()
    pe.run_script_doc_url = "file:///demo"
    try:
        with patch.object(pe, "terminate") as term:
            launch_mod.terminate_persistent_editor()
        term.assert_called_once()
        assert pe.sessions == {}
        assert pe.focused_id is None
        assert pe.run_script_doc is None
        assert pe.run_script_doc_url is None
    finally:
        _reset_persistent_editor()


def test_terminate_persistent_editor_calls_on_closed_and_empties_sessions():
    """terminate_persistent_editor must call on_closed on active sessions and empty sessions."""
    pe = launch_mod._PERSISTENT_EDITOR
    pe.sessions.clear()
    pe.focused_id = None
    closed: list[str] = []

    try:
        state = launch_mod.EditorSessionState(
            session_id="s1",
            mode="calc_cell",
            target={"cell": "A1"},
            on_closed=lambda: closed.append("state"),
        )
        pe.register_session(state)
        with patch.object(pe, "terminate"):
            launch_mod.terminate_persistent_editor()

        assert "state" in closed
        assert pe.sessions == {}
    finally:
        _reset_persistent_editor()


def test_launch_after_child_death_ends_stale_session_and_keeps_run_script_doc():
    """Dead child triggers purge of stale sessions before setting the new run_script_doc."""
    pe = launch_mod._PERSISTENT_EDITOR
    _reset_persistent_editor()
    dead_proc = MagicMock()
    dead_proc.poll.return_value = 1
    pe._proc = dead_proc

    closed: list[str] = []
    old_state = launch_mod.EditorSessionState(
        "old-sid",
        "run_script",
        {"script_name": "old"},
        on_closed=lambda: closed.append("old"),
    )
    pe.register_session(old_state)
    pe.run_script_doc = object()

    new_proc = MagicMock()
    new_proc.poll.return_value = None
    mock_doc = MagicMock()
    sent_messages: list[dict] = []

    try:
        with patch.object(launch_mod, "spawn_editor_process", return_value=new_proc):
            with patch.object(pe, "start", side_effect=lambda p: setattr(pe, "_proc", p)):
                with patch.object(pe, "wait_for_ready", return_value=True):
                    with patch.object(launch_mod, "run_in_background"):
                        with patch.object(pe, "send", side_effect=sent_messages.append):
                            ok = launch_mod.launch_monaco_editor(
                                MagicMock(),
                                exe="/venv/bin/python",
                                load_message={
                                    "type": "load",
                                    "mode": "run_script",
                                    "run_script_doc": mock_doc,
                                    "script_name": "new",
                                },
                                on_save=MagicMock(),
                            )
        assert ok is True
        assert closed == ["old"]
        assert "old-sid" not in pe.sessions
        assert pe.run_script_doc is mock_doc
        assert len(sent_messages) == 1
        assert sent_messages[0]["session_id"] != "old-sid"
    finally:
        _reset_persistent_editor()


def test_handle_disconnect_clears_run_script_document():
    """Unexpected editor exit must drop the launch document the picker targets."""
    editor = PersistentEditor()
    editor.ctx = MagicMock()
    editor.executor = MagicMock()
    editor.executor.call.side_effect = lambda fn, *args, **kwargs: fn()
    closed: list[str] = []
    editor.register_session(
        launch_mod.EditorSessionState(
            "sid",
            "run_script",
            {"script_name": "demo"},
            on_closed=lambda: closed.append("closed"),
        )
    )
    editor.run_script_doc = object()
    editor.run_script_doc_url = "file:///demo"

    editor._handle_disconnect()

    assert closed == ["closed"]
    assert editor.sessions == {}
    assert editor.focused_id is None
    assert editor.run_script_doc is None
    assert editor.run_script_doc_url is None


def test_handle_disconnect_keeps_replacement_launch_document():
    """A session registered from on_closed keeps the launch document it set."""
    editor = PersistentEditor()
    editor.ctx = MagicMock()
    editor.executor = MagicMock()
    editor.executor.call.side_effect = lambda fn, *args, **kwargs: fn()
    replacement_doc = object()

    def _closed() -> None:
        editor.register_session(
            launch_mod.EditorSessionState("new", "run_script", {"script_name": "other"})
        )
        editor.run_script_doc = replacement_doc
        editor.run_script_doc_url = "file:///new"

    editor.register_session(
        launch_mod.EditorSessionState(
            "old",
            "run_script",
            {"script_name": "demo"},
            on_closed=_closed,
        )
    )
    editor.run_script_doc = object()
    editor.run_script_doc_url = "file:///old"

    editor._handle_disconnect()

    assert "old" not in editor.sessions
    assert "new" in editor.sessions
    assert editor.focused_id == "new"
    assert editor.run_script_doc is replacement_doc
    assert editor.run_script_doc_url == "file:///new"


def test_venv_path_change_clears_webview_probe_failure_cache():
    exe = "/tmp/writeragent-probe-config-python"
    launch_mod.reset_webview_probe_state_for_tests()
    launch_mod._PROBE_CACHE[exe] = (True, "ok")
    launch_mod._PROBE_FAILURE_CACHE[exe] = (10**12, (False, "no"))
    try:
        with patch.object(launch_mod, "resolve_editor_python", return_value=(None, "")):
            with patch.object(launch_mod, "terminate_persistent_editor"):
                launch_mod._on_config_changed(key="scripting.python_venv_path")
        assert exe not in launch_mod._PROBE_CACHE
        assert exe not in launch_mod._PROBE_FAILURE_CACHE
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_send_untracked_type_still_writes_frame():
    pe = PersistentEditor()
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.stdin = io.BytesIO()
    pe._proc = mock_proc

    pe.send({"type": "validate_result"})

    initial_len = len(mock_proc.stdin.getvalue())
    assert initial_len > 0

    pe.send({"type": "load", "code": "x"})
    assert len(mock_proc.stdin.getvalue()) > initial_len


def test_send_sees_none_proc_as_runtimeerror():
    pe = PersistentEditor()
    assert pe._proc is None
    with pytest.raises(RuntimeError, match="No editor process is running"):
        pe.send({"type": "load"})


def test_terminate_during_send_does_not_raise_attributeerror():
    pe = PersistentEditor()
    poll_started = threading.Event()
    unblock_poll = threading.Event()

    proc = MagicMock()

    def fake_poll():
        poll_started.set()
        unblock_poll.wait(timeout=5.0)
        return None

    proc.poll.side_effect = fake_poll
    proc.stdin = io.BytesIO()
    pe._proc = proc

    send_exc: list[Exception] = []

    def _do_send():
        try:
            pe.send({"type": "load", "code": "pass"})
        except Exception as e:
            send_exc.append(e)

    def _do_terminate():
        pe.terminate()

    t_send = threading.Thread(target=_do_send)
    t_term = threading.Thread(target=_do_terminate)

    t_send.start()
    assert poll_started.wait(timeout=5.0)
    t_term.start()
    time.sleep(0.05)
    unblock_poll.set()

    t_send.join(timeout=5.0)
    t_term.join(timeout=5.0)

    assert not t_send.is_alive()
    assert not t_term.is_alive()
    assert all(not isinstance(e, AttributeError) for e in send_exc)
    for e in send_exc:
        assert isinstance(e, RuntimeError)
    assert pe._proc is None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows joins a blocking write thread; closing an in-process pipe while blocked in CRT write crashes",
)
def test_send_unread_pipe_times_out_and_terminates(monkeypatch):
    monkeypatch.setattr(launch_mod, "VENV_IPC_WRITE_TIMEOUT_SEC", 0.2)
    _reset_persistent_editor()
    read_fd, write_fd = os.pipe()
    try:
        import fcntl

        fcntl.fcntl(write_fd, fcntl.F_SETPIPE_SZ, 4096)
    except (ImportError, AttributeError, OSError):
        pass
    writer = os.fdopen(write_fd, "wb", buffering=0)

    try:
        pe = launch_mod._PERSISTENT_EDITOR
        closed_calls: list[str] = []
        state = launch_mod.EditorSessionState(
            session_id="s1",
            mode="calc_cell",
            target={"cell": "A1"},
            on_closed=lambda: closed_calls.append("closed"),
        )
        pe.register_session(state)

        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdin = writer
        proc.stdout = None
        proc.stderr = None
        pe._proc = proc

        captured_exc: list[Exception] = []

        def _do_send():
            try:
                pe.send({"type": "load", "code": "x" * (200 * 1024)})
            except Exception as exc:
                captured_exc.append(exc)

        t = threading.Thread(target=_do_send)
        t.start()
        t.join(5.0)

        assert not t.is_alive()
        assert len(captured_exc) == 1
        exc = captured_exc[0]
        assert isinstance(exc, RuntimeError)
        assert "Editor unresponsive" in str(exc)
        proc.kill.assert_called()
        assert pe._proc is None
        assert pe.sessions == {}
        assert len(closed_calls) == 1
    finally:
        try:
            writer.close()
        except Exception:
            pass
        try:
            os.close(read_fd)
        except OSError:
            pass
        _reset_persistent_editor()


def test_send_oversize_payload_raises_valueerror_and_preserves_proc(monkeypatch):
    pe = PersistentEditor()
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    pe._proc = proc

    monkeypatch.setattr("plugin.scripting.editor_ipc.DEFAULT_MAX_PAYLOAD_BYTES", 10)
    with pytest.raises(ValueError) as excinfo:
        pe.send({"type": "load", "code": "x" * 100})

    msg = str(excinfo.value)
    assert "exceeds maximum payload size" in msg
    assert any(char.isdigit() for char in msg)
    assert proc.stdin.getvalue() == b""
    assert pe._proc is proc


def test_is_running_captures_proc_once():
    pe = PersistentEditor()
    proc = MagicMock()

    def null_proc():
        pe._proc = None
        return None

    proc.poll.side_effect = null_proc
    pe._proc = proc
    # First access captures proc before poll nulls pe._proc; returns True and does not raise AttributeError.
    assert pe.is_running is True
    # Now that pe._proc is None, subsequent access returns False.
    assert pe.is_running is False

    # Same shape for one wait_for_ready iteration: patch process_events_to_idle
    # and use a timeout so the loop body runs; poll nulls _proc; no AttributeError.
    pe._proc = proc
    with patch("plugin.framework.uno_context.process_events_to_idle"):
        assert pe.wait_for_ready(MagicMock(), timeout_sec=0.1) is False


def test_wait_for_ready_never_ready_live_child_returns_false() -> None:
    pe = PersistentEditor()
    proc = MagicMock()
    proc.poll.return_value = None
    pe._proc = proc
    t0 = time.monotonic()
    assert pe.wait_for_ready(MagicMock(), timeout_sec=0.2) is False
    assert time.monotonic() - t0 >= 0.15


def test_wait_for_ready_event_set_returns_true_immediately() -> None:
    pe = PersistentEditor()
    proc = MagicMock()
    proc.poll.return_value = None
    pe._proc = proc
    pe._ready_event.set()
    t0 = time.monotonic()
    assert pe.wait_for_ready(MagicMock(), timeout_sec=5.0) is True
    assert time.monotonic() - t0 < 1.0


def test_wait_for_ready_under_drain_owner_returns_false_near_deadline() -> None:
    from plugin.framework.async_drain_guard import drain_owner_scope

    pe = PersistentEditor()
    proc = MagicMock()
    proc.poll.return_value = None
    pe._proc = proc
    t0 = time.monotonic()
    with drain_owner_scope("test"):
        assert pe.wait_for_ready(MagicMock(), timeout_sec=0.3) is False
    elapsed = time.monotonic() - t0
    assert 0.25 <= elapsed < 1.0


def test_start_drops_stderr_from_previous_generation():
    editor = PersistentEditor()
    editor._append_stderr_line("stale", 0)
    proc = _FakeProc(stderr=None)
    with patch("plugin.scripting.editor_host.run_in_background", return_value=MagicMock()):
        editor.start(proc)  # type: ignore[arg-type]
    editor._append_stderr_line("stale-late", generation=0)
    editor._append_stderr_line("fresh", generation=editor._stderr_generation)
    tail = editor.read_stderr_tail()
    assert "fresh" in tail
    assert "stale" not in tail
    assert "stale-late" not in tail
    assert editor._stderr_generation == 1


def test_queue_save_then_load_false_without_focused_session():
    _reset_persistent_editor()
    res = launch_mod.queue_save_then_load(
        {"type": "load", "mode": "calc_cell", "code": "x = 1"},
        MagicMock(),
        lambda: None,
    )
    assert res is False
    assert launch_mod._PERSISTENT_EDITOR.sessions == {}


def test_prepare_save_cancel_when_focused_session_disappears():
    pe = launch_mod._PERSISTENT_EDITOR
    _reset_persistent_editor()
    proc = MagicMock()
    proc.poll.return_value = None
    pe._proc = proc
    state = launch_mod.EditorSessionState(
        "sid-cell",
        "calc_cell",
        {"cell_address": "C3"},
        on_save=MagicMock(),
    )
    state.dirty = True
    pe.register_session(state)
    sent: list[dict] = []

    def _send(message: dict, session: object = None) -> None:
        sent.append(dict(message))

    pe.send = _send  # type: ignore[method-assign]

    def _confirm_and_clear(_ctx: object, _label: str) -> str:
        pe.sessions.clear()
        pe.focused_id = None
        return "save"

    try:
        with patch.object(launch_mod, "confirm_unsaved_monaco_edit", side_effect=_confirm_and_clear):
            ok = launch_mod.launch_monaco_editor(
                MagicMock(),
                exe="/venv/bin/python",
                load_message={
                    "type": "load",
                    "mode": "run_script",
                    "script_name": "Demo",
                    "code": "print(1)",
                },
                on_save=MagicMock(),
            )
        assert ok is True
        assert sent == []
        assert state.pending_load is None
    finally:
        _reset_persistent_editor()


def test_queue_save_then_load_true_when_request_sent():
    pe = launch_mod._PERSISTENT_EDITOR
    _reset_persistent_editor()
    proc = MagicMock()
    proc.poll.return_value = None
    pe._proc = proc
    state = launch_mod.EditorSessionState("sid", "calc_cell", {"cell_address": "A1"})
    state.dirty = True
    pe.register_session(state)
    sent: list[dict] = []

    def _send(message: dict, session: object = None) -> None:
        sent.append(dict(message))

    pe.send = _send  # type: ignore[method-assign]
    try:
        res = launch_mod.queue_save_then_load(
            {"type": "load", "mode": "run_script", "script_name": "Demo", "code": "print(1)"},
            MagicMock(),
            lambda: None,
        )
        assert res is True
        assert state.pending_load is not None
        assert [m["type"] for m in sent] == ["request_save"]
    finally:
        _reset_persistent_editor()


def test_force_internal_change_terminates_without_clearing_probe_cache():
    exe = "/tmp/writeragent-probe-force-internal"
    launch_mod._PROBE_CACHE[exe] = (True, "ok")
    try:
        with patch.object(launch_mod, "terminate_persistent_editor") as mock_term:
            launch_mod._on_config_changed(key="scripting.force_internal_script_editor")
            mock_term.assert_called_once()
            assert exe in launch_mod._PROBE_CACHE

        with patch.object(launch_mod, "terminate_persistent_editor") as mock_term:
            launch_mod._on_config_changed(key="", keys=("scripting.force_internal_script_editor",))
            mock_term.assert_called_once()
            assert exe in launch_mod._PROBE_CACHE
    finally:
        launch_mod._PROBE_CACHE.pop(exe, None)


def test_unrelated_config_key_does_not_terminate_editor():
    exe = "/tmp/writeragent-probe-unrelated"
    launch_mod._PROBE_CACHE[exe] = (True, "ok")
    try:
        with patch.object(launch_mod, "terminate_persistent_editor") as mock_term:
            launch_mod._on_config_changed(key="audio.tts_enabled")
            mock_term.assert_not_called()
            assert exe in launch_mod._PROBE_CACHE
    finally:
        launch_mod._PROBE_CACHE.pop(exe, None)


def test_launch_enriches_once_when_the_load_is_sent():
    from plugin.scripting.editor_ui_strings import enrich_monaco_load_message as real_enrich

    ctx = MagicMock()
    sent_messages: list[dict] = []
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    pe = launch_mod._PERSISTENT_EDITOR
    pe.sessions.clear()
    pe.focused_id = None

    def fake_send(msg: dict) -> None:
        sent_messages.append(msg)

    pe.send = fake_send  # type: ignore[method-assign]
    try:
        with patch.object(type(pe), "is_running", new=property(lambda self: True)):
            pe._proc = mock_proc
            with patch("plugin.scripting.editor_host.enrich_monaco_load_message", wraps=real_enrich) as mock_enrich:
                ok = launch_mod.launch_monaco_editor(
                    ctx,
                    exe="/venv/bin/python",
                    load_message={"type": "load", "code": "print(1)", "mode": "calc_cell", "cell_address": "A1"},
                    on_save=MagicMock(),
                )

        assert ok is True
        assert mock_enrich.call_count == 1
        assert sent_messages[0]["type"] == "load"
        assert "ui" in sent_messages[0]
        assert "theme" in sent_messages[0]
    finally:
        _reset_persistent_editor()


def test_launch_enriches_once_when_save_is_queued():
    from plugin.scripting.editor_ui_strings import enrich_monaco_load_message as real_enrich

    pe = launch_mod._PERSISTENT_EDITOR
    _reset_persistent_editor()
    proc = MagicMock()
    proc.poll.return_value = None
    pe._proc = proc
    state = launch_mod.EditorSessionState(
        "sid-cell",
        "calc_cell",
        {"cell_address": "C3"},
        on_save=MagicMock(),
    )
    state.dirty = True
    pe.register_session(state)
    sent: list[dict] = []

    def _send(message: dict, session: object = None) -> None:
        sent.append(dict(message))

    pe.send = _send  # type: ignore[method-assign]
    try:
        mock_doc = object()
        with patch("plugin.scripting.editor_host.enrich_monaco_load_message", wraps=real_enrich) as mock_enrich:
            with patch.object(launch_mod, "confirm_unsaved_monaco_edit", return_value="save"):
                ok = launch_mod.launch_monaco_editor(
                    MagicMock(),
                    exe="/venv/bin/python",
                    load_message={
                        "type": "load",
                        "mode": "run_script",
                        "script_name": "Demo",
                        "code": "print(1)",
                        "run_script_doc": mock_doc,
                    },
                    on_save=MagicMock(),
                )
        assert ok is True
        assert mock_enrich.call_count == 1
        assert [msg["type"] for msg in sent] == ["request_save"]
        assert state.pending_load is not None
        assert "ui" in state.pending_load
        assert state.pending_load["mode"] == "run_script"
        assert state.pending_load["run_script_doc"] is mock_doc
    finally:
        _reset_persistent_editor()


def test_editor_save_marshal_error_reports_and_clears_pending():
    for exc in (RuntimeError("AsyncCallback unavailable"), SendCancelled()):
        pe = PersistentEditor()
        pe.ctx = MagicMock()
        pe.executor = MagicMock()
        pe.executor.call.side_effect = exc
        state = launch_mod.EditorSessionState(session_id="s1", mode="calc_cell", target={"cell": "A1"})
        state.pending_load = {"code": "y = 2"}
        state.pending_on_save = MagicMock()
        state.pending_on_closed = MagicMock()
        state.dirty = True
        pe.register_session(state)
        sent: list[dict] = []

        def _send(message, *, session=None):
            sent.append(message)

        pe.send = _send  # type: ignore[method-assign]
        pe._dispatch_incoming({"type": "save", "code": "x = 1", "session_id": "s1"})
        assert pe._save_token is None
        assert state.pending_load is None
        assert state.pending_on_save is None
        assert state.pending_on_closed is None
        assert state.dirty is True
        assert sent and sent[0]["message"] == "Saving the script failed."
        assert "timed out" not in sent[0]["message"]


def test_editor_script_picker_marshal_error_sends_error_frame():
    for exc in (RuntimeError("AsyncCallback unavailable"), SendCancelled()):
        pe = PersistentEditor()
        pe.ctx = MagicMock()
        pe.executor = MagicMock()
        pe.executor.call.side_effect = exc
        sent: list[dict] = []

        def _send(message, *, session=None):
            sent.append(message)

        pe.send = _send  # type: ignore[method-assign]
        pe._dispatch_incoming({"type": "request_scripts"})
        assert sent and sent[0]["message"] == "The script list update failed."


def test_editor_close_marshal_error_stays_in_dispatch():
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.call.side_effect = RuntimeError("AsyncCallback unavailable")
    on_closed_mock = MagicMock()
    state = launch_mod.EditorSessionState(
        session_id="s1",
        mode="calc_cell",
        target={"cell": "A1"},
        on_closed=on_closed_mock,
    )
    pe.register_session(state)
    pe._dispatch_incoming({"type": "closed", "session_id": "s1"})
    on_closed_mock.assert_not_called()
    assert "s1" in pe.sessions
    assert pe.executor.call.call_args.kwargs["bound_scope"] is None


def test_handle_disconnect_marshal_error_stays_in_handler():
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.call.side_effect = RuntimeError("AsyncCallback unavailable")
    state = launch_mod.EditorSessionState(
        session_id="s1",
        mode="calc_cell",
        target={"cell": "A1"},
    )
    pe.register_session(state)
    pe._handle_disconnect()
    assert "s1" in pe.sessions


def test_picker_unknown_session_does_not_run_or_stamp_focused():
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.send = MagicMock()
    live_state = launch_mod.EditorSessionState(session_id="sid-live", mode="calc_cell", target={"cell": "A1"})
    pe.register_session(live_state)

    with patch("plugin.scripting.editor_host.handle_editor_script_message") as mock_handler:
        pe._dispatch_incoming({"type": "request_scripts", "session_id": "sid-dead"})
        mock_handler.assert_not_called()
        pe.executor.call.assert_not_called()
        pe.send.assert_not_called()


def test_picker_known_session_still_dispatches():
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.call.side_effect = lambda fn, *args, **kwargs: fn()
    pe.send = MagicMock()
    live_state = launch_mod.EditorSessionState(session_id="sid-live", mode="calc_cell", target={"cell": "A1"})
    pe.register_session(live_state)

    def fake_handler(kind, msg, *, session_doc, session_doc_url, send):
        send({"type": "scripts_list"})
        return True

    with patch("plugin.scripting.editor_host.handle_editor_script_message", side_effect=fake_handler) as mock_handler:
        pe._dispatch_incoming({"type": "request_scripts", "session_id": "sid-live"})
        mock_handler.assert_called_once()
        pe.send.assert_called_once_with({"type": "scripts_list"}, session=live_state)


def test_ask_unsaved_replies_with_native_choice():
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.post.side_effect = lambda fn, *args, **kwargs: fn()
    pe.send = MagicMock()
    live_state = launch_mod.EditorSessionState(session_id="sid-live", mode="run_script", target={})
    pe.register_session(live_state)

    with patch.object(launch_mod, "confirm_unsaved_monaco_edit", return_value="save") as confirm:
        pe._dispatch_incoming({"type": "ask_unsaved", "name": "MyScript", "session_id": "sid-live"})
    confirm.assert_called_once_with(pe.ctx, "MyScript")
    pe.executor.call.assert_not_called()
    pe.send.assert_called_once_with({"type": "unsaved_choice", "choice": "save"}, session=live_state)


def test_error_frame():
    from plugin.scripting.editor_host import error_frame

    frame = error_frame("test error")
    assert frame == {"type": "error", "message": "test error"}

    exc = ValueError("boom")
    frame_with_exc = error_frame("test error with exc", exc)
    assert frame_with_exc["type"] == "error"
    assert frame_with_exc["message"] == "test error with exc"
    assert "ValueError: boom" in frame_with_exc["traceback"]



def test_selected_editor_backend_default_codemirror_garbage():
    with patch("plugin.scripting.editor_host.get_config", return_value=""):
        assert launch_mod.selected_editor_backend() == "monaco"
    with patch("plugin.scripting.editor_host.get_config", return_value="monaco"):
        assert launch_mod.selected_editor_backend() == "monaco"
    with patch("plugin.scripting.editor_host.get_config", return_value="codemirror"):
        assert launch_mod.selected_editor_backend() == "codemirror"
    with patch("plugin.scripting.editor_host.get_config", return_value="  CodeMirror  "):
        assert launch_mod.selected_editor_backend() == "codemirror"
    with patch("plugin.scripting.editor_host.get_config", return_value="sublime"):
        assert launch_mod.selected_editor_backend() == "monaco"


def test_build_editor_child_env_sets_backend_env_var():
    with patch.object(launch_mod, "selected_editor_backend", return_value="codemirror"):
        env = launch_mod.build_editor_child_env()
        assert env["WRITERAGENT_EDITOR_BACKEND"] == "codemirror"
    with patch.object(launch_mod, "selected_editor_backend", return_value="monaco"):
        env = launch_mod.build_editor_child_env()
        assert env["WRITERAGENT_EDITOR_BACKEND"] == "monaco"


def test_probe_codemirror_does_not_require_rocher_monaco_does():
    exe = "/tmp/writeragent-probe-backend-test"
    launch_mod._PROBE_CACHE.pop(exe, None)
    launch_mod._PROBE_FAILURE_CACHE.pop(exe, None)
    executed_commands: list[list[str]] = []

    def mock_run(cmd, **kwargs):
        executed_commands.append(cmd)
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "ok"
        mock_proc.stderr = ""
        return mock_proc

    try:
        with patch.object(launch_mod.ManagedSubprocess, "run", side_effect=mock_run):
            with patch.object(launch_mod, "selected_editor_backend", return_value="codemirror"):
                ok, _ = launch_mod.probe_webview_import(exe, apply_generation=0)
                assert ok is True
                assert len(executed_commands) == 1
                probe_py = executed_commands[0][-1]
                assert "import webview" in probe_py
                assert "import rocher" not in probe_py

            launch_mod.reset_webview_probe_state_for_tests()
            executed_commands.clear()

            with patch.object(launch_mod, "selected_editor_backend", return_value="monaco"):
                ok, _ = launch_mod.probe_webview_import(exe, apply_generation=0)
                assert ok is True
                assert len(executed_commands) == 1
                probe_py = executed_commands[0][-1]
                assert "import webview" in probe_py
                assert "import rocher" in probe_py
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_probe_webview_missing_returns_false_for_both_backends():
    exe = "/tmp/writeragent-probe-webview-missing"
    launch_mod.reset_webview_probe_state_for_tests()

    def mock_fail(cmd, **kwargs):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "ModuleNotFoundError: No module named 'webview'"
        return mock_proc

    try:
        with patch.object(launch_mod.ManagedSubprocess, "run", side_effect=mock_fail):
            with patch.object(launch_mod, "selected_editor_backend", return_value="codemirror"):
                ok, detail = launch_mod.probe_webview_import(exe, apply_generation=0)
                assert ok is False
                assert "webview" in detail

            launch_mod.reset_webview_probe_state_for_tests()

            with patch.object(launch_mod, "selected_editor_backend", return_value="monaco"):
                ok, detail = launch_mod.probe_webview_import(exe, apply_generation=0)
                assert ok is False
                assert "webview" in detail
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_config_change_python_editor_terminates_editor_and_clears_probe_caches():
    exe = "/tmp/writeragent-probe-python-editor"
    launch_mod.reset_webview_probe_state_for_tests()
    launch_mod._PROBE_CACHE[exe] = (True, "ok")
    launch_mod._PROBE_FAILURE_CACHE[exe] = (999999.0, (False, "err"))
    try:
        with patch.object(launch_mod, "resolve_editor_python", return_value=(None, "")):
            with patch.object(launch_mod, "terminate_persistent_editor") as mock_term:
                launch_mod._on_config_changed(key="scripting.python_editor")
                mock_term.assert_called_once()
                assert exe not in launch_mod._PROBE_CACHE
                assert exe not in launch_mod._PROBE_FAILURE_CACHE

            launch_mod._PROBE_CACHE[exe] = (True, "ok")
            launch_mod._PROBE_FAILURE_CACHE[exe] = (999999.0, (False, "err"))

            with patch.object(launch_mod, "terminate_persistent_editor") as mock_term:
                launch_mod._on_config_changed(key="", keys=("scripting.python_editor",))
                mock_term.assert_called_once()
                assert exe not in launch_mod._PROBE_CACHE
                assert exe not in launch_mod._PROBE_FAILURE_CACHE
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_config_change_unrelated_key_keeps_editor_and_probe_cache():
    exe = "/tmp/writeragent-probe-unrelated-key"
    launch_mod.reset_webview_probe_state_for_tests()
    launch_mod._PROBE_CACHE[exe] = (True, "ok")
    try:
        with patch.object(launch_mod, "terminate_persistent_editor") as mock_term:
            launch_mod._on_config_changed(key="", keys=("scripting.python_session_mode",))
            mock_term.assert_not_called()
            assert launch_mod._PROBE_CACHE[exe] == (True, "ok")
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_monaco_editor_available_pending_does_not_run_subprocess():
    exe = "/venv/bin/python"
    launch_mod.reset_webview_probe_state_for_tests()
    try:
        with patch.object(launch_mod, "run_in_background"):
            launch_mod._ensure_webview_probe(exe)
        with patch.object(launch_mod, "resolve_editor_python", return_value=(exe, "")):
            with patch.object(
                launch_mod.ManagedSubprocess,
                "run",
                side_effect=AssertionError("ManagedSubprocess.run must not be called"),
            ) as mock_run:
                returned_exe, ok = launch_mod.monaco_editor_available()
                mock_run.assert_not_called()
        assert returned_exe == exe
        assert ok is False
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_venv_path_change_schedules_one_webview_probe():
    exe = "/venv/bin/python"
    try:
        launch_mod.reset_webview_probe_state_for_tests()
        with (
            patch.object(launch_mod, "resolve_editor_python", return_value=(exe, "")),
            patch.object(launch_mod, "run_in_background") as mock_bg,
            patch.object(launch_mod, "terminate_persistent_editor"),
        ):
            launch_mod._on_config_changed(key="scripting.python_venv_path")
            assert mock_bg.call_count == 1
            assert mock_bg.call_args[0][0] == launch_mod._webview_probe_worker
            assert mock_bg.call_args[1].get("name") == "webview-probe"
            assert mock_bg.call_args[1].get("dedicated") is False
            assert launch_mod._probe_running is True

            launch_mod._on_config_changed(key="scripting.python_venv_path")
            assert mock_bg.call_count == 1

        launch_mod.reset_webview_probe_state_for_tests()
        with (
            patch.object(launch_mod, "resolve_editor_python", return_value=(exe, "")),
            patch.object(launch_mod, "run_in_background") as mock_bg,
            patch.object(launch_mod, "terminate_persistent_editor"),
        ):
            launch_mod._on_config_changed(key="", keys=("scripting.python_venv_path",))
            assert mock_bg.call_count == 1
            assert mock_bg.call_args[0][0] == launch_mod._webview_probe_worker
            assert mock_bg.call_args[1].get("name") == "webview-probe"

        launch_mod.reset_webview_probe_state_for_tests()
        with (
            patch.object(launch_mod, "resolve_editor_python", return_value=(exe, "")),
            patch.object(launch_mod, "run_in_background") as mock_bg,
            patch.object(launch_mod, "terminate_persistent_editor"),
        ):
            launch_mod._on_config_changed(key="scripting.python_editor")
            assert mock_bg.call_count == 1
            assert mock_bg.call_args[0][0] == launch_mod._webview_probe_worker
            assert mock_bg.call_args[1].get("name") == "webview-probe"
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_venv_path_change_with_force_internal_does_not_probe():
    """When force_internal_script_editor is True, venv change invalidates cache but skips probe."""
    exe = "/venv/bin/python"
    try:
        launch_mod.reset_webview_probe_state_for_tests()
        launch_mod._PROBE_CACHE[exe] = (True, "ok")
        with (
            patch("plugin.scripting.editor_host.get_config", return_value=True),
            patch.object(launch_mod, "resolve_editor_python", return_value=(exe, "")),
            patch.object(launch_mod, "run_in_background") as mock_bg,
            patch.object(launch_mod, "terminate_persistent_editor"),
        ):
            launch_mod._on_config_changed(key="scripting.python_venv_path")
            mock_bg.assert_not_called()
            assert exe not in launch_mod._PROBE_CACHE
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_monaco_editor_available_cache_miss_arms_once():
    exe = "/venv/bin/python"
    launch_mod.reset_webview_probe_state_for_tests()
    try:
        with (
            patch.object(launch_mod, "resolve_editor_python", return_value=(exe, "")),
            patch.object(launch_mod, "run_in_background") as mock_bg,
            patch.object(
                launch_mod.ManagedSubprocess,
                "run",
                side_effect=AssertionError("ManagedSubprocess.run must not be called on caller"),
            ) as mock_run,
        ):
            returned_exe, ok = launch_mod.monaco_editor_available()
            assert returned_exe == exe
            assert ok is False
            assert mock_bg.call_count == 1
            assert mock_bg.call_args[0][0] == launch_mod._webview_probe_worker
            assert mock_bg.call_args[1].get("name") == "webview-probe"
            mock_run.assert_not_called()
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_probe_webview_import_stale_generation_does_not_store():
    exe = "/venv/bin/python"
    launch_mod.reset_webview_probe_state_for_tests()
    try:
        launch_mod._probe_generation = 5
        mock_res = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch.object(launch_mod.ManagedSubprocess, "run", return_value=mock_res):
            ok, _detail = launch_mod.probe_webview_import(exe, apply_generation=4)
            assert ok is True
            assert exe not in launch_mod._PROBE_CACHE
            assert exe not in launch_mod._PROBE_FAILURE_CACHE

            ok, _detail = launch_mod.probe_webview_import(exe, apply_generation=5)
            assert ok is True
            assert launch_mod._PROBE_CACHE.get(exe) == (True, "ok")
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_webview_probe_worker_picks_up_new_exe_on_config_change():
    """When config changes while a probe runs, stale result is dropped and new exe is probed."""
    launch_mod.reset_webview_probe_state_for_tests()
    with patch.object(launch_mod, "run_in_background"):
        launch_mod._ensure_webview_probe("/old/python")

    calls: list[str] = []

    def fake_run(cmd, **_kwargs):
        exe = cmd[0]
        calls.append(exe)
        if len(calls) == 1:
            # First probe is executing for /old/python. Simulate a config change mid-flight.
            launch_mod._invalidate_webview_probe("/new/python")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    try:
        with patch.object(launch_mod.ManagedSubprocess, "run", side_effect=fake_run):
            launch_mod._webview_probe_worker()

        assert len(calls) == 2
        assert "/old/python" not in launch_mod._PROBE_CACHE
        assert launch_mod._PROBE_CACHE.get("/new/python") == (True, "ok")
        assert launch_mod._probe_running is False
        assert launch_mod._probe_wanted is None
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_arm_webview_probe_at_startup_arms_once():
    """Startup arming schedules probe when venv is valid and force_internal is False."""
    launch_mod.reset_webview_probe_state_for_tests()
    exe = "/configured/python"
    try:
        with (
            patch("plugin.scripting.editor_host.get_config", return_value=False),
            patch.object(launch_mod, "resolve_editor_python", return_value=(exe, "")),
            patch.object(launch_mod, "run_in_background") as mock_bg,
        ):
            launch_mod.arm_webview_probe_at_startup()
            assert mock_bg.call_count == 1
            assert mock_bg.call_args[0][0] == launch_mod._webview_probe_worker
            assert mock_bg.call_args[1].get("name") == "webview-probe"

        launch_mod.reset_webview_probe_state_for_tests()
        with (
            patch("plugin.scripting.editor_host.get_config", return_value=True),
            patch.object(launch_mod, "resolve_editor_python", return_value=(exe, "")),
            patch.object(launch_mod, "run_in_background") as mock_bg,
        ):
            launch_mod.arm_webview_probe_at_startup()
            mock_bg.assert_not_called()

        launch_mod.reset_webview_probe_state_for_tests()
        with (
            patch("plugin.scripting.editor_host.get_config", return_value=False),
            patch.object(launch_mod, "resolve_editor_python", return_value=(None, "no venv")),
            patch.object(launch_mod, "run_in_background") as mock_bg,
        ):
            launch_mod.arm_webview_probe_at_startup()
            mock_bg.assert_not_called()
    finally:
        launch_mod.reset_webview_probe_state_for_tests()


def test_incoming_handlers_cover_child_to_parent_types():
    # Every type the editor child sends to LibreOffice must map to a handler
    # in PersistentEditor._incoming_handlers, and picker messages must stay a subset.
    handlers = PersistentEditor()._incoming_handlers()
    assert set(handlers) == CHILD_TO_PARENT_TYPES
    assert SCRIPT_PICKER_MESSAGE_TYPES <= CHILD_TO_PARENT_TYPES


def test_dispatch_cancel_is_unknown_and_keeps_session():
    # The child sends "closed" upon cancellation, never a literal "cancel" message.
    # An unhandled "cancel" message must be ignored rather than tearing down a live session.
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.call.side_effect = lambda fn, **kwargs: fn()
    on_closed_mock = MagicMock()
    state = launch_mod.EditorSessionState(
        session_id="s1",
        mode="calc_cell",
        target={"cell": "A1"},
        on_closed=on_closed_mock,
    )
    pe.register_session(state)
    pe._dispatch_incoming({"type": "cancel", "session_id": "s1"})
    on_closed_mock.assert_not_called()
    assert "s1" in pe.sessions
