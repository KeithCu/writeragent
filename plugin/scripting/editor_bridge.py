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
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import subprocess

from plugin.framework.queue_executor import QueueExecutor, default_executor
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.editor_diagnostics import exception_traceback
from plugin.scripting.editor_protocol import message_type, read_message, write_message

log = logging.getLogger(__name__)

_SESSION_LOCK = threading.Lock()
_ACTIVE_SESSION: EditorSession | None = None


class EditorSession:
    """One editor subprocess and pipe reader."""

    def __init__(
        self,
        proc: "subprocess.Popen[bytes]",
        *,
        on_save: Callable[[str, bool], dict[str, Any]],
        on_closed: Callable[[], None],
        executor: QueueExecutor | None = None,
    ) -> None:
        self._proc = proc
        self._on_save = on_save
        self._on_closed = on_closed
        self._executor = executor or default_executor
        self._stdin_lock = threading.Lock()
        self._closed = threading.Event()
        self._ready_event = threading.Event()
        self._reader_thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        if self._closed.is_set():
            return False
        return self._proc.poll() is None

    def start_reader(self) -> None:
        self._reader_thread = run_in_background(self._read_loop, name="editor-pipe-reader", daemon=True)

    def send(self, message: dict[str, Any]) -> None:
        """Thread-safe write to child stdin."""
        with self._stdin_lock:
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

    def wait_for_ready(self, ctx: Any, timeout_sec: float = 30.0) -> bool:
        """Wait for ``ready`` while pumping LibreOffice UI events."""
        from plugin.framework.uno_context import get_toolkit

        toolkit = get_toolkit(ctx)
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if self._ready_event.is_set():
                return True
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
            log.error("Editor ready timeout (%ss). child_running=%s stderr=%s", timeout_sec, self._proc.poll() is None, self.read_stderr_tail())
        return self._ready_event.is_set()

    def _read_loop(self) -> None:
        stdout = self._proc.stdout
        if stdout is None:
            return
        try:
            while not self._closed.is_set() and self._proc.poll() is None:
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
            self._finish()

    def _dispatch_incoming(self, msg: dict[str, Any]) -> None:
        kind = message_type(msg)
        if kind == "save":
            code = msg.get("code")
            if not isinstance(code, str):
                code = ""

            save_as_plain = bool(msg.get("save_as_plain"))

            def _handle_save() -> None:
                try:
                    result = self._on_save(code, save_as_plain)
                    if not isinstance(result, dict):
                        result = {"type": "saved", "ok": True}
                    self.send(result)
                except Exception as e:
                    log.exception("Editor save handler failed")
                    self.send({"type": "error", "message": str(e), "traceback": exception_traceback(e)})

            self._executor.execute(_handle_save, timeout=60.0)
            return
        if kind in ("closed", "cancel"):
            self._finish()
            return
        if kind == "ready":
            self._ready_event.set()
            return
        log.debug("Editor child message: %s", kind)

    def _finish(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._on_closed()
        except Exception:
            log.exception("Editor on_closed failed")
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
        except OSError:
            pass
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
