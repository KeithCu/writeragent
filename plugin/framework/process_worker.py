# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
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

"""Compute pool adapter: ``BaseProcessWorker`` is a :class:`ChildSlot` for pool workers.

Publish, drop, and the spawn handshake live in :mod:`plugin.framework.warm_child`.
This module is the multi-slot compute worker slot (``BaseProcessWorker``) only.
"""

from __future__ import annotations

import functools
import logging
import pickle
import sys
import threading
from typing import IO, TYPE_CHECKING, Any, final

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.framework.child_runtime import Deadline, error_dict
from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.framework.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    IpcFrameError,
    pack_pickle_frame,
)
from plugin.framework.warm_child import (
    Child,
    ChildSlot,
    FailPhase,
    SPAWN_READY_TIMEOUT_SEC,
    STDERR_LOG_CAP,
    framed_request_roundtrip,
    spawn_budget,
    stderr_thread_name,
    supervisor,
)

__all__ = ["BaseProcessWorker"]

# Pool slots log under compute_service.worker, the name the compute service and its tests capture.
log = logging.getLogger("compute_service.worker")


def _compute_slot_fail_message(
    code: str,
    detail: str,
    *,
    worker_name: str,
    worker_id: int,
) -> str:
    """User-facing error text for a compute slot ``framed_request_roundtrip`` failure."""
    if code == "WORKER_PIPE_BROKEN":
        return f"Failed to send request to {worker_name} #{worker_id}: {detail}"
    if code == "WORKER_IPC_ERROR":
        return f"{worker_name} rejected a response frame: {detail}"
    if code == "EMPTY_RESPONSE":
        return f"No response returned from {worker_name}."
    if code == "WORKER_CRASHED":
        return f"{worker_name} error: {detail}"
    raise AssertionError(f"unhandled fail code {code!r}")


# A killing failure while the slot is stopping. _fail_request reports
# SERVICE_SHUTDOWN instead, and does not kill again. A spent clock passes
# kill=False and stays EXECUTION_TIMEOUT. HTTP 500 or 200 here would hide a stop.
_SHUTDOWN_INSTEAD = frozenset({"WORKER_PIPE_BROKEN", "WORKER_CRASHED", "WORKER_IPC_ERROR", "EMPTY_RESPONSE", "EXECUTION_TIMEOUT"})


@final
class BaseProcessWorker(ChildSlot):
    """One slot. ``child`` is the live process, or None when the slot is empty.

    A child that survives ``wait(1s)`` is not kept here. The module reaper
    owns it, and the next ``respawn`` may start another process. Formula
    sessions store the ``Child`` object. A replaced child is a lost kernel
    even if that process has not exited yet.
    """

    def __init__(self, worker_id: int, script_path: str, worker_name: str = "Worker", *, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES, unpacker: Callable[[bytes], Any] | None = None, start: bool = True) -> None:
        super().__init__()
        self.worker_id: int = worker_id
        self.script_path: str = script_path
        self.worker_name: str = worker_name
        self._label: str = f"{worker_name} #{worker_id}"
        self.max_payload_bytes: int = max_payload_bytes
        # None is stock pickle.
        self._unpacker: Callable[[bytes], Any] | None = unpacker
        self.lock: threading.Lock = threading.Lock()
        # ChildSlot._lifecycle_lock serializes publish and drop; distinct from ``lock``,
        # which execute holds across spawn, so reap must not take ``lock``. Do not take
        # _lifecycle_lock from execute: a caller that already holds the pool condition
        # would deadlock with a drop that waited on that condition.
        self.tasks_executed: int = 0
        # shutdown calls request_shutdown() before kill(). execute and respawn
        # read the flag without self.lock: kill takes _lifecycle_lock, not the
        # execute lock, so a flag under self.lock would stay invisible until
        # the cell finished. The write is under _lifecycle_lock so publish either
        # sees shutdown or publishes a child that kill() still signals.
        # CPython's GIL publishes this bool. A free-threaded build would need
        # the read under _lifecycle_lock. The pool may hold its condition
        # while it reads the flag, and publish holds this lock, so the read
        # stays unlocked.
        self._shutting_down: bool = False
        self.pending_exit: bool = False
        if start:
            self.respawn()

    @property
    def process(self) -> Any:
        """The current child's ``Popen``, or None when the slot is empty.

        Tests assign a stand-in here. That installs a :class:`Child` around
        it. Production code publishes ``self.child`` from ``respawn``.
        """
        child = self.child
        return None if child is None else child.managed.popen

    @process.setter
    def process(self, proc: Any) -> None:
        if proc is None:
            self.child = None
            return
        current = self.child
        if current is not None and current.managed.popen is proc:
            return
        self.child = Child(
            ManagedSubprocess.wrap_existing(proc, label=self._label),
            worker_name=self.worker_name,
            worker_id=self.worker_id,
        )

    def _log_spawn_outcome(self, message: str, spawned: Child | None) -> None:
        """Log *message*. A shutdown-interrupted handshake is not a failed spawn.

        Anything else is a failed spawn and includes the child's stderr
        tail, or ``stderr=<empty>`` when the drain has none. Call this
        after ``kill`` so a reaped child's drain has been joined.
        """
        if self._shutting_down:
            log.info("%s", message)
            return
        snippet = spawned.diagnostic_tail() if spawned is not None else ""
        extra = f" stderr={snippet!r}" if snippet else " stderr=<empty>"
        log.error("%s%s", message, extra)

    def _publish_refusal(self) -> dict[str, Any] | None:
        return self._shutdown_error() if self._shutting_down else None

    def respawn(self, *, deadline: Deadline | None = None) -> None:
        """Spawn worker subprocess and await readiness handshake.

        Returns without a new child when the pool is stopping, when another
        thread is still inside the previous child's pipes, or when *deadline*
        is already too late to start one. A previous child that will not die
        is abandoned. The slot is then empty and this call may ``Popen``.
        Recycle only ``kill()``s and leaves the slot dead. The next lease's
        ``execute`` is what calls this. ``_shutting_down`` stops that spawn.

        *deadline* is the caller's request clock. The reap wait is inside that
        clock: budgeting the handshake from the time left before the reap
        would let a call run past its timeout. ``None`` is startup, which
        has no request clock. ``execute`` holds ``self.lock`` across this call;
        ``__init__(start=True)`` calls it without the lock, which is not
        re-entrant, so ``_publish`` refuses a second child.
        """
        if self._shutting_down:
            return
        if not self.kill():
            return
        if self._shutting_down or self.child is not None:
            return
        budget = spawn_budget(deadline) if deadline is not None else None
        if deadline is not None and budget is None:
            return
        managed = ManagedSubprocess(
            [sys.executable, self.script_path],
            base_env=None,
            scrub_env=True,
            optimize_pipes=True,
            stderr_tail_chars=STDERR_LOG_CAP,
            stderr_name=stderr_thread_name(self.worker_name, fallback="worker", worker_id=self.worker_id),
            label=self._label,
        )
        outcome = self._spawn_child(
            managed,
            worker_name=self.worker_name,
            worker_id=self.worker_id,
            # Not the caller thread: an HTTP worker exits at shutdown and
            # PDEATHSIG would kill this child with it.
            start=functools.partial(supervisor.spawn, timeout_sec=budget),
            deadline=deadline,
            cap_sec=SPAWN_READY_TIMEOUT_SEC,
            max_payload_bytes=self.max_payload_bytes,
            unpacker=self._unpacker,
        )
        label = self._label
        if outcome.tag == "ready":
            ready_data = outcome.detail
            popen = managed.popen
            log.info(
                "%s spawned (pid=%s, status=%s)",
                self._label,
                ready_data.get("pid", popen.pid if popen is not None else None),
                ready_data.get("status"),
            )
            self.tasks_executed = 0
            self.pending_exit = False
        elif outcome.tag == "not_ready":
            self._log_spawn_outcome(
                f"{label} spawn handshake was not ready (status={outcome.detail!r})",
                outcome.child,
            )
        elif outcome.tag == "timeout":
            self._log_spawn_outcome(
                f"{label} spawn handshake timed out (returncode={outcome.detail})",
                outcome.child,
            )
        elif outcome.tag == "failed":
            self._log_spawn_outcome(f"Failed to spawn {label}: {outcome.detail}", outcome.child)
        # refused, budget_exhausted, interrupted: the slot already cleaned up and none is a failed spawn.

    def is_shutting_down(self) -> bool:
        """Whether ``request_shutdown`` has published that this slot must not start work.

        Read unlocked. Publish holds ``_lifecycle_lock``, and the pool may
        hold its condition while it calls this. CPython's GIL publishes the bool.
        """
        return self._shutting_down

    def request_shutdown(self) -> None:
        """Publish shutdown so execute and respawn will not start a child.

        The flag shares ``_lifecycle_lock`` with process publish. ``_publish`` then
        either sees the flag or publishes a child ``kill`` still signals.
        """
        with self._lifecycle_lock:
            self._shutting_down = True

    def _fail_request(self, code: str, msg: str, *, pid: int | None, kill: bool = True) -> dict[str, Any]:
        """Build an error dict, then signal the child or leave it.

        ``kill`` is a timeout, a desynchronized pipe, a crash, or an empty
        response. The next lease respawns that slot. False leaves the child:
        shutdown, or a process that never became live. An oversized frame
        never gets here.
        """
        # A finished response is the only increment. A killing failure
        # already reaps this child, and the next handshake sets the count
        # back to 0, so counting the failure cannot retire a dying slot
        # across processes. Counting a timeout that never ran can retire
        # a healthy one. ``kill`` stays a call-site choice.
        # Map worker errors to shutdown error if the pool is stopping.
        if kill and self._shutting_down and code in _SHUTDOWN_INSTEAD:
            return self._shutdown_error()
        # Attach diagnostic tail only when killing the failed child.
        child = self.child
        if kill:
            self.kill()
            snippet = child.diagnostic_tail() if child is not None else ""
            if snippet:
                msg = f"{msg}\n{snippet}"
            log.warning("%s request failed (%s) on worker #%d; terminating pid=%s", self.worker_name, code, self.worker_id, pid)
        return error_dict(code, msg)

    def _shutdown_error(self) -> dict[str, Any]:
        """The slot is stopping. Do not kill, and do not read stderr.

        The shutdown thread has already called ``kill``, or is about to.
        A crash code here would return HTTP 500 for a stop the client can retry.
        """
        return error_dict("SERVICE_SHUTDOWN", f"{self._label} is shutting down.")

    def _timeout_message(self, deadline: Deadline) -> str:
        """Error text for a spent budget. Sub-second budgets are refused, not reported as 0."""
        seconds = deadline.budget_sec
        if seconds < 1:
            return "Execution timeout must be at least 1 second."
        whole = int(seconds)
        shown: int | float = whole if seconds == whole else seconds
        unit = "second" if shown == 1 else "seconds"
        return f"Execution exceeded maximum timeout of {shown} {unit}."

    def _fail_timeout(self, deadline: Deadline, pid: int | None, *, kill: bool) -> dict[str, Any]:
        """``EXECUTION_TIMEOUT`` for *deadline*. ``kill`` is false when no child should die."""
        return self._fail_request("EXECUTION_TIMEOUT", self._timeout_message(deadline), pid=pid, kill=kill)

    def _fail_if_unusable(self, deadline: Deadline) -> dict[str, Any] | None:
        """``EXECUTION_TIMEOUT`` when a spawn or a write must not start, else None.

        Does not kill. The frame has not been sent. The check after a
        successful write is separate: that one kills, because the child
        already has the frame.
        """
        if deadline.usable() is not None:
            return None
        proc = self.process
        pid = proc.pid if proc is not None else None
        return self._fail_timeout(deadline, pid, kill=False)

    def _ensure_live_process(self, deadline: Deadline) -> tuple[Any, IO[bytes], IO[bytes]] | dict[str, Any]:
        """Return a live child and its stdio pipes, or an error dict.

        Caller holds ``self.lock``. ``kill()`` during shutdown sets
        ``self.child`` to None; reading ``.pid`` or ``.stdin`` on that
        would be AttributeError. A dead child during shutdown is
        ``SERVICE_SHUTDOWN``, not a new process. The handshake uses the
        remaining request budget, not a fresh spawn timeout. Under one second
        left on a longer budget, or under ``_MIN_SPAWN_REMAINING_SEC`` on a
        one-second budget, do not spawn: the pipe-wait floor is not a
        handshake budget. Reap and ``Popen`` are inside that same clock. A
        budget that is gone when they return is ``EXECUTION_TIMEOUT``, not
        ``WORKER_SPAWN_FAILED``. A live child is not subject to the spawn floor.
        """
        proc = self.process

        def _refused_dead_slot() -> dict[str, Any] | None:
            """Shutdown or a clock that must not spawn. Neither is a failed spawn.

            ``proc`` is the slot's current child, including the one
            ``respawn`` just stored. A spent clock after reap or ``Popen``
            is ``EXECUTION_TIMEOUT``, not ``WORKER_SPAWN_FAILED``. A
            one-second budget with only a few milliseconds left is the
            same timeout: ``usable()`` would still start a child.
            """
            pid = proc.pid if proc is not None else None
            if self._shutting_down:
                return self._shutdown_error()
            if spawn_budget(deadline) is None:
                return self._fail_timeout(deadline, pid, kill=False)
            return None

        if proc is None or proc.poll() is not None:
            refused = _refused_dead_slot()
            if refused is not None:
                return refused
            # ``_spawn_child`` recomputes the handshake timeout from ``spawn_budget``
            # after ``Popen``, because the reap wait in ``respawn`` spends part of the clock.
            self.respawn(deadline=deadline)
            proc = self.process
            if proc is None or proc.poll() is not None:
                refused = _refused_dead_slot()
                if refused is not None:
                    return refused
                return self._fail_request("WORKER_SPAWN_FAILED", f"{self._label} could not be started.", pid=proc.pid if proc is not None else None, kill=False)

        stdin = proc.stdin
        stdout = proc.stdout
        if stdin is None or stdout is None:
            return self._fail_request("WORKER_SPAWN_FAILED", f"{self._label} has no stdio pipes.", pid=proc.pid, kill=False)
        return proc, stdin, stdout

    def execute(self, payload: dict[str, Any], timeout_sec: float, *, req_id: Any = None) -> dict[str, Any]:
        """Send request to worker process and await response with timeout.

        ``req_id`` is copied onto the returned dict when it is not None.
        """
        res = self._execute(payload, timeout_sec)
        if req_id is not None:
            res["id"] = req_id
        return res

    def _execute(self, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
        """Run one request. ``execute`` stamps ``id`` on the way out."""
        # One deadline covers spawn, the stdin write, and the stdout read.
        # It is created under self.lock so waiting for that lock is not
        # part of the budget. Do not floor timeout_sec before this: a
        # caller that already passed 0 would still spawn.
        with self.lock:
            deadline = Deadline(timeout_sec)
            spent = self._fail_if_unusable(deadline)
            if spent is not None:
                return spent
            try:
                # Pack before spawn and before any pipe write to ensure frame alignment.
                frame = pack_pickle_frame(payload, max_payload_bytes=self.max_payload_bytes)
            except IpcFrameError as exc:
                # Raised before any byte is written. No child was started for
                # this payload. An already-live child stays: killing it would
                # drop every shared session.
                return error_dict("PAYLOAD_TOO_LARGE", str(exc))
            except (pickle.PicklingError, TypeError, ValueError, RecursionError) as exc:
                # Serialization error occurred before any byte was written; child stays alive.
                return error_dict("REQUEST_NOT_SERIALIZABLE", str(exc))
            spent = self._fail_if_unusable(deadline)
            if spent is not None:
                # Packing spent the clock. Do not spawn, and do not kill a
                # child that was sent nothing.
                return spent
            ensured = self._ensure_live_process(deadline)
            if isinstance(ensured, dict):
                return ensured
            proc, stdin, stdout = ensured
            pid = proc.pid
            ready = deadline.usable()
            if ready is None:
                return self._fail_timeout(deadline, pid, kill=False)
            child = self.child
            if child is not None and child.managed.popen is proc:
                child.begin_io()
            else:
                child = None

            def _on_fail(code: str, detail: str, *, phase: FailPhase) -> dict[str, Any]:
                if code == "EXECUTION_TIMEOUT":
                    # before_send: nothing was written. after_send/write/read: the child
                    # already has the frame or is mid-I/O — kill so the slot respawns clean.
                    kill = phase != "before_send"
                    return self._fail_timeout(deadline, pid, kill=kill)
                msg = _compute_slot_fail_message(
                    code,
                    detail,
                    worker_name=self.worker_name,
                    worker_id=self.worker_id,
                )
                return self._fail_request(code, msg, pid=pid)

            try:
                resp = framed_request_roundtrip(
                    stdin,
                    stdout,
                    frame,
                    deadline,
                    is_alive=self.is_alive,
                    max_payload_bytes=self.max_payload_bytes,
                    unpacker=self._unpacker,
                    on_fail=_on_fail,
                )
                if resp.get("status") == "error":
                    return resp
                if resp.get("exiting") is True:
                    self.pending_exit = True
                    resp.pop("exiting", None)
                self.tasks_executed += 1
                return resp
            finally:
                if child is not None:
                    self._end_child_io(child)
