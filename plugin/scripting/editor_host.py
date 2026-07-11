# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Monaco editor host: spawn child process, pipe bridge, and session launch."""

from __future__ import annotations

import logging
import os
import select
import subprocess
import sys
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Callable, cast

from plugin.framework.worker_pool import get_subprocess_creationflags

if TYPE_CHECKING:
    import subprocess as subprocess_types

from plugin.framework.thread_guard import background
from plugin.framework.event_bus import global_event_bus
from plugin.framework.i18n import _
from plugin.framework.queue_executor import QueueExecutor, default_executor
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.editor_ipc import (
    exception_traceback,
    failure_detail,
    failure_message,
    message_type,
    read_message,
    write_message,
)
from plugin.scripting.venv_worker import resolve_venv_python, warm_venv_worker, scrub_subprocess_env, wrap_command_for_sandbox

log = logging.getLogger(__name__)


def _script_code_from_message(msg: dict[str, Any]) -> str:
    raw = msg.get("code", "")
    return raw if isinstance(raw, str) else ""


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
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "LD_LIBRARY_PATH"):
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
            "Set the Python venv path in WriterAgent Settings → Python (same venv where you ran "
            "'uv pip install pywebview rocher'). LibreOffice's built-in Python cannot run the Monaco editor.",
        )
    exe = resolve_venv_python(venv_dir)
    if not exe:
        return (
            None,
            f"No python executable found under configured venv: {venv_dir!r} "
            "(expected bin/python, bin/python3, or bin/python3.x).",
        )
    return exe, ""


_PROBE_CACHE: dict[str, tuple[bool, str]] = {}


def probe_webview_import(exe: str) -> tuple[bool, str]:
    """Return whether *exe* can ``import webview`` (pywebview package), with diagnostics (cached)."""
    if exe in _PROBE_CACHE:
        return _PROBE_CACHE[exe]
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
        res = (False, failure_detail(exc=e))
        _PROBE_CACHE[exe] = res
        return res
    detail = (r.stdout or "").strip()
    if r.stderr:
        detail = f"{detail}\n{r.stderr}".strip() if detail else r.stderr.strip()
    if r.returncode == 0:
        res = (True, detail)
        _PROBE_CACHE[exe] = res
        return res
    if not detail:
        detail = f"exit code {r.returncode}"
    log.warning("probe_webview_import: %s returned %s: %s", exe, r.returncode, detail)
    res = (False, detail)
    _PROBE_CACHE[exe] = res
    return res


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
        popen_kw["preexec_fn"] = os.setsid
    return cast("subprocess.Popen[bytes]", subprocess.Popen(wrap_command_for_sandbox([exe, _EDITOR_MAIN]), **popen_kw))


# --- Pipe bridge ---

_SESSION_LOCK = threading.RLock()
_ACTIVE_SESSION: EditorSession | None = None


class PersistentEditor:
    """Manages a single Monaco editor subprocess and keeps it alive in the background."""

    def __init__(self) -> None:
        self._proc: subprocess_types.Popen[bytes] | None = None
        self._stdin_lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail_lock = threading.Lock()
        self._stderr_tail = deque[str]()
        self._stderr_tail_max_chars = 65536
        self._ready_event = threading.Event()
        self._closed_event = threading.Event()

        # Transient session callbacks for the active cell edit
        self.on_save: Callable[..., dict[str, Any]] | None = None
        self.on_closed: Callable[[], None] | None = None
        self.executor: QueueExecutor = default_executor
        self.ctx: Any = None
        self.run_script_doc: Any = None
        self.run_script_doc_url: str | None = None

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
        self._reader_thread = run_in_background(self._read_loop, name="editor-pipe-reader", daemon=True)
        if proc.stderr is not None:
            self._stderr_thread = run_in_background(self._stderr_drain_loop, name="editor-stderr-drain", daemon=True)

    def terminate(self) -> None:
        """Force terminate the subprocess."""
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except OSError:
                pass
            # Close pipes
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass

    def send(self, message: dict[str, Any]) -> None:
        """Thread-safe write to child stdin."""
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
        """Best-effort read of child stderr (for startup failure messages)."""
        with self._stderr_tail_lock:
            if self._stderr_tail:
                text = "\n".join(self._stderr_tail)
                if len(text) > max_bytes:
                    return text[-max_bytes:].strip()
                return text.strip()
        if self._proc is None or sys.platform == "win32":
            return ""
        stderr = self._proc.stderr
        if stderr is None:
            return ""
        chunks: list[bytes] = []
        try:
            while len(b"".join(chunks)) < max_bytes:
                ready, _, _ = select.select([stderr], [], [], 0)
                if not ready:
                    break
                piece = stderr.read(512)
                if not piece:
                    break
                chunks.append(piece)
        except Exception:
            log.debug("read_stderr_tail failed", exc_info=True)
        if not chunks:
            return ""
        return b"".join(chunks).decode("utf-8", errors="replace").strip()

    def _append_stderr_line(self, line: str) -> None:
        if not line:
            return
        with self._stderr_tail_lock:
            self._stderr_tail.append(line)
            while self._stderr_tail and sum(len(s) + 1 for s in self._stderr_tail) > self._stderr_tail_max_chars:
                self._stderr_tail.popleft()

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
                    ready, _, _ = select.select([stderr], [], [], 0.5)
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
        from plugin.framework.uno_context import get_toolkit

        toolkit = get_toolkit(ctx)
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
            if toolkit is not None:
                try:
                    toolkit.processEventsToIdle()
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
            if self._proc is proc:
                self._handle_disconnect()
            else:
                log.info("editor_host: old reader loop ignored disconnect (superseded by new process)")

    def _read_loop_select(self, proc: subprocess_types.Popen[bytes], stdout: Any) -> None:
        """POSIX: use select() to poll the pipe with periodic liveness checks."""
        while proc.poll() is None:
            ready, _, _ = select.select([stdout], [], [], 0.5)
            if not ready:
                continue
            msg = read_message(stdout)
            if msg is None:
                break
            if self._proc is proc:
                self._dispatch_incoming(msg)
            else:
                log.warning("editor_host: ignored incoming message from old process")

    def _read_loop_blocking(self, proc: subprocess_types.Popen[bytes], stdout: Any) -> None:
        """Windows: blocking read (pipe close on process exit unblocks read)."""
        while True:
            msg = read_message(stdout)
            if msg is None:
                break
            if self._proc is proc:
                self._dispatch_incoming(msg)
            else:
                log.warning("editor_host: ignored incoming message from old process")

    def set_run_script_document(self, doc: Any | None) -> None:
        from plugin.scripting.document_scripts import document_scripts_identity

        self.run_script_doc = doc
        self.run_script_doc_url = document_scripts_identity(doc) if doc is not None else None

    def _resolve_run_script_doc(self) -> Any | None:
        from plugin.scripting.document_scripts import get_active_document_for_scripts

        if self.ctx is not None:
            active = get_active_document_for_scripts(self.ctx)
            if active is not None:
                return active
        return self.run_script_doc

    def _send_scripts_list(
        self,
        *,
        status_ok_text: str | None = None,
        status_error_text: str | None = None,
    ) -> None:
        from plugin.scripting.document_scripts import build_scripts_list_message

        doc = self._resolve_run_script_doc()
        self.send(
            build_scripts_list_message(
                self.ctx,
                session_doc=doc,
                session_doc_url=self.run_script_doc_url,
                status_ok_text=status_ok_text,
                status_error_text=status_error_text,
            )
        )

    def _save_user_script(self, name: str, code: str) -> None:
        from plugin.framework.config import get_config, set_config

        scripts = get_config("saved_python_scripts")
        if not isinstance(scripts, dict):
            scripts = {}
        scripts[name] = code
        set_config("saved_python_scripts", scripts)

    def _dispatch_incoming(self, msg: dict[str, Any]) -> None:
        kind = message_type(msg)
        if kind == "request_scripts":
            def _handle_request() -> None:
                log.info("editor_host: request_scripts")
                self._send_scripts_list()
            self.executor.execute(_handle_request)
            return

        if kind == "select_script":
            name = str(msg.get("name", "") or "").strip()

            def _handle_select() -> None:
                from plugin.framework.config import set_config
                from plugin.scripting.python_runner import resolve_run_script_name_config_key

                doc = self._resolve_run_script_doc()
                name_config_key = resolve_run_script_name_config_key(doc)
                set_config(name_config_key, name)

            self.executor.execute(_handle_select)
            return

        if kind == "save_script":
            name = str(msg.get("name", "") or "").strip()
            script_code = _script_code_from_message(msg)

            def _handle_save_named() -> None:
                from plugin.scripting.document_scripts import SCRIPT_ORIGIN_DOCUMENT

                origin = str(msg.get("origin", "") or "").strip()
                if not name:
                    self._send_scripts_list(status_error_text=_("Script name cannot be empty."))
                    return
                if origin == SCRIPT_ORIGIN_DOCUMENT:
                    doc = self._resolve_run_script_doc()
                    if doc is None:
                        self._send_scripts_list(status_error_text=_("No document is open to save scripts."))
                        return
                    from plugin.scripting.document_scripts import save_document_script

                    err = save_document_script(doc, name, script_code)
                    if err:
                        self._save_user_script(name, script_code)
                        self._send_scripts_list(
                            status_ok_text=_("Saved script '{0}' to My Scripts.").format(name),
                            status_error_text=err,
                        )
                        return
                    self._send_scripts_list(status_ok_text=_("Saved script '{0}' to this document.").format(name))
                    return
                self._save_user_script(name, script_code)
                log.info("editor_host: save_script '%s' (user)", name)
                self._send_scripts_list(status_ok_text=_("Saved script '{0}'.").format(name))
            self.executor.execute(_handle_save_named)
            return

        if kind == "attach_script":
            name = str(msg.get("name", "") or "").strip()
            script_code = _script_code_from_message(msg)
            overwrite = bool(msg.get("overwrite"))

            def _handle_attach() -> None:
                from plugin.scripting.document_scripts import attach_document_script

                doc = self._resolve_run_script_doc()
                if doc is None:
                    self._send_scripts_list(status_error_text=_("No document is open to attach scripts."))
                    return
                err = attach_document_script(doc, name, script_code, overwrite=overwrite)
                if err:
                    self._send_scripts_list(status_error_text=err)
                    return
                self._send_scripts_list(status_ok_text=_("Attached script '{0}' to this document.").format(name))
            self.executor.execute(_handle_attach)
            return

        if kind == "copy_script_to_user":
            name = str(msg.get("name", "") or "").strip()
            script_code = _script_code_from_message(msg)
            overwrite = bool(msg.get("overwrite"))

            def _handle_copy() -> None:
                from plugin.framework.config import get_config

                if not name:
                    self._send_scripts_list(status_error_text=_("Script name cannot be empty."))
                    return
                scripts = get_config("saved_python_scripts")
                if not isinstance(scripts, dict):
                    scripts = {}
                if name in scripts and not overwrite:
                    self._send_scripts_list(
                        status_error_text=_("A script named '{0}' already exists in My Scripts.").format(name)
                    )
                    return
                self._save_user_script(name, script_code)
                self._send_scripts_list(status_ok_text=_("Copied script '{0}' to My Scripts.").format(name))
            self.executor.execute(_handle_copy)
            return

        if kind == "delete_script":
            name = str(msg.get("name", "") or "").strip()
            origin = str(msg.get("origin", "") or "").strip()

            def _handle_delete() -> None:
                from plugin.scripting.document_scripts import SCRIPT_ORIGIN_DOCUMENT, delete_document_script

                if not name:
                    self._send_scripts_list(status_error_text=_("Script name cannot be empty."))
                    return
                if origin == SCRIPT_ORIGIN_DOCUMENT:
                    doc = self._resolve_run_script_doc()
                    if doc is None:
                        self._send_scripts_list(status_error_text=_("No document is open."))
                        return
                    err = delete_document_script(doc, name)
                    if err:
                        self._send_scripts_list(status_error_text=err)
                        return
                    self._send_scripts_list(status_ok_text=_("Deleted document script '{0}'.").format(name))
                    return
                from plugin.framework.config import get_config, set_config

                scripts = get_config("saved_python_scripts")
                if not isinstance(scripts, dict):
                    scripts = {}
                scripts.pop(name, None)
                set_config("saved_python_scripts", scripts)
                log.info("editor_host: delete_script '%s' (user)", name)
                self._send_scripts_list(status_ok_text=_("Deleted script '{0}'.").format(name))
            self.executor.execute(_handle_delete)
            return

        if kind == "save":
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

            def _handle_save() -> None:
                try:
                    on_save = self.on_save
                    if on_save is not None:
                        result = on_save(code, save_as_plain, data_binding, action)
                    else:
                        result = {"type": "saved", "ok": True}
                    if not isinstance(result, dict):
                        result = {"type": "saved", "ok": True}
                    self.send(result)
                except Exception as e:
                    log.exception("Editor save handler failed")
                    self.send({"type": "error", "message": str(e), "traceback": exception_traceback(e)})

            self.executor.execute(_handle_save, timeout=60.0)
            return

        if kind in ("closed", "cancel"):
            log.info("editor_host _dispatch_incoming: received close/cancel kind=%r", kind)
            # Capture callbacks at dispatch time so the handler can detect if a new
            # session has superseded this one before the executor processes the event.
            captured_on_save = self.on_save
            captured_on_closed = self.on_closed
            def _handle_close() -> None:
                try:
                    if captured_on_closed is not None:
                        captured_on_closed()
                except Exception:
                    log.exception("Editor on_closed failed")
                finally:
                    self._closed_event.set()
                    # Only clear and tear down if callbacks have not been replaced by
                    # a new session that was opened while this close event was queued.
                    if self.on_save is captured_on_save:
                        self.on_save = None
                    if self.on_closed is captured_on_closed:
                        self.on_closed = None
                        set_active_session(None)

            self.executor.execute(_handle_close)
            return

        if kind == "ready":
            self._ready_event.set()
            return
        log.debug("Editor child message: %s", kind)

    def _handle_disconnect(self) -> None:
        """Handle case where the subprocess exits or disconnects unexpectedly."""
        # Capture at schedule time; the reader loop finishes after terminate_persistent_editor()
        # which may race with a new session already installing its callbacks.
        captured_on_save = self.on_save
        captured_on_closed = self.on_closed
        def _handle_close() -> None:
            try:
                if captured_on_closed is not None:
                    captured_on_closed()
            except Exception:
                log.exception("Editor on_closed failed during disconnect")
            finally:
                self._closed_event.set()
                # Only clear if not superseded by a new session.
                if self.on_save is captured_on_save:
                    self.on_save = None
                if self.on_closed is captured_on_closed:
                    self.on_closed = None
                    set_active_session(None)
        self.executor.execute(_handle_close)


_PERSISTENT_EDITOR = PersistentEditor()


class EditorSession:
    """One editor session wrapper, delegating to the PersistentEditor singleton."""

    def __init__(
        self,
        proc: "subprocess_types.Popen[bytes]",
        *,
        on_save: Callable[..., dict[str, Any]],
        on_closed: Callable[[], None],
        executor: QueueExecutor | None = None,
    ) -> None:
        self._proc = proc
        self._on_save = on_save
        self._on_closed = on_closed
        self._executor = executor or default_executor

        _PERSISTENT_EDITOR.on_save = on_save
        _PERSISTENT_EDITOR.on_closed = on_closed
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
        # Only clear the shared callbacks if they still belong to this session.
        # A new session may have already set its own callbacks on _PERSISTENT_EDITOR
        # (via EditorSession.__init__) before _finish() is called, so we must not
        # blindly wipe them.
        if _PERSISTENT_EDITOR.on_save is self._on_save:
            _PERSISTENT_EDITOR.on_save = None
        if _PERSISTENT_EDITOR.on_closed is self._on_closed:
            _PERSISTENT_EDITOR.on_closed = None

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
        if session is not None and _ACTIVE_SESSION is not None and _ACTIVE_SESSION is not session:
            _ACTIVE_SESSION._finish()
        if session is None and _ACTIVE_SESSION is not None:
            _ACTIVE_SESSION._finish()
        _ACTIVE_SESSION = session


def terminate_persistent_editor() -> None:
    """Force terminate the background Monaco editor process."""
    _PERSISTENT_EDITOR.terminate()


def _on_config_changed(**kwargs: Any) -> None:
    key = kwargs.get("key", "")
    if key == "scripting.python_venv_path":
        log.info("editor_host: scripting.python_venv_path changed, terminating background Monaco process")
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


def launch_monaco_editor(
    ctx: Any,
    *,
    exe: str,
    load_message: dict[str, Any],
    on_save: Callable[..., dict[str, Any]],
    on_closed: Callable[[], None] | None = None,
) -> bool:
    """Start or reuse the Monaco child process and send *load_message*. Return True on success."""
    from plugin.chatbot.dialogs import msgbox_with_report

    _PERSISTENT_EDITOR.ctx = ctx
    # Host-only keys (e.g. pyuno document refs) must not cross the pickle IPC boundary.
    ipc_message = dict(load_message)
    run_script_doc = None
    if ipc_message.get("mode") == "run_script":
        run_script_doc = ipc_message.pop("run_script_doc", None)
        _PERSISTENT_EDITOR.set_run_script_document(run_script_doc)
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

            # Prefer any doc we had for the run_script case or if caller left it in msg
            doc_for_theme = run_script_doc or ipc_message.get("doc") or ipc_message.get("run_script_doc")
            ipc_message["theme"] = get_monaco_theme_info(doc=doc_for_theme, ctx=ctx)
        except Exception:
            log.debug("Failed to compute monaco theme info; falling back to light", exc_info=True)
            ipc_message["theme"] = {"monaco": "vs", "is_dark": False}

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
            msg = failure_message(_("Could not start the Python editor."), exc=e)
            msgbox_with_report(ctx, "WriterAgent", msg, box_type=3, reportable=True, report_title="Python editor spawn failed", report_extra=msg)
            return False

        session = EditorSession(proc, on_save=on_save, on_closed=closed_handler)
        set_active_session(session)
        session.start_reader()

        if not session.wait_for_ready(ctx, timeout_sec=45.0):
            detail = session.read_stderr_tail()
            set_active_session(None)
            msg = failure_message(_("The Python editor window did not start."), detail=detail)
            msgbox_with_report(ctx, "WriterAgent", msg, box_type=3, reportable=True, report_title="Python editor did not start", report_extra=msg)
            return False

        # Trigger background pre-warming of the venv subprocess now that Monaco is successfully up.
        run_in_background(warm_venv_worker, ctx, name="warm-venv-worker")

    if not session.is_running:
        detail = session.read_stderr_tail()
        set_active_session(None)
        msg = failure_message(_("The Python editor exited before it could load your code."), detail=detail)
        msgbox_with_report(
            ctx,
            "WriterAgent",
            msg,
            box_type=3,
            reportable=True,
            report_title="Python editor exited early",
            report_extra=msg,
        )
        return False

    try:
        session.send(ipc_message)
    except Exception as e:
        log.exception("Failed to send load to editor")
        set_active_session(None)
        msg = failure_message(_("Could not talk to the Python editor."), detail=session.read_stderr_tail(), exc=e)
        msgbox_with_report(
            ctx,
            "WriterAgent",
            msg,
            box_type=3,
            reportable=True,
            report_title="Python editor IPC failed",
            report_extra=msg,
        )
        return False

    return True
