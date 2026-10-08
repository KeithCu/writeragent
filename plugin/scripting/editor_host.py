# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Monaco editor host: spawn child process, pipe bridge, and session launch."""

# =========================================================================================
# WARNING: PARITY INVARIANT WITH MONACO JAVASCRIPT FRONTEND
# If you modify IPC message handlers, dispatching, or session launch here,
# you MUST also update the corresponding JavaScript / Protocol files:
#   - Monaco Editor Script:     plugin/contrib/scripting/assets/editor/editor.js
#   - JS Script Manager:        plugin/contrib/scripting/assets/editor/scripts_manager.js
#   - IPC Message Protocol:     plugin/scripting/editor_ipc.py
# =========================================================================================

from __future__ import annotations

import logging
import os
import select
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, cast

from plugin.framework.worker_pool import get_subprocess_creationflags

if TYPE_CHECKING:
    import subprocess as subprocess_types

from plugin.framework.thread_guard import background, main_thread_only
from plugin.framework.event_bus import global_event_bus
from plugin.framework.i18n import _
from plugin.framework.queue_executor import QueueExecutor, default_executor
from plugin.framework.worker_pool import BackgroundHandle, run_in_background
from plugin.scripting.editor_ipc import (
    exception_traceback,
    failure_detail,
    failure_message,
    message_type,
    new_session_id,
    session_id_of,
    stamp_session,
    target_from_load,
    target_identity_key,
    write_message,
)
from plugin.scripting.document_scripts import SCRIPT_PICKER_MESSAGE_TYPES, handle_editor_script_message
from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES, read_pickle_frame_with_timeout
from plugin.scripting.venv_worker import (
    _kill_process_tree,
    resolve_venv_python,
    scrub_subprocess_env,
    warm_venv_worker,
    wrap_command_for_sandbox,
)

log = logging.getLogger(__name__)


# --- Launcher ---

_EDITOR_MAIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "editor_main.py")
_ASSETS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "contrib", "scripting", "assets", "editor")
)

_WEBVIEW_PROBE_CODE = """\
import sys
import traceback
try:
    import webview
    import rocher
    print(getattr(webview, "__file__", "ok"))
except Exception:
    traceback.print_exc()
    sys.exit(1)
"""


def build_editor_child_env(*, assets_dir: str | None = None) -> dict[str, str]:
    """Environment for editor subprocess (venv python + GUI session variables)."""
    env = scrub_subprocess_env(dict(os.environ))
    env["WRITERAGENT_EDITOR_ASSETS"] = assets_dir or _ASSETS_DIR
    for key in (
        "DISPLAY",
        "XAUTHORITY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "LD_LIBRARY_PATH",
    ):
        if key in os.environ and key not in env:
            env[key] = os.environ[key]
    return env


def resolve_editor_python(uno_ctx: Any) -> tuple[str | None, str]:
    """Return (venv python executable, error message). Monaco requires a user venv."""
    from plugin.framework.config import get_config_str

    venv_dir = get_config_str("scripting.python_venv_path").strip()
    if not venv_dir:
        return (
            None,
            _(
                "Set the Python venv path in Settings → Python (same venv where you ran "
                "'uv pip install pywebview rocher'). LibreOffice's built-in Python cannot run the Monaco editor."
            ),
        )
    exe = resolve_venv_python(venv_dir)
    if not exe:
        return (
            None,
            _(
                "No python executable found under configured venv: {0} "
                "(expected bin/python, Scripts/python.exe, or env-root python.exe)."
            ).format(venv_dir),
        )
    return exe, ""


_PROBE_CACHE: dict[str, tuple[bool, str]] = {}
# Failures only: (monotonic expiry, result). Successes stay in _PROBE_CACHE
# until the venv path changes.
_PROBE_FAILURE_CACHE: dict[str, tuple[float, tuple[bool, str]]] = {}
# Short on purpose: long enough that a 30s hung probe is not repeated on every
# menu open, short enough that installing pywebview is picked up without
# restarting LibreOffice.
_PROBE_FAILURE_TTL_SEC = 30.0


def probe_webview_import(exe: str) -> tuple[bool, str]:
    """Return whether *exe* can ``import webview`` (pywebview package), with diagnostics.

    Successes stay cached until the venv path changes. Failures are remembered
    for ``_PROBE_FAILURE_TTL_SEC``.
    """
    cached = _PROBE_CACHE.get(exe)
    if cached is not None and cached[0]:
        return cached
    failed = _PROBE_FAILURE_CACHE.get(exe)
    if failed is not None:
        expires_at, result = failed
        if time.monotonic() < expires_at:
            return result
        _PROBE_FAILURE_CACHE.pop(exe, None)

    def _remember_failure(result: tuple[bool, str]) -> tuple[bool, str]:
        # What was wrong: only successes were cached. monaco_editor_available
        # calls this on the UI thread, and subprocess.run waits up to 30s.
        # A slow or hung venv blocked every editor open for that full timeout.
        # A failed probe used to be uncached so installing pywebview was visible
        # immediately; caching it forever brought back the stuck native dialog.
        # Why this works: the failure is reused until the TTL, then probed again.
        _PROBE_FAILURE_CACHE[exe] = (time.monotonic() + _PROBE_FAILURE_TTL_SEC, result)
        return result

    try:
        r = subprocess.run(
            wrap_command_for_sandbox([exe, "-c", _WEBVIEW_PROBE_CODE]),
            capture_output=True,
            timeout=30,
            env=build_editor_child_env(),
            text=True,
            **get_subprocess_creationflags(),
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("probe_webview_import failed for %s: %s", exe, e, exc_info=True)
        return _remember_failure((False, failure_detail(exc=e)))
    detail = (r.stdout or "").strip()
    if r.stderr:
        detail = f"{detail}\n{r.stderr}".strip() if detail else r.stderr.strip()
    if r.returncode == 0:
        res = (True, detail)
        _PROBE_CACHE[exe] = res
        _PROBE_FAILURE_CACHE.pop(exe, None)
        return res
    if not detail:
        detail = f"exit code {r.returncode}"
    log.warning("probe_webview_import: %s returned %s: %s", exe, r.returncode, detail)
    return _remember_failure((False, detail))


def spawn_editor_process(exe: str, *, assets_dir: str | None = None) -> subprocess.Popen[bytes]:
    """Start editor_main.py with stdin/stdout pipes."""
    env = build_editor_child_env(assets_dir=assets_dir)
    popen_kw: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
        "text": False,
        "bufsize": 0,
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        # What was wrong: preexec_fn=os.setsid runs Python in the child between
        # fork and exec. Other LibreOffice threads can hold locks the child
        # inherits, so the editor spawn can deadlock.
        # Why this works: start_new_session asks the C runtime to call setsid()
        # in the child. The process group is unchanged, so terminate() still
        # kills Qt grandchildren via killpg.
        popen_kw["start_new_session"] = True
    return cast("subprocess.Popen[bytes]", subprocess.Popen(wrap_command_for_sandbox([exe, _EDITOR_MAIN]), **popen_kw))


# --- Pipe bridge ---

# RLock is required: set_active_session() holds _SESSION_LOCK while invoking
# _finish() or session transition callbacks, which re-enter _SESSION_LOCK on
# the same thread to clear or check _ACTIVE_SESSION.
_SESSION_LOCK = threading.RLock()
_ACTIVE_SESSION: EditorSession | None = None
# Set on a dirty session after the user picks Don't save. _register_load_session
# refuses to overwrite a live dirty buffer unless this is present.
_REPLACE_CONFIRMED = "replace_confirmed"


_EditorDeliver = Callable[[dict[str, Any]], None]
_EditorStart = Callable[[_EditorDeliver], None]


class DeferredEditorResult:
    """A save whose Monaco frame is delivered later, on the main thread.

    Monaco Run used to call the venv wait inside the UI-thread save handler.
    ``start(deliver)`` schedules that work. ``deliver`` is called once with the
    same dict a synchronous ``on_save`` would have returned.
    """

    _start: _EditorStart

    def __init__(self, start: _EditorStart) -> None:
        self._start = start

    def start(self, deliver: _EditorDeliver) -> None:
        self._start(deliver)


EditorSaveCallback = Callable[..., dict[str, Any] | DeferredEditorResult]


@dataclass
class EditorSessionState:
    """One logical buffer: save callbacks + identity. Process is shared."""

    session_id: str
    mode: str
    target: dict[str, str]
    on_save: EditorSaveCallback | None = None
    on_closed: Callable[[], None] | None = None
    dirty: bool = False
    pending_load: dict[str, Any] | None = None
    pending_on_save: EditorSaveCallback | None = None
    pending_on_closed: Callable[[], None] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    target_key: tuple[str, str, str, str, str, str] = field(init=False)

    def __post_init__(self) -> None:
        self.target_key = target_identity_key(self.mode, self.target)


class PersistentEditor:
    """Manages a single Monaco editor subprocess and keeps it alive in the background."""

    _stdin_lock: threading.Lock
    _stderr_tail_lock: threading.Lock
    _stderr_tail: deque[str]
    _stderr_tail_chars: int
    _stderr_tail_max_chars: int
    _ready_event: threading.Event
    _closed_event: threading.Event
    _save_lock: threading.Lock

    def __init__(self) -> None:
        self._proc: subprocess_types.Popen[bytes] | None = None
        self._stdin_lock = threading.Lock()
        self._reader_thread: BackgroundHandle | None = None
        self._stderr_thread: BackgroundHandle | None = None
        self._stderr_tail_lock = threading.Lock()
        self._stderr_tail = deque[str]()
        self._stderr_tail_chars = 0
        self._stderr_tail_max_chars = 65536
        self._ready_event = threading.Event()
        self._closed_event = threading.Event()
        self._save_lock = threading.Lock()
        self._save_token: object | None = None

        self.sessions: dict[str, EditorSessionState] = {}
        self.focused_id: str | None = None
        self.executor: QueueExecutor = default_executor
        self.ctx: Any = None
        self.run_script_doc: Any = None
        self.run_script_doc_url: str | None = None

    def focused(self) -> EditorSessionState | None:
        if not self.focused_id:
            return None
        return self.sessions.get(self.focused_id)

    def lookup(self, session_id: str) -> EditorSessionState | None:
        sid = (session_id or "").strip()
        if not sid:
            return None
        return self.sessions.get(sid)

    def find_by_target(self, mode: str, target: dict[str, str]) -> EditorSessionState | None:
        key = target_identity_key(mode, target)
        for state in self.sessions.values():
            if state.target_key == key:
                return state
        return None

    def register_session(self, state: EditorSessionState) -> EditorSessionState:
        self.sessions[state.session_id] = state
        self.focused_id = state.session_id
        return state

    def end_session(self, session_id: str, *, call_closed: bool) -> None:
        state = self.sessions.pop(session_id, None)
        if state is None:
            return
        if self.focused_id == session_id:
            self.focused_id = next(iter(self.sessions), None)
        if call_closed and state.on_closed is not None:
            try:
                state.on_closed()
            except Exception:
                log.exception("Editor on_closed failed while ending session %s", session_id)

    def resolve_incoming(self, msg: dict[str, Any]) -> EditorSessionState | None:
        sid = session_id_of(msg)
        if sid:
            state = self.lookup(sid)
            if state is None:
                log.warning("editor_host: ignored message type=%s for unknown session_id=%s", message_type(msg), sid)
            return state
        return self.focused()

    @property
    def is_running(self) -> bool:
        if self._proc is None:
            return False
        return self._proc.poll() is None

    @property
    def proc(self) -> subprocess_types.Popen[bytes] | None:
        return self._proc

    def start(self, proc: subprocess_types.Popen[bytes]) -> None:
        """Start the reader thread for the spawned process."""
        self._proc = proc
        self._ready_event.clear()
        self._closed_event.clear()
        with self._stderr_tail_lock:
            self._stderr_tail.clear()
            self._stderr_tail_chars = 0
        self._reader_thread = run_in_background(self._read_loop, name="editor-pipe-reader", daemon=True, dedicated=True)
        if proc.stderr is not None:
            self._stderr_thread = run_in_background(self._stderr_drain_loop, name="editor-stderr-drain", daemon=True, dedicated=True)

    def terminate(self) -> None:
        """Force terminate the subprocess and its WebEngine children.

        Spawn uses ``start_new_session`` (POSIX ``setsid`` in the child).
        ``proc.terminate()`` leaves Qt grandchildren alive, and a surviving
        parent is then reused as if it were healthy.
        """
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                _kill_process_tree(proc)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except OSError:
            pass
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass

    def send(self, message: dict[str, Any], *, session: EditorSessionState | None = None) -> None:
        """Thread-safe write to child stdin. Session messages get ``session_id`` + ``target``."""
        kind = message_type(message)
        state = session or (self.lookup(session_id_of(message)) if session_id_of(message) else self.focused())
        if kind and kind != "ready" and state is not None:
            message = stamp_session(
                message,
                session_id=state.session_id,
                mode=state.mode or str(message.get("mode") or ""),
                target=state.target,
            )
        with self._stdin_lock:
            if self._proc is None:
                raise RuntimeError("No editor process is running")
            exit_code = self._proc.poll()
            if exit_code is not None:
                detail = self.read_stderr_tail()
                raise RuntimeError(f"Editor process already exited (code={exit_code}). {detail}")
            if self._proc.stdin is None:
                raise RuntimeError("Editor process stdin is closed")
            try:
                write_message(self._proc.stdin, message)
            except BrokenPipeError as e:
                detail = self.read_stderr_tail()
                raise RuntimeError(f"Editor process closed stdin. {detail}") from e

    def read_stderr_tail(self, max_bytes: int = 65536) -> str:
        """Return stderr already captured by the drain thread.

        A second ``select``/read on the same pipe races that thread and can
        drop the traceback from a spawn-failure dialog.
        """
        with self._stderr_tail_lock:
            if not self._stderr_tail:
                return ""
            text = "\n".join(self._stderr_tail)
        if len(text) > max_bytes:
            return text[-max_bytes:].strip()
        return text.strip()

    def _append_stderr_line(self, line: str) -> None:
        if not line:
            return
        with self._stderr_tail_lock:
            self._stderr_tail.append(line)
            # +1 matches the old sum(len(s) + 1) budget (one join newline per line).
            # What was wrong: every popleft recomputed that sum under the lock,
            # so a chatty child was O(n²) while the drain thread held it.
            # Why this works: the same budget is a running total, updated by
            # the appended line and by each dropped line.
            self._stderr_tail_chars += len(line) + 1
            while self._stderr_tail and self._stderr_tail_chars > self._stderr_tail_max_chars:
                dropped = self._stderr_tail.popleft()
                self._stderr_tail_chars -= len(dropped) + 1

    @background
    def _stderr_drain_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        stderr = proc.stderr
        try:
            if sys.platform == "win32":
                # Windows: blocking readline (pipe close on exit unblocks).
                while proc.poll() is None:
                    raw = stderr.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line:
                        log.debug("editor child: %s", line)
                        self._append_stderr_line(line)
            else:
                while proc.poll() is None:
                    ready, _unused_w, _unused_x = select.select([stderr], [], [], 0.5)
                    if not ready:
                        continue
                    raw = stderr.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line:
                        log.debug("editor child: %s", line)
                        self._append_stderr_line(line)
        except Exception:
            log.debug("editor stderr drain failed", exc_info=True)
        finally:
            try:
                remainder = stderr.read()
                if remainder:
                    for piece in remainder.decode("utf-8", errors="replace").splitlines():
                        if piece:
                            log.debug("editor child: %s", piece)
                            self._append_stderr_line(piece)
            except Exception:
                log.debug("editor stderr drain tail read failed", exc_info=True)

    def wait_for_ready(self, ctx: Any, timeout_sec: float = 30.0) -> bool:
        """Wait for ``ready`` while pumping LibreOffice UI events."""
        from plugin.framework.uno_context import process_events_to_idle

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if self._ready_event.is_set():
                return True
            if self._proc is None:
                return False
            exit_code = self._proc.poll()
            if exit_code is not None:
                log.error("Editor child exited before ready (code=%s). stderr=%s", exit_code, self.read_stderr_tail())
                return False
            try:
                process_events_to_idle(ctx)
            except Exception:
                pass
            time.sleep(0.05)
        if not self._ready_event.is_set():
            log.error("Editor ready timeout (%ss). child_running=%s stderr=%s", timeout_sec, self._proc is not None, self.read_stderr_tail())
        return self._ready_event.is_set()

    @background
    def _read_loop(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        proc = self._proc
        stdout = proc.stdout
        try:
            if sys.platform == "win32":
                self._read_loop_blocking(proc, stdout)
            else:
                self._read_loop_select(proc, stdout)
        except Exception:
            log.exception("Editor pipe reader failed")
        finally:
            log.info("editor_host: persistent reader loop finished.")
            still_ours = self._proc is proc
            if still_ours:
                self._handle_disconnect()
            else:
                log.info("editor_host: old reader loop ignored disconnect (superseded by new process)")
            # What was wrong: terminate() ran only when the reader raised.
            # A clean EOF (child closed stdout) while poll() was still None
            # dropped sessions via _handle_disconnect but left the process on
            # self._proc. is_running stayed True, so the next launch reused a
            # child with no reader.
            # Why this works: any exit of this loop that still owns a live
            # process kills it. A child that already exited, or a reader
            # superseded by a new spawn, is left alone.
            if still_ours and proc.poll() is None:
                self.terminate()

    def _read_editor_message(self, proc: subprocess_types.Popen[bytes], stdout: Any) -> dict[str, Any] | None:
        """Read one editor frame with a deadline so a partial length prefix cannot hang."""
        msg = read_pickle_frame_with_timeout(
            stdout,
            30.0,
            max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
            frame_label="editor message",
            require_dict=True,
            is_alive=lambda: proc.poll() is None,
        )
        if msg is None:
            return None
        if not isinstance(msg, dict):
            raise ValueError("Editor message must be a dict")
        return msg

    def _read_loop_select(self, proc: subprocess_types.Popen[bytes], stdout: Any) -> None:
        """POSIX: use select() to poll the pipe with periodic liveness checks."""
        while proc.poll() is None:
            ready, _unused_w, _unused_x = select.select([stdout], [], [], 0.5)
            if not ready:
                continue
            msg = self._read_editor_message(proc, stdout)
            if msg is None:
                break
            if self._proc is proc:
                self._dispatch_incoming(msg)
            else:
                log.warning("editor_host: ignored incoming message from old process")

    def _read_loop_blocking(self, proc: subprocess_types.Popen[bytes], stdout: Any) -> None:
        """Windows: peek-with-timeout so a partial frame cannot block ReadFile forever."""
        while proc.poll() is None:
            try:
                msg = self._read_editor_message(proc, stdout)
            except subprocess.TimeoutExpired:
                # Idle pipe: no bytes consumed, keep waiting. A stalled partial
                # frame has already been pulled into the helper's buffer, so the
                # next read raises and the reader exits.
                continue
            if msg is None:
                break
            if self._proc is proc:
                self._dispatch_incoming(msg)
            else:
                log.warning("editor_host: ignored incoming message from old process")

    def _marshal_timeout(self) -> float:
        """UI wait for a save/close. The script budget can be 600s; 60s used to overlap it."""
        from plugin.scripting.config_limits import configured_python_exec_timeout

        return float(configured_python_exec_timeout()) + 5.0

    def set_run_script_document(self, doc: Any | None) -> None:
        from plugin.scripting.document_scripts import document_scripts_identity

        self.run_script_doc = doc
        self.run_script_doc_url = document_scripts_identity(doc) if doc is not None else None

    def _dispatch_incoming(self, msg: dict[str, Any]) -> None:
        kind = message_type(msg)
        if kind in SCRIPT_PICKER_MESSAGE_TYPES:

            def _handle_picker() -> None:
                try:
                    handle_editor_script_message(
                        kind,
                        msg,
                        ctx=self.ctx,
                        # Launch document, not whichever window is focused now.
                        # _resolve_run_script_doc used to return the active doc, so a
                        # focus change saved the script into the newly focused file.
                        session_doc=self.run_script_doc,
                        session_doc_url=self.run_script_doc_url,
                        send=self.send,
                    )
                except Exception as e:
                    # What was wrong: only TimeoutError from executor.execute was
                    # caught. Any other exception (disposed document, config/UNO
                    # error, send() rejecting a payload over 16 MB) left
                    # _dispatch_incoming, the reader loop's blanket except marked
                    # the reader failed, and terminate() killed the editor —
                    # unsaved buffer included. Save and close already wrap their
                    # bodies; the picker did not.
                    # Why this works: the picker reports the error on the pipe
                    # and the reader stays up. A second failure while sending
                    # that frame is logged here so it cannot escape either.
                    log.exception("Editor script picker failed")
                    try:
                        self.send({"type": "error", "message": str(e), "traceback": exception_traceback(e)})
                    except Exception:
                        log.exception("Editor script picker could not send the error frame")

            try:
                # What was wrong: save and close pass timeout=_marshal_timeout(),
                # but the script picker called execute() with no timeout. The
                # pipe reader blocks inside that call, so a picker the UI thread
                # never starts held Monaco's reader for QueueExecutor's 30s
                # default. The TimeoutError branch below never saw the same
                # budget as save/close. Why this works: the reader gives up on
                # that shared deadline and reports the timeout.
                self.executor.execute(_handle_picker, timeout=self._marshal_timeout())
            except TimeoutError:
                log.exception("Editor script picker timed out")
                self.send({"type": "error", "message": _("The script list update timed out.")})
            return

        if kind == "dirty":
            state = self.resolve_incoming(msg)
            if state is not None:
                state.dirty = bool(msg.get("dirty"))
            return

        if kind == "save":
            state = self.resolve_incoming(msg)
            if state is None:
                return
            code = msg.get("code")
            if not isinstance(code, str):
                code = ""

            save_as_plain = bool(msg.get("save_as_plain"))
            data_binding = msg.get("data_binding")
            if data_binding is not None and not isinstance(data_binding, str):
                data_binding = str(data_binding)
            action = msg.get("action", "cell_save")
            if not isinstance(action, str):
                action = "cell_save"
            captured = state
            token = object()
            self._save_token = token

            def _send_save(payload: dict[str, Any]) -> None:
                with self._save_lock:
                    if self._save_token is not token:
                        return
                    # A deferred Run delivers on a later UI turn. A newer save
                    # replaces this token, so a late result must not clear dirty.
                    if payload.get("type") == "saved" or payload.get("ok"):
                        captured.dirty = False
                    self.send(payload, session=captured)

            def _handle_save() -> None:
                try:
                    on_save = captured.on_save
                    if on_save is not None:
                        result = on_save(code, save_as_plain, data_binding, action)
                    else:
                        result = {"type": "saved", "ok": True}
                    if isinstance(result, DeferredEditorResult):
                        # What was wrong: Monaco Run ran execute_and_insert_result
                        # inside this UI-thread handler. The venv wait froze
                        # LibreOffice for the whole script.
                        # How: the pipe reader blocks in executor.execute until
                        # on_save returns, and on_save waited for the venv.
                        # Why this works: on_save returns a DeferredEditorResult
                        # after the fast library write. start() uses the native
                        # prepare/finish split, so only the venv wait leaves the
                        # UI thread. The saved/error frame is sent when it
                        # finishes. pending_load stays for the Save click that
                        # queued a mode switch; this Run must not consume it.
                        def deliver(payload: dict[str, Any]) -> None:
                            if not isinstance(payload, dict):
                                payload = {"type": "error", "message": _("Script execution failed.")}
                            try:
                                _send_save(payload)
                            except Exception:
                                log.exception("Editor run result could not be sent")

                        try:
                            result.start(deliver)
                        except Exception as e:
                            log.exception("Editor run failed to start")
                            _send_save(
                                {"type": "error", "message": str(e), "traceback": exception_traceback(e)},
                            )
                        return
                    if not isinstance(result, dict):
                        result = {"type": "saved", "ok": True}
                    if result.get("type") == "saved" or result.get("ok"):
                        captured.dirty = False
                    _send_save(result)
                    pending = captured.pending_load
                    if pending is not None and (result.get("type") == "saved" or result.get("ok")):
                        next_on_save = captured.pending_on_save
                        next_on_closed = captured.pending_on_closed
                        captured.pending_load = None
                        captured.pending_on_save = None
                        captured.pending_on_closed = None
                        self.end_session(captured.session_id, call_closed=True)
                        if next_on_save is not None:
                            _activate_load(pending, next_on_save, next_on_closed or (lambda: None))
                except Exception as e:
                    # What was wrong: a failure while sending the save error
                    # frame escaped the reader and tore the editor down.
                    # How it happened: the save body was wrapped, but
                    # _send_save in this except was not. send() rejecting the
                    # payload (over 16 MB, or a pipe that already closed)
                    # left _handle_save, executor.execute re-raised it, and
                    # the reader loop's blanket except called terminate().
                    # Why this works: the same nested guard as the script
                    # picker logs a second failure and keeps the reader up.
                    log.exception("Editor save handler failed")
                    try:
                        _send_save(
                            {"type": "error", "message": str(e), "traceback": exception_traceback(e)},
                        )
                    except Exception:
                        log.exception("Editor save handler could not send the error frame")

            try:
                self.executor.execute(_handle_save, timeout=self._marshal_timeout())
            except TimeoutError:
                log.exception("Editor save handler timed out")
                with self._save_lock:
                    self._save_token = None
                self.send(
                    {"type": "error", "message": _("Saving the script timed out.")},
                    session=captured,
                )
            return

        if kind in ("closed", "cancel"):
            log.info("editor_host _dispatch_incoming: received close/cancel kind=%r", kind)
            state = self.resolve_incoming(msg)
            captured_id = state.session_id if state is not None else ""
            captured_on_closed = state.on_closed if state is not None else None

            def _handle_close() -> None:
                try:
                    if captured_on_closed is not None:
                        captured_on_closed()
                except Exception:
                    log.exception("Editor on_closed failed")
                finally:
                    self._closed_event.set()
                    live = self.lookup(captured_id) if captured_id else None
                    if live is not None and live.on_closed is captured_on_closed:
                        self.sessions.pop(captured_id, None)
                        if self.focused_id == captured_id:
                            self.focused_id = next(iter(self.sessions), None)
                        if not self.sessions:
                            set_active_session(None)

            try:
                self.executor.execute(_handle_close, timeout=self._marshal_timeout())
            except TimeoutError:
                # A busy UI thread used to raise out of the pipe reader and
                # terminate the editor child.
                log.exception("Editor close handler timed out")
            return

        if kind == "ready":
            self._ready_event.set()
            return
        log.debug("Editor child message: %s", kind)

    def _handle_disconnect(self) -> None:
        """Handle case where the subprocess exits or disconnects unexpectedly."""
        snapshot = list(self.sessions.values())

        def _handle_close() -> None:
            try:
                for state in snapshot:
                    if state.on_closed is not None:
                        try:
                            state.on_closed()
                        except Exception:
                            log.exception("Editor on_closed failed during disconnect")
            finally:
                self._closed_event.set()
                for state in snapshot:
                    self.sessions.pop(state.session_id, None)
                if not self.sessions:
                    self.focused_id = None
                    # What was wrong: unexpected exit cleared the sessions and
                    # the active session but left run_script_doc /
                    # run_script_doc_url pointing at the launch document.
                    # How it happened: _handle_disconnect copied the session
                    # cleanup and skipped the two fields
                    # terminate_persistent_editor clears. The picker still reads
                    # them, so a later message targeted that document and the
                    # UNO reference kept it alive.
                    # Why this works: the empty-session path drops both fields.
                    # A replacement session registered before this callback is
                    # left alone, including a launch document it already set.
                    self.run_script_doc = None
                    self.run_script_doc_url = None
                    set_active_session(None)

        try:
            self.executor.execute(_handle_close, timeout=self._marshal_timeout())
        except TimeoutError:
            log.exception("Editor disconnect handler timed out")


_PERSISTENT_EDITOR = PersistentEditor()


class EditorSession:
    """One editor session wrapper, delegating to the PersistentEditor singleton."""

    _proc: subprocess_types.Popen[bytes]
    _on_save: EditorSaveCallback
    _on_closed: Callable[[], None]
    _executor: QueueExecutor
    session_id: str

    def __init__(
        self,
        proc: "subprocess_types.Popen[bytes]",
        *,
        on_save: EditorSaveCallback,
        on_closed: Callable[[], None],
        executor: QueueExecutor | None = None,
        session_id: str = "",
    ) -> None:
        self._proc = proc
        self._on_save = on_save
        self._on_closed = on_closed
        self._executor = executor or default_executor
        self.session_id = session_id

        _PERSISTENT_EDITOR.executor = self._executor

    @property
    def is_running(self) -> bool:
        return _PERSISTENT_EDITOR.is_running

    def start_reader(self) -> None:
        if _PERSISTENT_EDITOR.proc is not self._proc:
            _PERSISTENT_EDITOR.start(self._proc)

    def send(self, message: dict[str, Any]) -> None:
        _PERSISTENT_EDITOR.send(message)

    def read_stderr_tail(self, max_bytes: int = 65536) -> str:
        return _PERSISTENT_EDITOR.read_stderr_tail(max_bytes)

    def wait_for_ready(self, ctx: Any, timeout_sec: float = 30.0) -> bool:
        return _PERSISTENT_EDITOR.wait_for_ready(ctx, timeout_sec)

    def _finish(self) -> None:
        if self.session_id:
            live = _PERSISTENT_EDITOR.lookup(self.session_id)
            if live is not None and live.on_save is self._on_save:
                # What was wrong: every replace ended the session here with
                # call_closed=False, before on_save and before on_closed.
                # How: launch_monaco_editor calls set_active_session first, so
                # the focused buffer was already gone when load overwrote it.
                # A dirty calc cell, Run Script, init script, or LaTeX buffer
                # was discarded with nothing written back.
                # Why this works: a dirty window that is still running stays
                # registered. launch_monaco_editor confirms or queues a save
                # before it gets here. Discard sets replace_confirmed, and
                # _register_load_session ends that target with on_closed.
                # A dead process still ends here so the map does not keep a
                # session whose window is gone.
                if not (live.dirty is True and _PERSISTENT_EDITOR.is_running):
                    _PERSISTENT_EDITOR.end_session(self.session_id, call_closed=False)

        global _ACTIVE_SESSION
        with _SESSION_LOCK:
            if _ACTIVE_SESSION is self:
                _ACTIVE_SESSION = None


def get_active_session() -> EditorSession | None:
    with _SESSION_LOCK:
        return _ACTIVE_SESSION


def set_active_session(session: EditorSession | None) -> None:
    global _ACTIVE_SESSION
    with _SESSION_LOCK:
        # _finish keeps a dirty running buffer. The confirm/flush gate in
        # launch_monaco_editor runs before this, so a replace does not drop
        # unsaved edits on the way into _register_load_session.
        if session is not None and _ACTIVE_SESSION is not None and _ACTIVE_SESSION is not session:
            _ACTIVE_SESSION._finish()
        if session is None and _ACTIVE_SESSION is not None:
            _ACTIVE_SESSION._finish()
        _ACTIVE_SESSION = session


def terminate_persistent_editor() -> None:
    """Force terminate the background Monaco editor process."""
    # What was wrong: sessions and focused_id were cleared with no lock, and
    # run_script_doc / run_script_doc_url kept pointing at the document the
    # dead editor was launched for. The picker reads those two fields, so a
    # later message still targeted that document.
    # Why this works: the clear holds the same lock as the active session,
    # and both document fields go back to None. terminate() stays outside
    # the lock so a reader blocked in set_active_session is not stuck behind
    # the process wait.
    with _SESSION_LOCK:
        _PERSISTENT_EDITOR.sessions.clear()
        _PERSISTENT_EDITOR.focused_id = None
        _PERSISTENT_EDITOR.run_script_doc = None
        _PERSISTENT_EDITOR.run_script_doc_url = None
    _PERSISTENT_EDITOR.terminate()


def _on_config_changed(**kwargs: Any) -> None:
    key = kwargs.get("key", "")
    changed = kwargs.get("keys") or ()
    # Settings OK emits one event with key="" and the changed names in keys.
    # Matching only key dropped a venv-path change that was saved with other fields.
    if key == "scripting.python_venv_path" or "scripting.python_venv_path" in changed:
        log.info("editor_host: scripting.python_venv_path changed, terminating background Monaco process")
        _PROBE_CACHE.clear()
        _PROBE_FAILURE_CACHE.clear()
        terminate_persistent_editor()
        try:
            from plugin.vision.vision_availability import invalidate_vision_availability_cache

            invalidate_vision_availability_cache()
        except Exception:
            log.debug("vision availability cache invalidation failed", exc_info=True)


global_event_bus.subscribe("config:changed", _on_config_changed)


# --- Session launch ---


def monaco_editor_available(ctx: Any) -> tuple[str | None, bool]:
    """Return (venv python exe, True) when Monaco can launch, else (exe or None, False)."""
    from plugin.framework.config import get_config
    if get_config("scripting.force_internal_script_editor"):
        log.debug("monaco_editor_available: bypassed by scripting.force_internal_script_editor")
        return None, False

    exe, err = resolve_editor_python(ctx)
    if not exe:
        log.debug("monaco_editor_available: no venv python (%s)", err)
        return None, False
    if _PERSISTENT_EDITOR.is_running:
        log.debug("monaco_editor_available: Monaco editor process already running, skipping probe")
        return exe, True
    webview_ok, detail = probe_webview_import(exe)
    if not webview_ok:
        log.debug("monaco_editor_available: webview probe failed for %s: %s", exe, detail[:200] if detail else "")
        return exe, False
    return exe, True


def monaco_open_expected(ctx: Any) -> tuple[str | None, bool]:
    """Return (venv python exe, True) when Run Python Script should use Monaco."""
    exe, ok = monaco_editor_available(ctx)
    return exe, ok and bool(exe)


def monaco_session_needs_flush() -> bool:
    """True when the running Monaco window has unsaved edits, in any mode."""
    editor = _PERSISTENT_EDITOR
    if not editor.is_running:
        return False
    focused = editor.focused()
    if focused is None:
        return False
    # ``is True`` so a MagicMock stand-in for the editor does not look dirty.
    return focused.dirty is True


def calc_cell_session_needs_flush() -> bool:
    """True when the running Monaco buffer has unsaved edits.

    What was wrong: this returned True only for ``mode == "calc_cell"``.
    Opening a cell while Run Python Script, the init script, or LaTeX was
    dirty skipped the confirm. ``run_python_dialog`` never called this, so a
    dirty cell was dropped the other way when Run Python Script opened.
    How: ``launch_monaco_editor`` then ``set_active_session`` → ``_finish``
    → ``end_session(call_closed=False)``, and the next load overwrote the
    buffer. ``on_save`` never ran, so the edits were not written.
    Why this works: any dirty focused buffer counts. ``launch_monaco_editor``
    asks before replace, for every mode, in both directions.
    """
    return monaco_session_needs_flush()


def focused_monaco_buffer_label() -> str:
    """Short name for the unsaved-changes question (cell, script, or formula)."""
    focused = _PERSISTENT_EDITOR.focused()
    if focused is None:
        return ""
    target = focused.target
    if focused.mode == "calc_cell":
        return target.get("cell_address", "")
    if focused.mode == "run_script":
        return target.get("script_name", "")
    if focused.mode == "init_script":
        return _("the initialization script")
    if focused.mode == "latex":
        resource = target.get("resource", "")
        if resource and resource != "insert":
            return resource
        return _("the LaTeX formula")
    return target.get("resource", "")


def last_calc_cell_address() -> str:
    focused = _PERSISTENT_EDITOR.focused()
    if focused is None:
        return ""
    return focused.target.get("cell_address", "")


def _dirty_blocks_replace(state: EditorSessionState) -> bool:
    """True when *state* is a live unsaved buffer the user has not discarded."""
    if state.dirty is not True:
        return False
    if state.extra.get(_REPLACE_CONFIRMED) is True:
        return False
    return _PERSISTENT_EDITOR.is_running


class DirtyBufferError(RuntimeError):
    """Refused to replace a live Monaco buffer that still has unsaved edits."""


@main_thread_only
def confirm_unsaved_monaco_edit(ctx: Any, label: str) -> str:
    """Ask Save / Don't save / Cancel. Returns ``save``, ``discard``, or ``cancel``."""
    from plugin.framework.uno_context import get_desktop, product_display_name

    title = product_display_name(ctx)
    where = label.strip() if label and label.strip() else _("this editor")
    message = _(
        "Save changes to {0}?\n\nYes saves them before switching. No discards them. Cancel keeps the current editor."
    ).format(where)
    try:
        desktop = get_desktop(ctx)
        frame = desktop.getCurrentFrame() if desktop is not None else None
        window = frame.getContainerWindow() if frame is not None else None
        if window is None:
            return "cancel"
        smgr = ctx.getServiceManager()
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        # QUERYBOX=4, BUTTONS_YES_NO_CANCEL=4. Results: YES=2, NO=3, CANCEL=0.
        box = toolkit.createMessageBox(window, 4, 4, title, message)
        result = int(box.execute())
        if result == 2:
            return "save"
        if result == 3:
            return "discard"
        return "cancel"
    except Exception:
        log.exception("confirm_unsaved_monaco_edit failed")
        return "cancel"


def prepare_monaco_buffer_replace(
    ctx: Any,
    load_message: dict[str, Any],
    on_save: EditorSaveCallback,
    on_closed: Callable[[], None],
) -> str:
    """Confirm or queue a save before a load replaces the focused buffer.

    Returns ``proceed`` (nothing dirty, or the user discarded), ``queued``
    (request_save is in flight; do not load yet), or ``cancel``.
    A second call after discard returns ``proceed`` without asking again.
    """
    if not monaco_session_needs_flush():
        return "proceed"
    focused = _PERSISTENT_EDITOR.focused()
    if focused is None:
        return "proceed"
    if focused.extra.get(_REPLACE_CONFIRMED) is True:
        return "proceed"
    choice = confirm_unsaved_monaco_edit(ctx, focused_monaco_buffer_label())
    if choice == "cancel":
        return "cancel"
    if choice == "save":
        queue_save_then_load(load_message, on_save, on_closed)
        return "queued"
    focused.extra[_REPLACE_CONFIRMED] = True
    return "proceed"


def _register_load_session(
    ipc_message: dict[str, Any],
    on_save: EditorSaveCallback,
    on_closed: Callable[[], None],
) -> tuple[EditorSessionState, dict[str, Any]]:
    """Bind *ipc_message* to a session (reuse same target; replace focused if different)."""
    mode = str(ipc_message.get("mode") or "calc_cell")
    target = target_from_load(ipc_message)
    ipc_message["mode"] = mode
    ipc_message["target"] = target

    existing = _PERSISTENT_EDITOR.find_by_target(mode, target)
    focused = _PERSISTENT_EDITOR.focused()
    blocked = existing if existing is not None and _dirty_blocks_replace(existing) else None
    if blocked is None and existing is None and focused is not None and _dirty_blocks_replace(focused):
        blocked = focused
    if blocked is not None:
        # What was wrong: switching modes ended the focused session and the
        # load overwrote the buffer. The only confirm covered calc_cell, and
        # only when opening another cell. Run Script, init, and LaTeX edits
        # were dropped, and Run Script dropped a dirty cell.
        # How: set_active_session → _finish used call_closed=False, so neither
        # on_save nor on_closed ran before the new code replaced the editor.
        # Why this works: launch_monaco_editor asks first. Save queues
        # request_save and does not reach here until that save clears dirty.
        # Discard sets replace_confirmed. Anything else keeps the buffer.
        raise DirtyBufferError(
            f"Refusing to replace dirty Monaco session {blocked.session_id} ({blocked.mode})"
        )

    if mode == "run_script" and "run_script_doc" in ipc_message:
        # Queued save-then-load still carries the document. The direct launch
        # path pops it before this call and has already stored it.
        _PERSISTENT_EDITOR.set_run_script_document(ipc_message.get("run_script_doc"))
    ipc_message.pop("run_script_doc", None)
    ipc_message.pop("doc", None)

    if existing is not None:
        existing.on_save = on_save
        existing.on_closed = on_closed
        existing.target = target
        existing.mode = mode
        existing.dirty = False
        existing.extra.pop(_REPLACE_CONFIRMED, None)
        # What was wrong: reuse reset dirty but left pending_load /
        # pending_on_save / pending_on_closed. A queue_save_then_load still
        # in flight then applied that stale load on a later unrelated save.
        # Why this works: reuse means this target is current; the queued
        # switch is no longer the next buffer.
        existing.pending_load = None
        existing.pending_on_save = None
        existing.pending_on_closed = None
        state = existing
    else:
        if focused is not None:
            # One visible buffer: switching targets ends the previous session.
            _PERSISTENT_EDITOR.end_session(focused.session_id, call_closed=True)
        state = EditorSessionState(
            session_id=new_session_id(),
            mode=mode,
            target=target,
            on_save=on_save,
            on_closed=on_closed,
        )
        _PERSISTENT_EDITOR.register_session(state)

    _PERSISTENT_EDITOR.focused_id = state.session_id
    stamped = stamp_session(ipc_message, session_id=state.session_id, mode=mode, target=target)
    return state, stamped


def _activate_load(
    ipc_message: dict[str, Any],
    on_save: EditorSaveCallback,
    on_closed: Callable[[], None],
) -> EditorSessionState:
    """Register or reuse a session and send ``load`` (process already up)."""
    state, stamped = _register_load_session(ipc_message, on_save, on_closed)
    _PERSISTENT_EDITOR.send(stamped, session=state)
    return state


def queue_save_then_load(
    load_message: dict[str, Any],
    on_save: EditorSaveCallback,
    on_closed: Callable[[], None],
) -> None:
    """Ask Monaco to save the current buffer, then load *load_message*."""
    from plugin.scripting.editor_ui_strings import enrich_monaco_load_message

    focused = _PERSISTENT_EDITOR.focused()
    if focused is None:
        return
    focused.pending_load = enrich_monaco_load_message(dict(load_message))
    focused.pending_on_save = on_save
    focused.pending_on_closed = on_closed
    try:
        _PERSISTENT_EDITOR.send({"type": "request_save"}, session=focused)
    except Exception:
        # The queued load must not fire on a later unrelated save when the
        # request never reached the child.
        focused.pending_load = None
        focused.pending_on_save = None
        focused.pending_on_closed = None
        raise


def launch_monaco_editor(
    ctx: Any,
    *,
    exe: str,
    load_message: dict[str, Any],
    on_save: EditorSaveCallback,
    on_closed: Callable[[], None] | None = None,
) -> bool:
    """Start or reuse the Monaco child and send *load_message*.

    Returns True when the editor matches the request: the load was sent, the
    user kept the current buffer, or a save-then-load was queued. Returns False
    only when the child failed to start or the load could not be sent.
    """
    from plugin.chatbot.dialogs import msgbox_with_report
    from plugin.framework.uno_context import product_display_name
    from plugin.scripting.editor_ui_strings import enrich_monaco_load_message

    title = product_display_name(ctx)

    _PERSISTENT_EDITOR.ctx = ctx
    # Host-only keys (e.g. pyuno document refs) must not cross the pickle IPC boundary.
    ipc_message = enrich_monaco_load_message(dict(load_message))
    closed_handler = on_closed if on_closed is not None else (lambda: None)

    # Inject current LO theme so the child Monaco + toolbar chrome automatically
    # match the LibreOffice light/dark (and basic surface color) without any
    # user setting or toggle in the editor. This is computed from the active
    # window's StyleSettings (same heuristic used by the chat sidebar).
    # We always recompute on send so that switching cells or reopening picks
    # up a theme change. See plugin/framework/appearance.py.
    if "theme" not in ipc_message:
        try:
            from plugin.framework.appearance import get_monaco_theme_info

            # Prefer any doc we had for the run_script case or if caller left it in msg.
            # Leave run_script_doc on the message until after the unsaved-changes
            # question. Cancel must not retarget the script picker document.
            doc_for_theme = ipc_message.get("doc") or ipc_message.get("run_script_doc")
            ipc_message["theme"] = get_monaco_theme_info(doc=doc_for_theme, ctx=ctx)
        except Exception:
            log.debug("Failed to compute monaco theme info; falling back to light", exc_info=True)
            ipc_message["theme"] = {"monaco": "vs", "is_dark": False}

    # What was wrong: this function replaced the focused buffer before anyone
    # asked. The cell editor only asked for mode calc_cell, and Run Python
    # Script never asked, so each direction dropped the other buffer.
    # Why this works: one gate covers cell, Run Script, init script, and LaTeX.
    # Cancel and queued save return before set_active_session. Discard is
    # marked on the session so _register_load_session may replace it.
    decision = prepare_monaco_buffer_replace(ctx, ipc_message, on_save, closed_handler)
    if decision != "proceed":
        return True

    if ipc_message.get("mode") == "run_script":
        run_script_doc = ipc_message.pop("run_script_doc", None)
        _PERSISTENT_EDITOR.set_run_script_document(run_script_doc)

    if _PERSISTENT_EDITOR.is_running:
        log.info("editor_host: reusing running Monaco background process")
        proc = _PERSISTENT_EDITOR.proc
        assert proc is not None
        session = EditorSession(proc, on_save=on_save, on_closed=closed_handler)
        set_active_session(session)
    else:
        log.info("editor_host: spawning new Monaco background process")
        try:
            proc = spawn_editor_process(exe)
        except OSError as e:
            log.exception("Failed to spawn editor")
            msg = failure_message(_("Could not start the Python editor."), detail=exception_traceback(e))
            msgbox_with_report(ctx, title, msg, box_type=3, reportable=True, report_title="Python editor spawn failed", report_extra=msg)
            return False

        session = EditorSession(proc, on_save=on_save, on_closed=closed_handler)
        set_active_session(session)
        session.start_reader()

        if not session.wait_for_ready(ctx, timeout_sec=45.0):
            detail = session.read_stderr_tail()
            # set_active_session(None) does not kill the child. The next open
            # saw is_running and skipped wait_for_ready.
            terminate_persistent_editor()
            set_active_session(None)
            msg = failure_message(_("The Python editor window did not start."), detail=detail)
            msgbox_with_report(ctx, title, msg, box_type=3, reportable=True, report_title="Python editor did not start", report_extra=msg)
            return False

        # Trigger background pre-warming of the venv subprocess now that Monaco is successfully up.
        run_in_background(warm_venv_worker, ctx, name="warm-venv-worker", dedicated=True)

    if not session.is_running:
        detail = session.read_stderr_tail()
        set_active_session(None)
        msg = failure_message(_("The Python editor exited before it could load your code."), detail=detail)
        msgbox_with_report(
            ctx,
            title,
            msg,
            box_type=3,
            reportable=True,
            report_title="Python editor exited early",
            report_extra=msg,
        )
        return False

    try:
        state, stamped = _register_load_session(ipc_message, on_save, closed_handler)
        session.session_id = state.session_id
        session.send(stamped)
    except DirtyBufferError:
        # The confirm gate should have queued, cancelled, or marked discard.
        # Killing the child here would throw away the buffer we just refused
        # to replace.
        log.error("editor_host: refused to replace a dirty Monaco buffer")
        return True
    except Exception as e:
        log.exception("Failed to send load to editor")
        ipc_detail = "\n\n".join(filter(None, [session.read_stderr_tail(), exception_traceback(e)]))
        # Same as the ready-timeout path: a live child with no session makes
        # the next open skip the webview probe.
        terminate_persistent_editor()
        set_active_session(None)
        msg = failure_message(_("Could not talk to the Python editor."), detail=ipc_detail)
        msgbox_with_report(
            ctx,
            title,
            msg,
            box_type=3,
            reportable=True,
            report_title="Python editor IPC failed",
            report_extra=msg,
        )
        return False

    return True
