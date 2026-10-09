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
import queue
import subprocess
import threading
import time
from typing import Any, cast

from plugin.framework.errors import ToolExecutionError, safe_json_loads
from plugin.framework.worker_pool import BackgroundHandle, StderrTail, get_subprocess_creationflags, run_in_background, start_stderr_drain

log = logging.getLogger(__name__)

_JSONRPC_VERSION = "2.0"


def _is_jsonrpc_id(value: Any) -> bool:
    """JSON-RPC 2.0 id: string, number, or null. Not an array, object, or boolean.

    ``bool`` subclasses ``int``, and ``True`` hashes equal to ``1``, so a JSON
    ``true`` id would complete the in-flight integer request if it were used
    as a ``_pending`` key.
    """
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))


class ACPConnection:
    """Manages a JSON-RPC stdio connection to an ACP subprocess."""

    _cmd_line: list[str]
    _env: dict[str, str] | None
    _cwd: str | None
    _lock: threading.Lock
    _request_id: int
    _running: bool
    _notify_callback: Any
    _write_queue: queue.Queue[tuple[str, Any, bytes | None]]
    _writer_started: bool

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
        self._notify_callback = None
        self._write_queue = queue.Queue()
        self._writer_started = False

    def start(self) -> None:
        """Spawn the ACP subprocess."""
        log.info(f"Spawning: {' '.join(self._cmd_line)}")

        env = dict(os.environ)
        if self._env:
            env.update(self._env)

        from plugin.scripting.sandbox import wrap_command_for_sandbox

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

        ``stop()`` used to close the child and leave every ``send_request``
        waiting on its ``Event`` until the caller's timeout (the prompt uses
        600s). The chat worker stayed parked after the UI had already shown
        Stopped, and the reader stayed in ``readline`` until that timeout
        too. Publish a cancellation error and set every pending event, then
        terminate. A second ``stop()`` sees no process and only wakes
        waiters. The reader loop uses the same sweep when it exits for any
        other reason.
        """
        self._wake_pending("ACP process stopped")
        with self._lock:
            proc = self._proc
            # Claim it so a concurrent shutdown does not terminate twice.
            self._proc = None
            # What was wrong: this also set ``_stderr_drain`` to None.
            # The reader finally logs that drain after stop() returns, so
            # a normal Stop dropped the tail and never logged it.
            # Why: this connection is not respawned. Leave the drain for
            # the reader finally and stderr_text().
        if proc is None:
            return

        # Stdin writes and stdin.close() on the calling thread freeze the UI
        # when the pipe buffer is full. Sending writes to the background
        # while stop() calls terminate() immediately races and kills the
        # child before session/cancel is sent. Sequence writes and stop
        # through a background FIFO: session/cancel and permission responses
        # flush before proc.stdin.close() and proc.terminate() run
        # off-thread. A fallback watchdog forces terminate() if a wedged
        # pipe blocks writes.
        self._enqueue_write("stop", proc, None)

        def _watchdog_terminate() -> None:
            time.sleep(0.5)
            try:
                if proc and proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
            try:
                if proc and proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass

        run_in_background(_watchdog_terminate, name="acp-stop-watchdog", dedicated=True)

    def _enqueue_write(self, action: str, proc: Any, data: bytes | None) -> None:
        with self._lock:
            self._write_queue.put((action, proc, data))
            if not self._writer_started:
                self._writer_started = True
                run_in_background(self._writer_drain_loop, name="acp-writer", dedicated=True)

    @background
    def _writer_drain_loop(self) -> None:
        while True:
            try:
                item = self._write_queue.get(timeout=0.2)
            except queue.Empty:
                with self._lock:
                    if not self._running and self._write_queue.empty():
                        self._writer_started = False
                        break
                continue

            action, proc, data = item
            if action == "write":
                try:
                    stdin = proc.stdin if proc is not None else None
                    if stdin and data is not None:
                        stdin.write(data)
                        stdin.flush()
                except Exception:
                    # A closed pipe during stop is expected. Debug keeps a
                    # failed session/cancel visible without an error traceback.
                    log.debug("ACP stdin write failed", exc_info=True)
            elif action == "stop":
                try:
                    if proc and proc.stdin:
                        proc.stdin.close()
                except Exception:
                    pass
                try:
                    if proc:
                        proc.terminate()
                except Exception:
                    pass
                if proc:
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                # A child that exits while stdout is still held open, or during
                # stop(), leaves readline() blocked. Closing proc.stdout here
                # unblocks the reader without polling the pipe.
                try:
                    if proc and proc.stdout:
                        proc.stdout.close()
                except Exception:
                    pass
                with self._lock:
                    self._writer_started = False
                break

    @property
    def is_alive(self) -> bool:
        # What was wrong: ``self._proc is not None and self._proc.poll()``
        # loaded ``_proc`` twice. stop() sets it to None between those
        # loads, so the second was None.poll() and raised AttributeError.
        # Why: keep the process from the first load.
        proc = self._proc
        return proc is not None and proc.poll() is None

    def stderr_text(self) -> str:
        """Stderr already captured by the live drain. Does not join that thread."""
        drain = self._stderr_drain
        if drain is None:
            return ""
        return drain.text()

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
            # stop() sets self._proc to None between two loads of it. The write
            # below must use the process from this locked check. Re-reading
            # self._proc (`if self._proc and self._proc.stdin`) hits None.stdin
            # and raises AttributeError, which is not BrokenPipeError or
            # OSError, so it escapes the handler. A pipe stop() already closed
            # raises ValueError ("I/O operation on closed file"), which is the
            # same failed write.
            proc = self._proc
            self._pending[req_id] = {"event": event, "response": None}

        line = json.dumps(msg) + "\n"
        log.debug(f"→ {method} (id={req_id})")

        try:
            stdin = proc.stdin
            if stdin:
                stdin.write(line.encode("utf-8"))
                stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
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
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        msg = {"jsonrpc": _JSONRPC_VERSION, "method": method, "params": params or {}}
        line = json.dumps(msg) + "\n"
        self._enqueue_write("write", proc, line.encode("utf-8"))

    def send_response(self, msg_id: Any, result: Any = None, error: Any = None) -> None:
        """Send a JSON-RPC response to a request from the agent."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        msg = {"jsonrpc": _JSONRPC_VERSION, "id": msg_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result or {}

        line = json.dumps(msg) + "\n"
        self._enqueue_write("write", proc, line.encode("utf-8"))

    def set_notification_callback(self, callback: Any) -> None:
        """Set a callback(method, params, msg_id) for incoming notifications."""
        self._notify_callback = callback

    @background
    def _reader_loop(self) -> None:
        """Read JSON-RPC messages from stdout and dispatch them."""
        log.info("Reader loop started")
        try:
            # Popen.poll() is waitpid(WNOHANG) and becomes set the moment the
            # child exits, independent of unread stdout. Requiring poll() is
            # None dropped a session/prompt response the child wrote and then
            # exited while this thread was inside a session/update callback.
            # The finally sweep then failed the turn with "ACP process
            # terminated" and the answer was discarded. readline() returns
            # b"" at EOF, so the poll() check only dropped buffered bytes.
            # Waiters that still have no response are failed by the sweep.
            while self._running:
                # stop() sets self._proc to None between the while test and
                # self._proc.stdout. Copy the process once per iteration.
                # None means stop() already claimed it; leave without touching
                # stdout. Re-reading raises AttributeError and the except
                # logs "Reader error".
                proc = self._proc
                if proc is None:
                    break
                try:
                    if proc.stdout is None:
                        break
                    raw = proc.stdout.readline()
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

                    msg = safe_json_loads(line)
                    if msg is None:
                        log.debug(f"Non-JSON output: {line[:200]}")
                        continue
                    # JSON-RPC messages are objects. A stdout line that is valid
                    # JSON but not an object (bare number, true/false, quoted
                    # string, or a JSON array) reaches `"id" in msg` or
                    # msg.get and raises TypeError or AttributeError. The
                    # inner except then breaks the reader, and the finally
                    # sweep fails the in-flight session/prompt with "ACP
                    # process terminated" while the child is still alive.
                    # Log stray stdout and keep reading. Do not gate this
                    # loop on Popen.poll() — that drops a response already
                    # buffered when the child exits.
                    if not isinstance(msg, dict):
                        log.debug(f"Non-object JSON output: {line[:200]}")
                        continue
                    # JSON-RPC ids are string, number, or null. An array or object
                    # id passes the dict guard, then pending.get(req_id) raises
                    # TypeError (unhashable type). The inner except breaks the
                    # reader, and the finally sweep fails the in-flight
                    # session/prompt while the child is still alive. Log it
                    # and keep reading.
                    if "id" in msg and not _is_jsonrpc_id(msg["id"]):
                        log.debug(f"Non-JSON-RPC id output: {line[:200]}")
                        continue

                    if "id" in msg and msg["id"] is not None and "method" not in msg:
                        # Response to our request. Assign under the same lock
                        # _wake_pending uses, so a stop sweep cannot observe a
                        # missing response and overwrite this one.
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
                        # What was wrong: ``if self._notify_callback:`` then
                        # called ``self._notify_callback(...)``. shutdown()
                        # sets it to None between those reads, so the call
                        # was None(...) and logged "Notification callback
                        # error". Why: copy the callback once.
                        callback = self._notify_callback
                        if callback:
                            try:
                                callback(method, params, msg_id)
                            except Exception:
                                log.exception("Notification callback error")

                except Exception:
                    if self._running:
                        log.exception("Reader error")
                    break
        finally:
            # A child exit, stdout EOF, or an unexpected exception ends this
            # loop. The sweep used to live only in stop(), so in-flight Events
            # stayed unset, session/prompt sat on event.wait(600), and the
            # sidebar stayed on Sending. Every reader exit wakes waiters the
            # same way stop() does. A response stop() already stored is not
            # replaced. Non-object JSON and objects with a non-JSON-RPC id
            # are skipped above and do not take this path.
            self._wake_pending("ACP process terminated")
            # Live drain already collected stderr; log a bounded tail for debugging.
            drain = self._stderr_drain
            if drain is not None:
                stderr_text = drain.finish_text().strip()
                if stderr_text:
                    log.warning("ACP stderr: %s", stderr_text[:500])

            log.info("Reader loop ended")
