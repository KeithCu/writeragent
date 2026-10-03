# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Monaco editor host (spawn, bridge, session launch)."""

from __future__ import annotations

import os
import subprocess
import threading
from unittest.mock import MagicMock, patch

from plugin.scripting import editor_host as launch_mod
from plugin.scripting.editor_host import PersistentEditor, _ASSETS_DIR


def test_editor_save_timeout_clears_the_token_and_reports():
    pe = PersistentEditor()
    pe.ctx = MagicMock()
    pe.executor = MagicMock()
    pe.executor.execute.side_effect = TimeoutError()
    state = launch_mod.EditorSessionState(session_id="s1", mode="calc_cell", target={"cell": "A1"})
    pe.register_session(state)
    sent: list[dict] = []

    def _send(message, *, session=None):
        sent.append(message)

    pe.send = _send  # type: ignore[method-assign]
    with patch("plugin.scripting.config_limits.configured_python_exec_timeout", return_value=30):
        pe._dispatch_incoming({"type": "save", "code": "x = 1", "session_id": "s1"})
    assert pe._save_token is None
    assert sent and "timed out" in sent[0]["message"]
    assert pe.executor.execute.call_args.kwargs["timeout"] == 35.0


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

    with patch.object(type(pe), "is_running", new=property(lambda self: True)):
        pe._proc = mock_proc
        with patch.object(launch_mod, "EditorSession") as mock_session_cls:
            session = MagicMock()
            session.is_running = True
            session.send = fake_send
            mock_session_cls.return_value = session

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
    mock_session_cls.assert_called_once()
    pe.sessions.clear()
    pe.focused_id = None
    pe._proc = None


def test_launch_monaco_editor_spawns_when_not_running():
    ctx = MagicMock()
    mock_proc = MagicMock()
    mock_doc = MagicMock()
    pe = launch_mod._PERSISTENT_EDITOR
    pe.sessions.clear()
    pe.focused_id = None

    with patch.object(type(pe), "is_running", new=property(lambda self: False)):
        with patch.object(launch_mod, "spawn_editor_process", return_value=mock_proc):
            with patch.object(launch_mod, "EditorSession") as mock_session_cls:
                session = MagicMock()
                session.is_running = True
                session.wait_for_ready.return_value = True
                mock_session_cls.return_value = session

                load_message = {"type": "load", "mode": "run_script", "run_script_doc": mock_doc, "script_name": "demo"}
                ok = launch_mod.launch_monaco_editor(
                    ctx,
                    exe="/venv/bin/python",
                    load_message=load_message,
                    on_save=MagicMock(),
                )

    assert ok is True
    assert pe.run_script_doc is mock_doc
    session.start_reader.assert_called_once()
    sent = session.send.call_args[0][0]
    assert sent["type"] == "load"
    assert sent["mode"] == "run_script"
    assert sent["session_id"]
    assert sent["target"]["script_name"] == "demo"
    assert "run_script_doc" not in sent
    assert "theme" in sent
    assert "ui" in sent
    assert sent["ui"]["script_label"]
    assert load_message["run_script_doc"] is mock_doc
    pe.sessions.clear()
    pe.focused_id = None
    pe.run_script_doc = None


def test_monaco_editor_available_false_without_venv():
    ctx = MagicMock()
    with patch.object(launch_mod, "resolve_editor_python", return_value=(None, "missing venv")):
        exe, ok = launch_mod.monaco_editor_available(ctx)
    assert exe is None
    assert ok is False


def test_probe_webview_import_timeout_returns_diagnostic():
    """A probe timeout must return the traceback Monaco shows, not raise under deal."""
    exe = "/tmp/writeragent-probe-timeout-python"
    launch_mod._PROBE_CACHE.pop(exe, None)
    timeout = subprocess.TimeoutExpired(cmd=[exe, "-c", "import webview"], timeout=30)
    with patch("plugin.scripting.editor_host.subprocess.run", side_effect=timeout):
        ok, detail = launch_mod.probe_webview_import(exe)
    assert ok is False
    assert "TimeoutExpired" in detail
    assert exe not in launch_mod._PROBE_CACHE


def test_probe_webview_import_oserror_returns_diagnostic():
    exe = "/tmp/writeragent-probe-oserror-python"
    launch_mod._PROBE_CACHE.pop(exe, None)
    with patch("plugin.scripting.editor_host.subprocess.run", side_effect=OSError("boom")):
        ok, detail = launch_mod.probe_webview_import(exe)
    assert ok is False
    assert "OSError" in detail
    assert "boom" in detail
    assert exe not in launch_mod._PROBE_CACHE


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
    editor.executor.execute.side_effect = lambda fn, timeout=None: fn()
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
    editor.executor.execute.side_effect = lambda fn, timeout=None: fn()
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


def test_resolve_editor_python_missing_venv_mentions_settings():
    with patch("plugin.framework.config.get_config_str", return_value=""):
        exe, err = launch_mod.resolve_editor_python(MagicMock())
    assert exe is None
    assert "Settings → Python" in err
    assert "WriterAgent Settings" not in err


def test_launch_monaco_spawn_oserror_uses_product_display_name():
    ctx = MagicMock()
    with patch.object(launch_mod, "_PERSISTENT_EDITOR") as mock_persistent:
        mock_persistent.is_running = False
        with patch.object(launch_mod, "spawn_editor_process", side_effect=OSError("boom")):
            with patch("plugin.chatbot.dialogs.msgbox_with_report") as box:
                with patch("plugin.framework.uno_context.product_display_name", return_value="LibrePy"):
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


def test_scripts_manager_js_guards_save_and_resets_on_load():
    js_path = os.path.join(_ASSETS_DIR, "scripts_manager.js")
    assert os.path.isfile(js_path)
    with open(js_path, "r", encoding="utf-8") as f:
        js_content = f.read()

    # Invariants:
    # 1. Track currentMode in scripts_manager.js
    assert "currentMode" in js_content
    # 2. Guard btn-save listener to only intercept in run_script mode
    assert "isRunScriptActive" in js_content or 'currentMode === "run_script"' in js_content
    # 3. Reset dropdown state on non-run_script load
    assert 'selectedScriptName = ""' in js_content
    assert 'currentSelectedName = ""' in js_content


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
    editor.executor.execute.side_effect = lambda fn, timeout=None: fn()

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

