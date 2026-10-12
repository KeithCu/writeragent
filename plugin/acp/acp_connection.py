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
import math
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, cast

from plugin.framework.errors import ToolExecutionError, safe_json_loads
from plugin.framework.worker_pool import BackgroundHandle, StderrTail, get_subprocess_creationflags, run_in_background, start_stderr_drain

log = logging.getLogger(__name__)

_JSONRPC_VERSION = "2.0"
# send_request's idle wait polls this often so an inbound line can extend it.
_IDLE_POLL_S = 0.05
# Not a JSON-RPC id. None is a valid id, so rejection cannot use None.
_BAD_ID = object()


def _canonical_id(value: Any) -> Any:
    """Pending-map key for a JSON-RPC id, or ``_BAD_ID``.

    ``bool`` subclasses ``int``, and ``True`` hashes equal to ``1``, so a JSON
    ``true`` id would complete the in-flight integer request. A whole-number
    float has the same hash: JSON ``1.0`` must become ``1`` or it collides
    with request id ``1`` by accident and ``1.5`` would be stored as a float
    key we never send.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return _BAD_ID
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return _BAD_ID
        return int(value)
    return _BAD_ID


@dataclass
class _Pending:
    """One in-flight JSON-RPC request."""

    event: threading.Event
    response: Any = None


@dataclass
class _QueuedWrite:
    """One stdin line. ``fail_req_id`` is set for ``send_request``."""

    proc: Any
    data: bytes
    fail_req_id: int | None = None


@dataclass
class _QueuedStop:
    """Close stdin and kill this process. Carries the ``stop()`` snapshot."""

    proc: Any


def _accept_message(line: str) -> dict[str, Any] | None:
    """Return a JSON-RPC object, or None for stray stdout.

    A line that is valid JSON but not an object (a number, boolean, string,
    or array) used to reach ``msg.get`` and raise. The reader then stopped,
    and the finally sweep failed the in-flight ``session/prompt`` while the
    child was still alive. An ``id`` that is an array, object, or JSON
    boolean is the same stray line: ``pending.get`` would raise ``TypeError``
    on an unhashable key, or ``true`` would hash equal to request id ``1``.

    What was wrong: ``find("{")`` ran on every line, so ``[{"a": 1}]`` became
    ``{"a": 1}`` and was dispatched as an object. Why: slice a prefix only
    when the line is not already a JSON object or array. A log line in front
    of an object still parses.
    """
    payload = line.lstrip()
    if not payload.startswith("{") and not payload.startswith("["):
        idx = line.find("{")
        if idx >= 0:
            payload = line[idx:]
    msg = safe_json_loads(payload)
    if msg is None:
        log.debug("Non-JSON output: %s", payload[:200])
        return None
    if not isinstance(msg, dict):
        log.debug("Non-object JSON output: %s", payload[:200])
        return None
    if "id" in msg:
        canonical = _canonical_id(msg["id"])
        if canonical is _BAD_ID:
            log.debug("Non-JSON-RPC id output: %s", payload[:200])
            return None
        msg["id"] = canonical
    return msg


def _kill_acp_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill ``proc`` and its descendants.

    ``terminate()`` and ``kill()`` signal only the direct child. A wrapper
    (node, npx, a shell script) can exit and leave a grandchild holding
    stdout, so ``readline`` never sees EOF. ``BufferedReader.close()`` does
    not unblock that read: it waits on the same lock ``readline`` holds.
    """
    from plugin.scripting.venv_worker import _kill_process_tree

    try:
        _kill_process_tree(proc)
    except Exception:
        log.debug("ACP process tree kill failed", exc_info=True)
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            log.debug("ACP process kill failed", exc_info=True)


class ACPConnection:
    """Manages a JSON-RPC stdio connection to an ACP subprocess."""

    _cmd_line: list[str]
    _env: dict[str, str] | None
    _cwd: str | None
    _lock: threading.Lock
    _request_id: int
    _running: bool
    _notify_callback: Any
    _write_queue: queue.Queue[Any]
    _writer_thread: BackgroundHandle | None
    _last_inbound: float

    def __init__(self, cmd_line: list[str], env: dict[str, str] | None = None, cwd: str | None = None) -> None:
        self._cmd_line = cmd_line
        self._env = env
        self._cwd = cwd
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._pending: dict[Any, _Pending] = {}
        self._reader_thread: BackgroundHandle | None = None
        self._stderr_drain: StderrTail | None = None
        self._running = False
        self._notify_callback = None
        self._write_queue = queue.Queue()
        self._writer_thread = None
        self._last_inbound = time.monotonic()

    def start(self) -> None:
        """Spawn the ACP subprocess."""
        log.info(f"Spawning: {' '.join(self._cmd_line)}")

        env = dict(os.environ)
        if self._env:
            env.update(self._env)

        from plugin.scripting.sandbox import wrap_command_for_sandbox

        # preexec_fn=os.setsid runs Python between fork and exec. Another
        # LibreOffice thread can hold a lock the child inherits, so the spawn
        # deadlocks before exec. start_new_session asks the C spawn path to
        # call setsid. The child is its own session, so killing that group
        # does not signal LibreOffice. Windows has no start_new_session;
        # taskkill /T covers the tree there.
        popen_kw = get_subprocess_creationflags()
        if sys.platform != "win32":
            popen_kw["start_new_session"] = True
        self._proc = cast("subprocess.Popen[bytes]", subprocess.Popen(wrap_command_for_sandbox(self._cmd_line), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=self._cwd, **popen_kw))
        self._stderr_drain = start_stderr_drain(self._proc.stderr, name=f"acp-stderr-{self._proc.pid}")
        self._running = True
        # One thread owns stdin for the life of the child. Request writes,
        # notifications, and the kill all go through it.
        self._ensure_writer()
        self._reader_thread = run_in_background(self._reader_loop, daemon=True, name="acp-reader", dedicated=True)

    def _wake_pending(self, message: str) -> None:
        """Fail in-flight ``send_request`` calls that have no response yet.

        ``_running`` is cleared under the same lock that registers a waiter,
        so a request that arrives after this sweep fails immediately instead
        of waiting out its timeout. A response already stored is kept.
        """
        with self._lock:
            self._running = False
            self._fail_waiters(message, only_unanswered=False)

    def _complete_null_id(self, msg: dict[str, Any]) -> None:
        """Deliver a JSON-RPC error whose id is null.

        A parse error cannot echo the request id, so the id is null. That
        used to take the notification path (``callback("", {}, None)``) and
        leave ``session/prompt`` waiting out its 600s timeout. ACP has one
        request in flight. If several are waiting, each one gets this error.
        """
        error = msg.get("error")
        if error is None:
            log.debug("ACP response with null id and no error")
            return
        text = error.get("message", str(error)) if isinstance(error, dict) else str(error)
        with self._lock:
            waiting = self._fail_waiters(text, only_unanswered=True)
        if not waiting:
            log.debug("ACP null-id error with no waiter: %s", text)
            return
        if waiting > 1:
            log.warning("ACP null-id error applied to %s in-flight requests", waiting)

    def _fail_waiters(self, message: str, *, only_unanswered: bool) -> int:
        """Store ``message`` on waiters that have no response. Caller holds ``_lock``.

        ``_wake_pending`` also sets the event on a waiter that already has a
        response, so a stop cannot leave that caller blocked. A null-id error
        leaves those waiters alone.
        """
        failed = 0
        for entry in self._pending.values():
            if entry.response is None:
                entry.response = {"error": {"message": message}}
                entry.event.set()
                failed += 1
            elif not only_unanswered:
                entry.event.set()
        return failed

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
        # when the pipe buffer is full. One writer thread owns the pipe.
        # Queued lines, including session/cancel, run before this stop item
        # closes stdin and kills the tree. The bytes are written if the pipe
        # accepts them. This does not wait for the agent to read them.
        self._enqueue(_QueuedStop(proc))

        def _watchdog_terminate() -> None:
            # What was wrong: sleep(0.5) then poll() always waited half a
            # second, and killed again after the writer had already reaped
            # the child. Why: wait returns as soon as the child is gone.
            # Kill only when that wait times out, which is a writer blocked
            # in a full-pipe write that has not reached its own wait.
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    _kill_acp_tree(proc)
                except Exception:
                    log.debug("ACP stop watchdog kill failed", exc_info=True)
            except Exception:
                log.debug("ACP stop watchdog wait failed", exc_info=True)

        run_in_background(_watchdog_terminate, name="acp-stop-watchdog", dedicated=True)

    def _ensure_writer(self) -> None:
        """Start the stdin thread once. Tests that skip ``start()`` enqueue first."""
        with self._lock:
            if self._writer_thread is not None:
                return
            self._writer_thread = run_in_background(self._writer_drain_loop, name="acp-writer", dedicated=True)

    def _enqueue(self, item: _QueuedWrite | _QueuedStop) -> None:
        self._ensure_writer()
        self._write_queue.put(item)

    def _fail_request(self, req_id: int, message: str) -> None:
        """Unblock one ``send_request`` after its stdin write failed."""
        with self._lock:
            entry = self._pending.get(req_id)
            if entry is not None and entry.response is None:
                entry.response = {"error": {"message": message}}
                entry.event.set()

    @background
    def _writer_drain_loop(self) -> None:
        try:
            while True:
                item = self._write_queue.get()
                if isinstance(item, _QueuedStop):
                    proc = item.proc
                    try:
                        if proc and proc.stdin:
                            proc.stdin.close()
                    except Exception:
                        pass
                    # What was wrong: terminate()/kill() hit only this process, and
                    # closing stdout was supposed to unblock readline. close()
                    # takes the buffer lock readline already holds, so both
                    # threads wait forever when a grandchild still has the pipe.
                    # Why: kill the process group. The child's death closes the
                    # write end, and readline returns EOF.
                    if proc is not None:
                        _kill_acp_tree(proc)
                        try:
                            proc.wait(timeout=3)
                        except Exception:
                            pass
                    break
                try:
                    stdin = item.proc.stdin if item.proc is not None else None
                    if stdin and item.data is not None:
                        stdin.write(item.data)
                        stdin.flush()
                except Exception as exc:
                    # A closed pipe during stop is expected. Debug keeps a
                    # failed session/cancel visible without an error traceback.
                    # A request write must still unblock send_request. ValueError
                    # from a closed pipe used to escape the caller's handler.
                    log.debug("ACP stdin write failed", exc_info=True)
                    if item.fail_req_id is not None:
                        self._fail_request(item.fail_req_id, f"Failed to write to ACP: {exc}")
        finally:
            with self._lock:
                self._writer_thread = None

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

    def send_request(self, method: str, params: Any = None, timeout: float = 120) -> Any:
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
            self._pending[req_id] = _Pending(event)

        line = json.dumps(msg) + "\n"
        log.debug(f"→ {method} (id={req_id})")
        # The caller used to write stdin itself. A full pipe blocked this
        # thread before the timeout started, and a second writer could run
        # beside the queue. The writer thread performs the write. A closed
        # pipe fails this waiter instead of raising ValueError here.
        self._enqueue(_QueuedWrite(proc, line.encode("utf-8"), req_id))

        if not self._wait_idle(event, timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            raise TimeoutError(f"ACP request {method} timed out after {timeout}s")

        with self._lock:
            entry = self._pending.pop(req_id, None)

        resp = entry.response if entry is not None else None
        if resp and "error" in resp:
            err = resp["error"]
            msg_str = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise ToolExecutionError(f"ACP error: {msg_str}")

        return resp.get("result") if resp else None

    def _wait_idle(self, event: threading.Event, timeout: float) -> bool:
        """True when ``event`` is set before ``timeout`` seconds of silence.

        What was wrong: one ``event.wait(timeout)`` was a wall clock. A
        ``session/prompt`` that was still streaming died at 600s. Why: each
        accepted stdout line stores ``_last_inbound``. The wait is sliced so
        that reset is visible before the idle budget runs out.
        """
        self._last_inbound = time.monotonic()
        while True:
            if event.is_set():
                return True
            idle_for = time.monotonic() - self._last_inbound
            if idle_for >= timeout:
                return False
            remaining = timeout - idle_for
            if event.wait(timeout=min(_IDLE_POLL_S, remaining)):
                return True

    def send_notification(self, method: str, params: Any = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        msg = {"jsonrpc": _JSONRPC_VERSION, "method": method, "params": params or {}}
        line = json.dumps(msg) + "\n"
        self._enqueue(_QueuedWrite(proc, line.encode("utf-8")))

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
        self._enqueue(_QueuedWrite(proc, line.encode("utf-8")))

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
                    # Popen is binary. `raw` stays bytes; `line` is the decoded text
                    # `_accept_message` parses. Reusing one name left mypy on bytes.
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    msg = _accept_message(line)
                    if msg is None:
                        continue
                    # Silence is what times out a prompt. Any accepted line,
                    # including session/update, resets that clock.
                    self._last_inbound = time.monotonic()

                    if "id" in msg and msg["id"] is None and "method" not in msg:
                        self._complete_null_id(msg)
                        continue

                    if "id" in msg and msg["id"] is not None and "method" not in msg:
                        # Response to our request. Assign under the same lock
                        # _wake_pending uses, so a stop sweep cannot observe a
                        # missing response and overwrite this one.
                        req_id = msg["id"]
                        with self._lock:
                            entry = self._pending.get(req_id)
                            if entry:
                                entry.response = msg
                                entry.event.set()
                            else:
                                log.warning("Response for unknown id=%s", req_id)
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
