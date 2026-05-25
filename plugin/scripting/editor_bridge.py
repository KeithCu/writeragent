# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pipe bridge between LibreOffice and the Monaco editor child process."""

from __future__ import annotations

import logging
import select
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import subprocess

from plugin.framework.queue_executor import QueueExecutor, default_executor
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.editor_diagnostics import exception_traceback
from plugin.scripting.editor_protocol import message_type, read_message, write_message
from plugin.framework.event_bus import global_event_bus

log = logging.getLogger(__name__)

_SESSION_LOCK = threading.RLock()
_ACTIVE_SESSION: EditorSession | None = None


class PersistentEditor:
    """Manages a single Monaco editor subprocess and keeps it alive in the background."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
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

    @property
    def is_running(self) -> bool:
        if self._proc is None:
            return False
        return self._proc.poll() is None

    @property
    def proc(self) -> subprocess.Popen[bytes] | None:
        return self._proc

    def start(self, proc: subprocess.Popen[bytes]) -> None:
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
        if self._proc is None:
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

    def _stderr_drain_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        stderr = proc.stderr
        try:
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

    def _read_loop(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        stdout = self._proc.stdout
        try:
            while self._proc is not None and self._proc.poll() is None:
                ready, _, _ = select.select([stdout], [], [], 0.5)
                if not ready:
                    continue
                msg = read_message(stdout)
                if msg is None:
                    break
                self._dispatch_incoming(msg)
        except Exception:
            log.exception("Editor pipe reader failed")
        finally:
            log.info("editor_bridge: persistent reader loop finished.")
            self._handle_disconnect()

    def _dispatch_incoming(self, msg: dict[str, Any]) -> None:
        kind = message_type(msg)
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
            def _handle_close() -> None:
                try:
                    on_closed = self.on_closed
                    if on_closed is not None:
                        on_closed()
                except Exception:
                    log.exception("Editor on_closed failed")
                finally:
                    self.on_save = None
                    self.on_closed = None
                    self._closed_event.set()
                    set_active_session(None)

            self.executor.execute(_handle_close)
            return

        if kind == "ready":
            self._ready_event.set()
            return
        log.debug("Editor child message: %s", kind)

    def _handle_disconnect(self) -> None:
        """Handle case where the subprocess exits or disconnects unexpectedly."""
        def _handle_close() -> None:
            try:
                on_closed = self.on_closed
                if on_closed is not None:
                    on_closed()
            except Exception:
                log.exception("Editor on_closed failed during disconnect")
            finally:
                self.on_save = None
                self.on_closed = None
                self._closed_event.set()
                set_active_session(None)
        self.executor.execute(_handle_close)


_PERSISTENT_EDITOR = PersistentEditor()


class EditorSession:
    """One editor session wrapper, delegating to the PersistentEditor singleton."""

    def __init__(
        self,
        proc: "subprocess.Popen[bytes]",
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
        _PERSISTENT_EDITOR.on_save = None
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


def _on_config_changed(key: str, **kwargs: Any) -> None:
    if key == "scripting.python_venv_path":
        log.info("editor_bridge: scripting.python_venv_path changed, terminating background Monaco process")
        terminate_persistent_editor()


global_event_bus.subscribe("config:changed", _on_config_changed)
