# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Generic Agent Communication Protocol (ACP) adapter over stdio JSON-RPC.

Supports initial handshakes, prompt sessions, and streaming notifications
acting as an ACP client connected to a supporting agent binary backend.
"""

from __future__ import annotations

from plugin.framework.thread_guard import background
import json
import logging
import os
import subprocess
import threading
from typing import Any, cast

from plugin.framework.errors import ToolExecutionError
from plugin.framework.worker_pool import BackgroundHandle, StderrTail, get_subprocess_creationflags, run_in_background, start_stderr_drain

log = logging.getLogger(__name__)

_LOG = "ABP"

_JSONRPC_VERSION = "2.0"
_ACP_PROTOCOL_VERSION = 1


class ACPConnection:
    """Manages a JSON-RPC stdio connection to an ACP subprocess."""

    _cmd_line: list[str]
    _env: dict[str, str] | None
    _cwd: str | None
    _lock: threading.Lock
    _request_id: int
    _running: bool
    _notify_callback: Any

    def __init__(self, cmd_line: list[str], env: dict[str, str] | None = None, cwd: str | None = None) -> None:
        self._cmd_line = cmd_line
        self._env = env
        self._cwd = cwd
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._pending: dict[Any, Any] = {}  # id -> threading.Event, response dict
        self._reader_thread: BackgroundHandle | None = None
        self._stderr_drain: StderrTail | None = None
        self._running = False
        self._notifications: list[Any] = []  # queue of notification dicts
        self._notify_callback = None

    def start(self) -> None:
        """Spawn the ACP subprocess."""
        log.info(f"Spawning: {' '.join(self._cmd_line)}")

        env = dict(os.environ)
        if self._env:
            env.update(self._env)

        from plugin.scripting.venv_worker import wrap_command_for_sandbox

        self._proc = cast("subprocess.Popen[bytes]", subprocess.Popen(wrap_command_for_sandbox(self._cmd_line), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=self._cwd, **get_subprocess_creationflags()))
        self._stderr_drain = start_stderr_drain(self._proc.stderr, name=f"acp-stderr-{self._proc.pid}")
        self._running = True
        self._reader_thread = run_in_background(self._reader_loop, daemon=True, name="acp-reader", dedicated=True)

    def _wake_pending(self, message: str) -> None:
        """Fail in-flight ``send_request`` calls that have no response yet.

        ``_running`` is cleared under the same lock that registers a waiter,
        so a request that arrives after this sweep fails immediately instead
        of waiting out its timeout. A response already stored is kept.
        """
        with self._lock:
            self._running = False
            for entry in self._pending.values():
                if entry.get("response") is None:
                    entry["response"] = {"error": {"message": message}}
                event = entry.get("event")
                if event is not None:
                    event.set()

    def stop(self) -> None:
        """Terminate the subprocess and unblock in-flight ``send_request`` calls.

        What was wrong: ``stop()`` closed the child but left every
        ``send_request`` waiting on its ``Event`` until the caller's timeout
        (the prompt uses 600s). The chat worker stayed parked after the UI
        had already shown Stopped, and the reader stayed in ``readline``
        until that timeout too. Why: publish a cancellation error and set
        every pending event, then terminate. A second ``stop()`` sees no
        process and only wakes waiters. The reader loop uses the same sweep
        when it exits for any other reason.
        """
        self._wake_pending("ACP process stopped")
        with self._lock:
            proc = self._proc
            # Claim it so a concurrent shutdown does not terminate twice.
            self._proc = None
            self._stderr_drain = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _next_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

    def send_request(self, method: str, params: Any = None, timeout: int = 120) -> Any:
        """Send a JSON-RPC request and wait for the response."""
        if not self.is_alive:
            raise ToolExecutionError("ACP process is not running")

        req_id = self._next_id()
        msg = {"jsonrpc": _JSONRPC_VERSION, "id": req_id, "method": method, "params": params or {}}

        event = threading.Event()
        with self._lock:
            # stop() may have swept _pending and dropped _proc between the
            # is_alive check above and this registration. Registering after
            # that sweep would wait until timeout: the event is never set.
            if not self._running or self._proc is None:
                raise ToolExecutionError("ACP process is not running")
            self._pending[req_id] = {"event": event, "response": None}

        line = json.dumps(msg) + "\n"
        log.debug(f"→ {method} (id={req_id})")

        try:
            if self._proc and self._proc.stdin:
                self._proc.stdin.write(line.encode("utf-8"))
                self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            with self._lock:
                self._pending.pop(req_id, None)
            raise ToolExecutionError(f"Failed to write to ACP: {e}") from e

        if not event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            raise TimeoutError(f"ACP request {method} timed out after {timeout}s")

        with self._lock:
            entry = self._pending.pop(req_id, {})

        resp = entry.get("response")
        if resp and "error" in resp:
            err = resp["error"]
            msg_str = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise ToolExecutionError(f"ACP error: {msg_str}")

        return resp.get("result") if resp else None

    def send_notification(self, method: str, params: Any = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if not self.is_alive:
            return
        msg = {"jsonrpc": _JSONRPC_VERSION, "method": method, "params": params or {}}
        line = json.dumps(msg) + "\n"
        try:
            if self._proc and self._proc.stdin:
                self._proc.stdin.write(line.encode("utf-8"))
                self._proc.stdin.flush()
        except Exception:
            pass

    def send_response(self, msg_id: Any, result: Any = None, error: Any = None) -> None:
        """Send a JSON-RPC response to a request from the agent."""
        if not self.is_alive:
            return
        msg = {"jsonrpc": _JSONRPC_VERSION, "id": msg_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result or {}

        line = json.dumps(msg) + "\n"
        try:
            if self._proc and self._proc.stdin:
                self._proc.stdin.write(line.encode("utf-8"))
                self._proc.stdin.flush()
        except Exception:
            log.exception("Failed to send response")

    def set_notification_callback(self, callback: Any) -> None:
        """Set a callback(method, params, msg_id) for incoming notifications."""
        self._notify_callback = callback

    @background
    def _reader_loop(self) -> None:
        """Read JSON-RPC messages from stdout and dispatch them."""
        log.info("Reader loop started")
        try:
            # What was wrong: this guard also required poll() is None.
            # Popen.poll() is waitpid(WNOHANG) and becomes set the moment the
            # child exits, independent of unread stdout. If the child wrote
            # the session/prompt response and exited while this thread was
            # inside a session/update callback, the loop never called
            # readline() again. The finally sweep then failed the turn with
            # "ACP process terminated" and the answer was discarded.
            # Why: readline() returns b"" at EOF, so the poll() check only
            # dropped buffered bytes. Waiters that still have no response
            # are failed by the sweep below.
            while self._running and self._proc:
                try:
                    if self._proc.stdout is None:
                        break
                    raw = self._proc.stdout.readline()
                    if not raw:
                        break
                    # Popen is binary. Reusing `line` for the decoded str leaves
                    # mypy on bytes, so find("{") and the debug f-string still see bytes.
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    idx = line.find("{")
                    if idx >= 0:
                        line = line[idx:]

                    from plugin.framework.errors import safe_json_loads

                    msg = safe_json_loads(line)
                    if msg is None:
                        log.debug(f"Non-JSON output: {line[:200]}")
                        continue

                    if "id" in msg and msg["id"] is not None and "method" not in msg:
                        # Response to our request
                        req_id = msg["id"]
                        with self._lock:
                            entry = self._pending.get(req_id)
                        if entry:
                            entry["response"] = msg
                            entry["event"].set()
                        else:
                            log.warning(f"Response for unknown id={req_id}")
                    else:
                        # Notification or Request from the agent
                        method = msg.get("method", "")
                        params = msg.get("params", {})
                        msg_id = msg.get("id")
                        if self._notify_callback:
                            try:
                                self._notify_callback(method, params, msg_id)
                            except Exception:
                                log.exception("Notification callback error")

                except Exception:
                    if self._running:
                        log.exception("Reader error")
                    break
        finally:
            # What was wrong: this sweep lived only in stop(). A child exit,
            # stdout EOF, or an exception here (malformed non-object JSON)
            # ended the loop and left in-flight Events unset, so
            # session/prompt sat on event.wait(600) and the sidebar stayed
            # on Sending. Why: every reader exit wakes waiters the same way
            # stop() does. A response stop() already stored is not replaced.
            self._wake_pending("ACP process terminated")
            # Live drain already collected stderr; log a bounded tail for debugging.
            drain = self._stderr_drain
            if drain is not None:
                stderr_text = drain.finish_text().strip()
                if stderr_text:
                    log.warning("ACP stderr: %s", stderr_text[:500])

            log.info("Reader loop ended")
