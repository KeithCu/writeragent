# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Monaco editor host: spawn child process, pipe bridge, and session launch.

The open editor buffer is the focused `EditorSessionState` in `PersistentEditor.sessions`; nothing else holds it.

Concurrency:
_PROBE_LOCK guards the webview probe caches (_PROBE_CACHE, _PROBE_FAILURE_CACHE),
_probe_generation, _probe_wanted, and _probe_running. UI thread code only reads
cached results via cached_webview_probe and arms checks via _ensure_webview_probe.
The webview-probe worker thread is the only caller that runs ManagedSubprocess.run
for this probe and the only writer that stores results into the probe caches.
_PROBE_LOCK is never held across ManagedSubprocess.run.
"""

# =========================================================================================
# WARNING: PARITY INVARIANT WITH MONACO JAVASCRIPT FRONTEND
# If you modify IPC message handlers, dispatching, or session launch here,
# you MUST also update the corresponding JavaScript / Protocol files:
#   - Monaco Editor Script:     plugin/contrib/scripting/assets/editor/editor_shell.js
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
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import IO, Any, Callable, cast
from plugin.framework.thread_guard import background, main_thread_only
from plugin.framework.event_bus import get_event_bus
from plugin.framework.i18n import _
from plugin.framework.queue_executor import MainThread, main
from plugin.framework.worker_pool import BackgroundHandle, run_in_background
from plugin.scripting.config_limits import VENV_IPC_WRITE_TIMEOUT_SEC
from plugin.scripting.editor_errors import (
    exception_traceback,
    failure_detail,
    failure_message,
)
from plugin.scripting.editor_ipc import (
    PARENT_TO_CHILD_TYPES,
    message_type,
    pack_message,
    session_id_of,
    stamp_session,
    target_from_load,
    target_identity_key,
)
from plugin.scripting.document_scripts import SCRIPT_PICKER_MESSAGE_TYPES, handle_editor_script_message
from plugin.framework.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    read_pickle_frame,
    write_packed_frame_with_timeout,
)
from plugin.framework.subprocess_env import scrub_subprocess_env
from plugin.scripting.sandbox import resolve_venv_python, wrap_command_for_sandbox
from plugin.scripting.venv_worker import (
    warm_venv_worker,
)
from plugin.framework.managed_subprocess import ManagedSubprocess, kill_process_tree

log = logging.getLogger(__name__)

# Toolkit createMessageBox constants
_BOX_QUERYBOX = 4
_BOX_BUTTONS_YES_NO_CANCEL = 4
_BOX_RESULT_YES = 2
_BOX_RESULT_NO = 3


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

# CodeMirror ships in the extension (assets/editor/cm6), so only pywebview is needed.
_WEBVIEW_PROBE_CODE_CODEMIRROR = _WEBVIEW_PROBE_CODE.replace("    import rocher\n", "")

EDITOR_BACKENDS = ("monaco", "codemirror")


def selected_editor_backend() -> str:
    """Return the configured web editor (scripting.python_editor): "monaco" or "codemirror"."""
    from plugin.framework.config import get_config

    value = str(get_config("scripting.python_editor") or "").strip().lower()
    return value if value in EDITOR_BACKENDS else "monaco"


def build_editor_child_env() -> dict[str, str]:
    """Environment for editor subprocess (venv python + GUI session variables)."""
    env = scrub_subprocess_env(dict(os.environ), keep=("LD_LIBRARY_PATH",))
    env["WRITERAGENT_EDITOR_ASSETS"] = _ASSETS_DIR
    env["WRITERAGENT_EDITOR_BACKEND"] = selected_editor_backend()
    for key in (
        "DISPLAY",
        "XAUTHORITY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    ):
        if key in os.environ and key not in env:
            env[key] = os.environ[key]
    return env


def resolve_editor_python() -> tuple[str | None, str]:
    """Return (venv python executable, error message). Monaco requires a user venv."""
    from plugin.framework.config import get_config

    venv_dir = str(get_config("scripting.python_venv_path") or "").strip()
    if not venv_dir:
        return (
            None,
            _(
                "Set the Python venv path in Settings → Python (same venv where you ran "
                "'uv pip install pywebview rocher'). LibreOffice's built-in Python cannot run the editor."
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


_PROBE_LOCK = threading.Lock()
_probe_generation: int = 0
_probe_wanted: str | None = None
_probe_running: bool = False

_PROBE_CACHE: dict[str, tuple[bool, str]] = {}
# Failures only: (monotonic expiry, result). Successes stay in _PROBE_CACHE
# until the venv path changes.
_PROBE_FAILURE_CACHE: dict[str, tuple[float, tuple[bool, str]]] = {}
# Short on purpose: long enough that a 30s hung probe is not repeated on every
# menu open, short enough that installing pywebview is picked up without
# restarting LibreOffice.
_PROBE_FAILURE_TTL_SEC = 30.0


def _cached_probe_locked(exe: str) -> tuple[bool, str] | None:
    cached = _PROBE_CACHE.get(exe)
    if cached is not None and cached[0]:
        return cached
    failed = _PROBE_FAILURE_CACHE.get(exe)
    if failed is not None:
        expires_at, result = failed
        if time.monotonic() < expires_at:
            return result
        _PROBE_FAILURE_CACHE.pop(exe, None)
    return None


def reset_webview_probe_state() -> None:
    """Reset webview probe caches and worker state (testing only)."""
    global _probe_generation, _probe_wanted, _probe_running
    with _PROBE_LOCK:
        _PROBE_CACHE.clear()
        _PROBE_FAILURE_CACHE.clear()
        _probe_generation = 0
        _probe_wanted = None
        _probe_running = False


def cached_webview_probe(exe: str) -> tuple[bool, str] | None:
    """Return cached webview probe result for *exe*, or None if not cached or expired."""
    with _PROBE_LOCK:
        return _cached_probe_locked(exe)


def probe_webview_import(exe: str, *, apply_generation: int) -> tuple[bool, str]:
    """Return whether *exe* can ``import webview`` (pywebview package), with diagnostics.

    Blocks up to 30 s; call only from the ``webview-probe`` worker.
    Monaco also needs ``rocher`` (Monaco's files); CodeMirror does not.
    Successes stay cached until the venv path or scripting.python_editor changes. Failures are remembered
    for ``_PROBE_FAILURE_TTL_SEC``.
    """
    cached = cached_webview_probe(exe)
    if cached is not None:
        return cached

    def _remember_failure(result: tuple[bool, str]) -> tuple[bool, str]:
        # Failed probes are cached for a TTL so a broken or slow venv does not repeatedly trigger
        # background subprocess probes on every editor check. Once the TTL expires, the next check
        # can arm a fresh probe if the user has installed the missing packages.
        with _PROBE_LOCK:
            if apply_generation == _probe_generation:
                _PROBE_FAILURE_CACHE[exe] = (time.monotonic() + _PROBE_FAILURE_TTL_SEC, result)
        return result

    env = build_editor_child_env()
    probe_code = _WEBVIEW_PROBE_CODE_CODEMIRROR if env["WRITERAGENT_EDITOR_BACKEND"] == "codemirror" else _WEBVIEW_PROBE_CODE
    try:
        r = ManagedSubprocess.run(
            wrap_command_for_sandbox([exe, "-c", probe_code], env=env),
            capture_output=True,
            timeout=30,
            env=env,
            text=True,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("probe_webview_import failed for %s: %s", exe, e, exc_info=True)
        return _remember_failure((False, failure_detail(exc=e)))
    detail = (r.stdout or "").strip()
    if r.stderr:
        detail = f"{detail}\n{r.stderr}".strip() if detail else r.stderr.strip()
    if r.returncode == 0:
        res = (True, detail)
        with _PROBE_LOCK:
            if apply_generation == _probe_generation:
                _PROBE_CACHE[exe] = res
                _PROBE_FAILURE_CACHE.pop(exe, None)
        return res
    if not detail:
        detail = f"exit code {r.returncode}"
    log.warning("probe_webview_import: %s returned %s: %s", exe, r.returncode, detail)
    return _remember_failure((False, detail))


def _spawn_probe_worker(exe: str) -> None:
    """Schedule the background probe worker outside the lock, rolling back on failure."""
    global _probe_running, _probe_wanted
    try:
        run_in_background(_webview_probe_worker, name="webview-probe", dedicated=False)
    except Exception:
        with _PROBE_LOCK:
            _probe_running = False
            if _probe_wanted == exe:
                _probe_wanted = None
        raise


def _ensure_webview_probe(exe: str) -> None:
    """Arm a background probe for *exe* if no cached result exists and no probe is running."""
    global _probe_wanted, _probe_running
    with _PROBE_LOCK:
        if _cached_probe_locked(exe) is not None:
            return
        if _probe_running or _probe_wanted is not None:
            return
        _probe_wanted = exe
        _probe_running = True

    _spawn_probe_worker(exe)


def _invalidate_webview_probe(exe: str | None) -> None:
    """Clear probe caches, invalidate in-flight probe generation, and queue *exe* if not None."""
    global _probe_generation, _probe_wanted, _probe_running
    start_worker = False
    with _PROBE_LOCK:
        _PROBE_CACHE.clear()
        _PROBE_FAILURE_CACHE.clear()
        _probe_generation += 1
        _probe_wanted = exe
        if exe is not None and not _probe_running:
            _probe_running = True
            start_worker = True

    if start_worker and exe is not None:
        _spawn_probe_worker(exe)


def _webview_probe_worker() -> None:
    """Worker loop that drains requested webview probes outside the lock."""
    global _probe_wanted, _probe_running
    while True:
        with _PROBE_LOCK:
            exe = _probe_wanted
            _probe_wanted = None
            gen = _probe_generation
            if exe is None:
                _probe_running = False
                return
        try:
            probe_webview_import(exe, apply_generation=gen)
        except Exception:
            log.exception("Unexpected error in webview-probe worker for %s", exe)


def spawn_editor_process(exe: str) -> subprocess.Popen[bytes]:
    """Start editor_main.py through ManagedSubprocess.start and return that Popen; the host stderr thread is the drain; terminate owns shutdown."""
    # base_env is the full child environment from build_editor_child_env.
    # env= would overlay that onto os.environ and put scrubbed secrets back.
    # scrub_env=True is not used: build_editor_child_env already scrubbed the environment while keeping LD_LIBRARY_PATH.
    # drain_stderr stays false: _stderr_drain_loop is already the stderr reader.
    # register stays false: shutdown_all must not kill_tree this child.
    # log_start_failure stays false: launch_monaco_editor catches the original OSError.
    env = build_editor_child_env()
    managed = ManagedSubprocess(
        [exe, _EDITOR_MAIN],
        base_env=env,
        command_wrapper=wrap_command_for_sandbox,
        drain_stderr=False,
        register=False,
        log_start_failure=False,
        label="monaco-editor",
    )
    managed.start()
    return cast("subprocess.Popen[bytes]", managed.popen)


# --- Pipe bridge ---

# Set on a dirty session after the user picks Don't save. _register_load_session
# refuses to overwrite a live dirty buffer unless this is present.
_REPLACE_CONFIRMED = "replace_confirmed"


_EditorDeliver = Callable[[dict[str, Any]], None]
_EditorStart = Callable[[_EditorDeliver], None]


def error_frame(message: str, exc: BaseException | None = None) -> dict[str, Any]:
    """Build an editor ``error`` frame, with a traceback when *exc* is given."""
    frame: dict[str, Any] = {"type": "error", "message": message}
    if exc is not None:
        frame["traceback"] = exception_traceback(exc)
    return frame


class DeferredEditorResult:
    """A save whose Monaco frame is delivered later, on the main thread.

    ``start(deliver)`` runs the venv wait off the UI-thread save path so the dialog stays responsive. ``deliver`` is called once with the same dict a synchronous ``on_save`` would have returned.
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

    def _clear_pending(self) -> None:
        self.pending_load = None
        self.pending_on_save = None
        self.pending_on_closed = None

    @property
    def target_key(self) -> tuple[str, ...]:
        return target_identity_key(self.mode, self.target)


def _poll_readable(stream: Any, timeout: float) -> bool:
    """POSIX select. Windows pipes are not selectable, so the caller blocks in read."""
    if sys.platform == "win32":
        return True
    ready, _unused_w, _unused_x = select.select([stream], [], [], timeout)
    return bool(ready)


class PersistentEditor:
    """Manages a single Monaco editor subprocess and keeps it alive in the background."""

    _stdin_lock: threading.Lock
    _stderr_tail_lock: threading.Lock
    _stderr_tail: deque[str]
    _stderr_tail_chars: int
    _stderr_generation: int
    _stderr_tail_max_chars: int
    _ready_event: threading.Event
    _save_lock: threading.Lock

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._stdin_lock = threading.Lock()
        self._reader_thread: BackgroundHandle | None = None
        self._stderr_thread: BackgroundHandle | None = None
        self._stderr_tail_lock = threading.Lock()
        self._stderr_tail = deque()
        self._stderr_tail_chars = 0
        self._stderr_generation = 0
        self._stderr_tail_max_chars = 65536
        self._ready_event = threading.Event()
        self._save_lock = threading.Lock()
        self._save_token: object | None = None

        self.sessions: dict[str, EditorSessionState] = {}
        self.focused_id: str | None = None
        self.executor: MainThread = main
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
        if not self.sessions:
            self.run_script_doc = None
            self.run_script_doc_url = None
        if call_closed and state.on_closed is not None:
            try:
                state.on_closed()
            except Exception:
                log.exception("Editor on_closed failed while ending session %s", session_id)

    def end_all_sessions(self) -> None:
        """End every registered session and run each on_closed.

        Used when the child is gone (terminate, or a launch that finds it dead)
        so no buffer of a dead process stays registered."""
        for state in list(self.sessions.values()):
            self.end_session(state.session_id, call_closed=True)

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
        proc = self._proc
        if proc is None:
            return False
        return proc.poll() is None

    @property
    def proc(self) -> subprocess.Popen[bytes] | None:
        return self._proc

    def start(self, proc: subprocess.Popen[bytes]) -> None:
        """Start the reader thread for the spawned process."""
        with self._stdin_lock:
            self._proc = proc
        self._ready_event.clear()
        with self._stderr_tail_lock:
            self._stderr_generation += 1
            generation = self._stderr_generation
            self._stderr_tail.clear()
            self._stderr_tail_chars = 0
        self._reader_thread = run_in_background(self._read_loop, name="editor-pipe-reader", daemon=True, dedicated=True)
        if proc.stderr is not None:
            def _drain() -> None:
                self._stderr_drain_loop(proc, generation)

            self._stderr_thread = run_in_background(_drain, name="editor-stderr-drain", daemon=True, dedicated=True)

    def terminate(self) -> None:
        """Force terminate the subprocess and its WebEngine children.

        Spawn uses ``start_new_session`` (POSIX ``setsid`` in the child).
        ``proc.terminate()`` leaves Qt grandchildren alive, and a surviving
        parent is then reused as if it were healthy.
        ManagedSubprocess.close() (SIGTERM, 3s, reaper) and kill_tree() (no wait, no pipe close) are a different shutdown.
        ``kill_process_tree`` runs even when ``poll()`` is already set. ``wait`` / ``proc.kill`` run only if the leader was alive before the tree kill.
        """
        # terminate() clears self._proc under _stdin_lock before closing pipes so concurrent send/reader paths capture a stable proc reference or see None safely. Without that ordering, one thread can call poll() on None or write to a closed stdin.
        with self._stdin_lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            # kill_process_tree runs even when the leader already exited so Qt/WebEngine children from start_new_session do not survive a crash. Liveness is sampled first so we still wait/kill a leader this call terminated.
            leader_alive = proc.poll() is None
            kill_process_tree(proc)
            if leader_alive:
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except OSError:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                log.debug("editor_host: pipe close failed", exc_info=True)

    def send(self, message: dict[str, Any], *, session: EditorSessionState | None = None) -> None:
        """Thread-safe write to child stdin. Session messages get ``session_id`` + ``target``."""
        kind = message_type(message)
        if kind and kind not in PARENT_TO_CHILD_TYPES:
            log.warning("editor_host: type %s is not queued by the editor child", kind)
        state = session or (self.lookup(session_id_of(message)) if session_id_of(message) else self.focused())
        if kind and kind != "ready" and state is not None:
            message = stamp_session(
                message,
                session_id=state.session_id,
                mode=state.mode or str(message.get("mode") or ""),
                target=state.target,
            )
        timed_out = False
        with self._stdin_lock:
            proc = self._proc
            if proc is None:
                raise RuntimeError("No editor process is running")
            exit_code = proc.poll()
            if exit_code is not None:
                detail = self.read_stderr_tail()
                raise RuntimeError(f"Editor process already exited (code={exit_code}). {detail}")
            if proc.stdin is None:
                raise RuntimeError("Editor process stdin is closed")
            frame = pack_message(message)
            try:
                write_packed_frame_with_timeout(
                    proc.stdin,
                    frame,
                    VENV_IPC_WRITE_TIMEOUT_SEC,
                    is_alive=lambda: proc.poll() is None,
                )
            except BrokenPipeError as e:
                detail = self.read_stderr_tail()
                raise RuntimeError(f"Editor process closed stdin. {detail}") from e
            except subprocess.TimeoutExpired:
                timed_out = True

        if timed_out:
            log.warning("editor_host: stdin write timed out after %ss; editor unresponsive", VENV_IPC_WRITE_TIMEOUT_SEC)
            # write_packed_frame_with_timeout caps stdin writes so a child that stops reading cannot block the UI thread indefinitely; a timed-out write tears down the editor because partial frames must not be reused.
            terminate_persistent_editor()
            detail = self.read_stderr_tail()
            raise RuntimeError(f"Editor unresponsive. {detail}")

    def read_stderr_tail(self) -> str:
        """Return stderr already captured by the drain thread."""
        with self._stderr_tail_lock:
            text = "\n".join(self._stderr_tail)
        return text.strip()

    def _append_stderr_line(self, line: str, generation: int) -> None:
        if not line:
            return
        with self._stderr_tail_lock:
            # A drain started by an older start() still runs after the tail is cleared;
            # its generation is stale, so its lines must not enter the new tail.
            if generation != self._stderr_generation:
                return
            self._stderr_tail.append(line)
            # +1 matches the old sum(len(s) + 1) budget (one join newline per line).
            # Recomputing that sum on every popleft is O(n²) while the drain
            # thread holds the lock. Keep a running total, updated by the
            # appended line and by each dropped line.
            self._stderr_tail_chars += len(line) + 1
            while self._stderr_tail and self._stderr_tail_chars > self._stderr_tail_max_chars:
                dropped = self._stderr_tail.popleft()
                self._stderr_tail_chars -= len(dropped) + 1

    def _read_and_append_stderr_line(self, stderr: IO[bytes], generation: int) -> bool:
        raw = stderr.readline()
        if not raw:
            return False
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line:
            log.debug("editor child: %s", line)
            self._append_stderr_line(line, generation=generation)
        return True

    @background
    def _stderr_drain_loop(self, proc: subprocess.Popen[bytes], generation: int) -> None:
        if proc.stderr is None:
            return
        stderr = proc.stderr
        try:
            while proc.poll() is None:
                if not _poll_readable(stderr, 0.5):
                    continue
                if not self._read_and_append_stderr_line(stderr, generation):
                    break
        except Exception:
            log.debug("editor stderr drain failed", exc_info=True)
        finally:
            try:
                if _poll_readable(stderr, 0.05):
                    remainder = stderr.read()
                    if remainder:
                        for piece in remainder.decode("utf-8", errors="replace").splitlines():
                            if piece:
                                log.debug("editor child: %s", piece)
                                self._append_stderr_line(piece, generation=generation)
            except Exception:
                log.debug("editor stderr drain tail read failed", exc_info=True)

    def wait_for_ready(self, ctx: Any, timeout_sec: float = 30.0) -> bool:
        """Wait for ``ready`` while pumping LibreOffice UI events."""
        from plugin.framework.uno_context import wait_while_pumping

        deadline = time.monotonic() + timeout_sec
        proc = self._proc
        while True:
            if self._ready_event.is_set():
                return True
            proc = self._proc
            if proc is None:
                return False
            exit_code = proc.poll()
            if exit_code is not None:
                log.error("Editor child exited before ready (code=%s). stderr=%s", exit_code, self.read_stderr_tail())
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait_while_pumping(self._ready_event, ctx, timeout=min(0.25, remaining))
        if not self._ready_event.is_set():
            log.error("Editor ready timeout (%ss). child_running=%s stderr=%s", timeout_sec, proc is not None, self.read_stderr_tail())
        return self._ready_event.is_set()

    @background
    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stdout = proc.stdout
        try:
            self._read_incoming(proc, stdout)
        except Exception:
            log.exception("Editor pipe reader failed")
        finally:
            log.info("editor_host: persistent reader loop finished.")
            still_ours = self._proc is proc
            if still_ours:
                self._handle_disconnect()
            else:
                log.info("editor_host: old reader loop ignored disconnect (superseded by new process)")
            # If this loop still owns a live process after EOF or disconnect, terminate() clears it so is_running does not keep a leader with no reader. Superseded or already-exited children are left alone.
            if still_ours and proc.poll() is None:
                self.terminate()

    def _read_editor_message(self, proc: subprocess.Popen[bytes], stdout: Any) -> dict[str, Any] | None:
        """Read one editor frame with a deadline so a partial length prefix cannot hang."""
        msg = read_pickle_frame(
            stdout,
            30.0,
            max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
            frame_label="editor message",
            require_dict=True,
            is_alive=lambda: proc.poll() is None,
        )
        if msg is None:
            return None
        return cast("dict[str, Any]", msg)

    def _read_incoming(self, proc: subprocess.Popen[bytes], stdout: Any) -> None:
        while proc.poll() is None:
            if sys.platform == "win32":
                try:
                    msg = self._read_editor_message(proc, stdout)
                except subprocess.TimeoutExpired:
                    # Idle pipe: no bytes consumed, keep waiting. A stalled partial
                    # frame has already been pulled into the helper's buffer, so the
                    # next read raises and the reader exits.
                    continue
            else:
                if not _poll_readable(stdout, 0.5):
                    continue
                msg = self._read_editor_message(proc, stdout)
            if msg is None:
                break
            if self._proc is proc:
                self._dispatch_incoming(msg)
            else:
                log.warning("editor_host: ignored incoming message from old process")

    def _marshal_timeout(self) -> float:
        """UI wait for a save/close. Matches configured_python_exec_timeout plus slack so long script budgets are not cut off at a fixed 60s."""
        from plugin.scripting.config_limits import configured_python_exec_timeout

        return float(configured_python_exec_timeout()) + 5.0

    def _run_on_main(self, fn: Callable[[], None], label: str) -> Exception | None:
        """Queue *fn* on the UI thread. Return the marshal failure, else None."""
        try:
            self.executor.call(fn, timeout=self._marshal_timeout(), bound_scope=None)
        except Exception as exc:
            log.exception("%s timed out" if isinstance(exc, TimeoutError) else "%s marshal failed", label)
            return exc
        return None

    def _report_marshal_failure(self, send: Callable[[dict[str, Any]], None], timed_out: bool, messages: tuple[str, str], who: str) -> None:
        timed_out_message, failed_message = messages
        message = timed_out_message if timed_out else failed_message
        # A bare send here lets a broken pipe escape _dispatch_incoming and terminate() the editor. Log and continue.
        try:
            send(error_frame(message))
        except Exception:
            log.warning("Editor %s could not send timeout error frame" if timed_out else "Editor %s could not send marshal-failure error frame", who, exc_info=True)

    def set_run_script_document(self, doc: Any | None) -> None:
        from plugin.scripting.document_scripts import document_scripts_identity

        self.run_script_doc = doc
        self.run_script_doc_url = document_scripts_identity(doc) if doc is not None else None

    def _on_ask_unsaved(self, msg: dict[str, Any]) -> None:
        """Answer the picker's unsaved-edit question with the native Yes/No/Cancel box."""
        target_session = self.resolve_incoming(msg)
        name = str(msg.get("name") or "")

        def _ask() -> None:
            choice = confirm_unsaved_monaco_edit(self.ctx, name)
            try:
                self.send({"type": "unsaved_choice", "choice": choice}, session=target_session)
            except Exception:
                log.exception("Editor could not send unsaved_choice")

        # post, not execute: the modal box waits on the user, not a timeout.
        self.executor.post(_ask, bound_scope=None)

    def _on_picker(self, msg: dict[str, Any]) -> None:
        kind = message_type(msg)
        target_session = self.resolve_incoming(msg)
        if session_id_of(msg) and target_session is None:
            return

        def _picker_send(reply: dict[str, Any]) -> None:
            self.send(reply, session=target_session)

        def _handle_picker() -> None:
            try:
                handle_editor_script_message(
                    kind,
                    msg,
                    # Launch document, not whichever window is focused now.
                    session_doc=self.run_script_doc,
                    session_doc_url=self.run_script_doc_url,
                    send=_picker_send,
                )
            except Exception as e:
                log.exception("Editor script picker failed")
                try:
                    _picker_send(error_frame(str(e), e))
                except Exception:
                    log.exception("Editor script picker could not send the error frame")

        exc = self._run_on_main(_handle_picker, "Editor script picker")
        if exc is not None:
            self._report_marshal_failure(_picker_send, isinstance(exc, TimeoutError), (_("The script list update timed out."), _("The script list update failed.")), "script picker")

    def _on_dirty(self, msg: dict[str, Any]) -> None:
        state = self.resolve_incoming(msg)
        if state is not None:
            state.dirty = bool(msg.get("dirty"))

    def _save_superseded(self, token: object) -> bool:
        """True when a newer save replaced *token* or its handler timed out. Call with _save_lock held."""
        return self._save_token is not token

    def _on_save(self, msg: dict[str, Any]) -> None:
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
        with self._save_lock:
            self._save_token = token

        def _send_save(payload: dict[str, Any]) -> None:
            with self._save_lock:
                # A deferred Run delivers on a later UI turn. A newer save
                # replaces this token, so a late result must not clear dirty.
                if self._save_superseded(token):
                    return
                if payload.get("type") == "saved" or payload.get("ok"):
                    captured.dirty = False
            # self.send() runs outside _save_lock so a write timeout can terminate the child without deadlocking the pipe reader, which also needs _save_lock in _on_save.
            self.send(payload, session=captured)

        def _handle_save() -> None:
            with self._save_lock:
                if self._save_superseded(token):
                    # executor.execute(..., timeout) raising TimeoutError does not
                    # cancel the queued _handle_save. Check the save token before
                    # on_save, or the save runs after the user was told it timed out.
                    log.warning("Editor save skipped: timed out or superseded")
                    return
            try:
                on_save = captured.on_save
                if on_save is not None:
                    result = on_save(code, save_as_plain, data_binding, action)
                else:
                    result = {"type": "saved", "ok": True}
                if isinstance(result, DeferredEditorResult):
                    def deliver(payload: dict[str, Any]) -> None:
                        if not isinstance(payload, dict):
                            payload = error_frame(_("Script execution failed."))
                        try:
                            _send_save(payload)
                        except Exception:
                            log.exception("Editor run result could not be sent")

                    try:
                        result.start(deliver)
                    except Exception as e:
                        log.exception("Editor run failed to start")
                        _send_save(error_frame(str(e), e))
                    # This path is Monaco Run (action="run"). pending_load is applied
                    # only by the synchronous success path below, which is what request_save
                    # hits (#btn-save → action="save"). Applying the queued load from
                    # deliver would switch buffers when a run finishes.
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
                    captured._clear_pending()
                    self.end_session(captured.session_id, call_closed=True)
                    if next_on_save is not None:
                        _activate_load(pending, next_on_save, next_on_closed or (lambda: None))
            except Exception as e:
                log.exception("Editor save handler failed")
                try:
                    _send_save(error_frame(str(e), e))
                except Exception:
                    log.exception("Editor save handler could not send the error frame")

        exc = self._run_on_main(_handle_save, "Editor save handler")
        if exc is not None:
            with self._save_lock:
                self._save_token = None
            captured._clear_pending()
            self._report_marshal_failure(lambda payload: self.send(payload, session=captured), isinstance(exc, TimeoutError), (_("Saving the script timed out."), _("Saving the script failed.")), "save handler")

    def _on_close(self, msg: dict[str, Any]) -> None:
        log.info("editor_host: received closed")
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
                if captured_id:
                    self.end_session(captured_id, call_closed=False)

        self._run_on_main(_handle_close, "Editor close handler")

    def _on_ready(self, msg: dict[str, Any]) -> None:
        # The child's GUI loop is up; wait_for_ready may now send the first load.
        self._ready_event.set()

    def _incoming_handlers(self) -> dict[str, _EditorDeliver]:
        """Handler for each child-to-parent type (editor_ipc.CHILD_TO_PARENT_TYPES).

        Built per message from bound methods, so a test that patches one method
        on an instance is the method that runs.
        """
        return {
            "ready": self._on_ready,
            "closed": self._on_close,
            "dirty": self._on_dirty,
            "save": self._on_save,
            "ask_unsaved": self._on_ask_unsaved,
            **dict.fromkeys(SCRIPT_PICKER_MESSAGE_TYPES, self._on_picker),
        }

    def _dispatch_incoming(self, msg: dict[str, Any]) -> None:
        try:
            kind = message_type(msg)
            handler = self._incoming_handlers().get(kind)
            if handler is None:
                log.debug("Editor child message: %s", kind)
                return
            handler(msg)
        except Exception:
            log.exception("Editor incoming message dispatch failed type=%s", message_type(msg))

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
                for state in snapshot:
                    self.end_session(state.session_id, call_closed=False)

        self._run_on_main(_handle_close, "Editor disconnect handler")


_PERSISTENT_EDITOR = PersistentEditor()


def terminate_persistent_editor() -> None:
    """End every session (on_closed runs) and force terminate the editor process."""
    _PERSISTENT_EDITOR.end_all_sessions()
    _PERSISTENT_EDITOR.terminate()


def _on_config_changed(**kwargs: Any) -> None:
    key = kwargs.get("key", "")
    changed = kwargs.get("keys") or ()
    # Settings OK emits one event with key="" and the changed names in keys.
    # Matching only key dropped a venv-path change that was saved with other fields.
    venv_changed = key == "scripting.python_venv_path" or "scripting.python_venv_path" in changed
    force_changed = (
        key == "scripting.force_internal_script_editor"
        or "scripting.force_internal_script_editor" in changed
    )
    editor_changed = key == "scripting.python_editor" or "scripting.python_editor" in changed
    if not venv_changed and not force_changed and not editor_changed:
        return

    if venv_changed or editor_changed:
        # The probe checks different packages per editor (rocher only for Monaco).
        log.info("editor_host: venv path or python_editor changed, terminating background editor process")
        exe = resolve_probeable_editor_python()
        try:
            _invalidate_webview_probe(exe)
        except Exception:
            log.exception("editor_host: failed to schedule webview probe on config change")

    if force_changed:
        log.info("editor_host: scripting.force_internal_script_editor changed, terminating background editor process")

    terminate_persistent_editor()


get_event_bus().subscribe("config:changed", _on_config_changed)


def resolve_probeable_editor_python() -> str | None:
    """Return configured venv Python executable if Monaco is enabled and configured, else None."""
    from plugin.framework.config import get_config

    if get_config("scripting.force_internal_script_editor"):
        log.debug("monaco editor bypassed by scripting.force_internal_script_editor")
        return None
    exe, err = resolve_editor_python()
    if not exe:
        log.debug("monaco editor venv python unavailable (%s)", err)
        return None
    return exe


def arm_webview_probe_at_startup() -> None:
    """Arm the webview probe on the background pool at startup if configured."""
    exe = resolve_probeable_editor_python()
    if exe is not None:
        _ensure_webview_probe(exe)


# --- Session launch ---


def monaco_editor_available() -> tuple[str | None, bool]:
    """Return (venv python exe, True) when Monaco can launch, else (exe or None, False)."""
    exe = resolve_probeable_editor_python()
    if not exe:
        return None, False
    if _PERSISTENT_EDITOR.is_running:
        log.debug("monaco_editor_available: editor process already running, skipping probe")
        return exe, True
    cached = cached_webview_probe(exe)
    if cached is None:
        _ensure_webview_probe(exe)
        return exe, False
    if not cached[0]:
        log.debug("monaco_editor_available: webview probe failed for %s: %s", exe, cached[1][:200] if cached[1] else "")
    return exe, cached[0]


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


def _dirty_blocks_replace(state: EditorSessionState) -> bool:
    """True when *state* is a live unsaved buffer the user has not discarded."""
    if state.dirty is not True:
        return False
    if state.extra.get(_REPLACE_CONFIRMED) is True:
        return False
    return _PERSISTENT_EDITOR.is_running


class DirtyBufferError(RuntimeError):
    """Refused to replace a live Monaco buffer that still has unsaved edits."""


def ask_save_discard_cancel(ctx: Any, *, title: str, message: str, log_label: str) -> str:
    """Ask Save / Don't save / Cancel. Returns ``save``, ``discard``, or ``cancel``."""
    from plugin.framework.uno_context import get_desktop

    try:
        desktop = get_desktop(ctx)
        frame = desktop.getCurrentFrame() if desktop is not None else None
        window = frame.getContainerWindow() if frame is not None else None
        if window is None:
            return "cancel"
        smgr = ctx.getServiceManager()
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        box = toolkit.createMessageBox(window, _BOX_QUERYBOX, _BOX_BUTTONS_YES_NO_CANCEL, title, message)
        result = int(box.execute())
        if result == _BOX_RESULT_YES:
            return "save"
        if result == _BOX_RESULT_NO:
            return "discard"
        # Cancel (MessageBoxResults CANCEL = 0), closing the box, or any other result keeps the buffer as it is.
        return "cancel"
    except Exception:
        log.exception("%s failed", log_label)
        return "cancel"


@main_thread_only
def confirm_unsaved_monaco_edit(ctx: Any, label: str) -> str:
    """Ask Save / Don't save / Cancel. Returns ``save``, ``discard``, or ``cancel``."""
    from plugin.framework.uno_context import product_display_name

    title = product_display_name(ctx)
    where = label.strip() if label and label.strip() else _("this editor")
    message = _(
        "Save changes to {0}?\n\nYes saves them before switching. No discards them. Cancel keeps the current editor."
    ).format(where)
    return ask_save_discard_cancel(ctx, title=title, message=message, log_label="confirm_unsaved_monaco_edit")


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
    "queued" means request_save was sent; if the focused session is already gone,
    the result is "cancel" so the caller does not load.
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
        if not queue_save_then_load(load_message, on_save, on_closed):
            return "cancel"
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
        # Dirty buffers must not be replaced silently when switching mode or target; launch_monaco_editor must confirm or save first. Without this check, Run Script, init, and LaTeX edits could be overwritten without on_save or on_closed.
        raise DirtyBufferError(
            f"Refusing to replace dirty Monaco session {blocked.session_id} ({blocked.mode})"
        )

    # Opening a cell after Run Script must drop the old document UNO reference.
    # Leaving it on _PERSISTENT_EDITOR.run_script_doc held the document until
    # disconnect or terminate. Reset it when mode is not run_script.
    if mode == "run_script":
        if "run_script_doc" in ipc_message:
            # Queued save-then-load still carries the document. The direct launch
            # path pops it before this call and has already stored it.
            _PERSISTENT_EDITOR.set_run_script_document(ipc_message.get("run_script_doc"))
    else:
        _PERSISTENT_EDITOR.set_run_script_document(None)
    ipc_message.pop("run_script_doc", None)
    ipc_message.pop("doc", None)

    if existing is not None:
        existing.on_save = on_save
        existing.on_closed = on_closed
        existing.target = target
        existing.mode = mode
        existing.dirty = False
        existing.extra.pop(_REPLACE_CONFIRMED, None)
        # Reuse means this target is current, so a queued switch is no longer
        # the next buffer. Resetting dirty alone left pending_load /
        # pending_on_save / pending_on_closed, and a queue_save_then_load still
        # in flight then applied that stale load on a later unrelated save.
        existing._clear_pending()
        state = existing
    else:
        if focused is not None:
            # One visible buffer: switching targets ends the previous session.
            _PERSISTENT_EDITOR.end_session(focused.session_id, call_closed=True)
        state = EditorSessionState(
            session_id=uuid.uuid4().hex,
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
) -> bool:
    """Ask Monaco to save the current buffer, then load *load_message*."""
    from plugin.scripting.editor_ui_strings import enrich_monaco_load_message

    focused = _PERSISTENT_EDITOR.focused()
    if focused is None:
        return False
    # Only enrich on this path. launch_monaco_editor has not attached ui yet.
    focused.pending_load = enrich_monaco_load_message(dict(load_message))
    focused.pending_on_save = on_save
    focused.pending_on_closed = on_closed
    try:
        _PERSISTENT_EDITOR.send({"type": "request_save"}, session=focused)
        return True
    except Exception:
        # The queued load must not fire on a later unrelated save when the
        # request never reached the child.
        focused._clear_pending()
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

    def _launch_fail(msg_text: str, report_title: str) -> bool:
        msgbox_with_report(ctx, title, msg_text, reportable=True, report_title=report_title, report_extra=msg_text)
        return False

    _PERSISTENT_EDITOR.ctx = ctx
    # ui is attached later, once. Enriching before the unsaved-changes gate
    # and again in queue_save_then_load rebuilt the same catalog.
    ipc_message = dict(load_message)
    closed_handler = on_closed if on_closed is not None else (lambda: None)

    # Inject current LO theme so the child Monaco + toolbar chrome automatically
    # match the LibreOffice light/dark (and basic surface color) without any
    # user setting or toggle in the editor. This is computed from the active
    # window's StyleSettings (same heuristic used by the chat sidebar).
    # We always recompute on send so that switching cells or reopening picks
    # up a theme change. See plugin/framework/appearance.py.
    if "theme" not in ipc_message:
        from plugin.framework.appearance import get_monaco_theme_info

        # Prefer any doc we had for the run_script case or if caller left it in msg.
        # Leave run_script_doc on the message until after the unsaved-changes
        # question. Cancel must not retarget the script picker document.
        doc_for_theme = ipc_message.get("doc") or ipc_message.get("run_script_doc")
        ipc_message["theme"] = get_monaco_theme_info(doc=doc_for_theme, ctx=ctx)
        log.debug("editor_host: chosen theme: %s", ipc_message["theme"])

    # One gate covers cell, Run Script, init script, and LaTeX. Replacing the
    # focused buffer before asking dropped the other buffer: the cell editor
    # only asked for mode calc_cell, and Run Python Script never asked.
    # Cancel and queued save return before the dead-session purge and the load. Discard is
    # marked on the session so _register_load_session may replace it.
    decision = prepare_monaco_buffer_replace(ctx, ipc_message, on_save, closed_handler)
    if decision != "proceed":
        return True

    if not _PERSISTENT_EDITOR.is_running:
        # The reader thread reports a child exit through _handle_disconnect, but
        # this launch can run on the UI thread first. Ending the dead child's
        # buffers here keeps the same target from reusing their session id, which
        # the late disconnect handler would then end under the new child.
        _PERSISTENT_EDITOR.end_all_sessions()

    if ipc_message.get("mode") == "run_script":
        run_script_doc = ipc_message.pop("run_script_doc", None)
        _PERSISTENT_EDITOR.set_run_script_document(run_script_doc)
    else:
        _PERSISTENT_EDITOR.set_run_script_document(None)

    if _PERSISTENT_EDITOR.is_running:
        log.info("editor_host: reusing running editor background process")
    else:
        log.info("editor_host: spawning new editor background process")
        try:
            proc = spawn_editor_process(exe)
        except OSError as e:
            log.exception("Failed to spawn editor")
            msg = failure_message(_("Could not start the Python editor."), exc=e)
            return _launch_fail(msg, "Python editor spawn failed")

        _PERSISTENT_EDITOR.start(proc)

        if not _PERSISTENT_EDITOR.wait_for_ready(ctx, timeout_sec=45.0):
            detail = _PERSISTENT_EDITOR.read_stderr_tail()
            # terminate_persistent_editor ends the sessions and kills the child;
            # a live child with no session would make the next open skip wait_for_ready.
            terminate_persistent_editor()
            msg = failure_message(_("The Python editor window did not start."), detail=detail)
            return _launch_fail(msg, "Python editor did not start")

        # Finite pre-warm, same as the native script dialog; the shared daemon pool
        # is for this; dedicated=True is for servers, pipe drains, and jobs another
        # thread joins.
        run_in_background(warm_venv_worker, name="warm-venv-worker", dedicated=False)

    if not _PERSISTENT_EDITOR.is_running:
        detail = _PERSISTENT_EDITOR.read_stderr_tail()
        msg = failure_message(_("The Python editor exited before it could load your code."), detail=detail)
        return _launch_fail(msg, "Python editor exited early")

    try:
        ipc_message = enrich_monaco_load_message(ipc_message)
        _unused_state, stamped = _register_load_session(ipc_message, on_save, closed_handler)
        _PERSISTENT_EDITOR.send(stamped)
    except DirtyBufferError:
        # The confirm gate should have queued, cancelled, or marked discard.
        # Killing the child here would throw away the buffer we just refused
        # to replace.
        log.error("editor_host: refused to replace a dirty editor buffer")
        return True
    except Exception as e:
        log.exception("Failed to send load to editor")
        ipc_detail = "\n\n".join(filter(None, [_PERSISTENT_EDITOR.read_stderr_tail(), exception_traceback(e)]))
        # Same as the ready-timeout path: a live child with no session makes
        # the next open skip the webview probe.
        terminate_persistent_editor()
        msg = failure_message(_("Could not talk to the Python editor."), detail=ipc_detail)
        return _launch_fail(msg, "Python editor IPC failed")

    return True
