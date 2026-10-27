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

"""One persistent child process: spawn, handshake, framed request, and reap.

Formula and vision use this supervisor. Kokoro's warm worker is
``ManagedSubprocess`` inside ``kokoro_pool._KokoroWorker``, not this class.
The pool that leases several slots stays in ``compute_service.worker_base``.
This module does not import ``compute_service``: that package is not part of
the WriterAgent OXT.

The slot holds at most one :class:`Child`. Adopt is one pointer swap.
Another thread may only signal that child. The thread inside the ready
read or the request closes stdin and stdout, then waits. A child still
alive after one 1s wait is handed to the five-pass reaper in
``managed_subprocess`` and the slot can spawn again. The signal is
``kill_tree`` (process group), not ``Popen.kill``. Stderr is still a pipe.
The drain is started by ``ManagedSubprocess.start``.

``tasks_executed`` counts a response dict that was consumed. A timeout, a
crash, or a spawn failure does not. A ready handshake resets the count.

``Deadline.left`` is the true remainder and may be negative. ``usable``
is the gate before a write, and before a pool lifts a short remainder.
A dead slot also refuses to spawn once less than
``_MIN_SPAWN_REMAINING_SEC`` is left. The 0.01s floor that keeps
``select`` off zero lives in ``plugin.scripting.ipc``.
"""

from __future__ import annotations

import logging
import pickle
import subprocess
import sys
import threading
import time
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.scripting.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    IpcFrameError,
    IpcPartialFrameTimeout,
    pack_pickle_frame,
    read_pickle_frame,
    write_packed_frame_with_timeout,
)

__all__ = ["BaseProcessWorker", "Child", "Deadline", "MIN_REQUEST_SEC", "error_dict"]

# Same logger the compute tests capture. The class moved; the log name did not.
log = logging.getLogger("compute_service.worker")

_SPAWN_READY_TIMEOUT_SEC = 15.0
_STDERR_SNIPPET = 500
# One child writes stderr for its whole life. The drain keeps this many
# characters. The snippet attached to an error is the last _STDERR_SNIPPET.
_STDERR_LOG_CAP = 64 * 1024
# A write, and a pool's decision to lift, is refused below one second.
# The 0.01s select floor lives in plugin.scripting.ipc, and only for a
# remainder that is already positive. formula_pool and vision import this.
MIN_REQUEST_SEC = 1.0
# usable() still lets a one-second budget through with a short remainder
# so those pools can lift it. A dead slot will not Popen below this:
# the handshake would be given the leftover and the child killed.
_MIN_SPAWN_REMAINING_SEC = 0.25

# Distinct from id=None, which is stored. A missing req_id omits the key.
_OMIT_ID = object()


def error_dict(code: str, error: str, *, req_id: Any = _OMIT_ID, message: str | None = None) -> dict[str, Any]:
    """Build ``{"status": "error", "code", "error"}``.

    ``id`` is set when the caller passes ``req_id``, including ``None``.
    ``message`` is set only when passed. Pool execute methods pass ``req_id``.
    The worker's ``_fail_request`` passes ``message`` for the two codes
    callers already read from both keys.
    """
    res: dict[str, Any] = {"status": "error", "code": code, "error": error}
    if req_id is not _OMIT_ID:
        res["id"] = req_id
    if message is not None:
        res["message"] = message
    return res


# These codes copy the text onto ``message`` as well as ``error``.
# ``kill`` stays a call-site choice. Nothing here increments
# ``tasks_executed``: only a consumed response does, and a ready
# handshake resets that count.
_MIRROR_MESSAGE = frozenset({"EXECUTION_TIMEOUT", "WORKER_CRASHED"})

# A killing failure while the slot is stopping. _fail_request reports
# SERVICE_SHUTDOWN instead, and does not kill again. A spent clock passes
# kill=False and stays EXECUTION_TIMEOUT. HTTP 500 or 200 here would hide a stop.
_SHUTDOWN_INSTEAD = frozenset({"WORKER_PIPE_BROKEN", "WORKER_CRASHED", "WORKER_IPC_ERROR", "EMPTY_RESPONSE", "EXECUTION_TIMEOUT"})


class Deadline:
    """Accept-time budget for a pool, or the worker clock started under its lock.

    ``__init__`` starts at now unless ``end`` is passed. ``execute`` builds
    that under ``self.lock`` so the wait for the lock is not part of the
    budget. ``from_absolute`` keeps the original requested seconds and an
    end the HTTP handler already chose. Formula and vision use that one.

    ``left()`` is the true remainder and may be negative. ``usable()``
    is the gate before a write, and before a pool lifts a short remainder:
    None when that work must not start, otherwise ``left()``. A one-second
    request still passes after the clock has moved. A longer request with
    under one second left does not. Spawning a dead slot is tighter: see
    ``_spawn_budget``. The 0.01s floor that keeps ``select`` off zero
    is applied in ``plugin.scripting.ipc`` to a remainder that is already
    positive.
    """

    __slots__: tuple[str, ...] = ("budget_sec", "_end")
    budget_sec: float
    _end: float

    def __init__(self, timeout_sec: float, *, end: float | None = None) -> None:
        """Start a clock of *timeout_sec* seconds, or one that already ends at *end*.

        *end* is keyword-only so ``Deadline(5)`` still starts at now.
        The budget stays *timeout_sec*, not ``end - now``.
        """
        self.budget_sec = float(timeout_sec)
        self._end = float(end) if end is not None else time.monotonic() + self.budget_sec

    @classmethod
    def from_absolute(cls, requested_sec: float, end: float) -> Deadline:
        """Clock whose end is already fixed and whose budget is the original request.

        ``requested_sec`` is not ``end - now``. A one-second request whose
        clock has already moved still starts; rebuilding the budget from
        the time left would refuse it.
        """
        return cls(requested_sec, end=end)

    def left(self) -> float:
        """Seconds until ``_end``. Negative once the clock has passed it.

        No floor and no one-second lift. Callers that must not start pass
        this through ``usable()``. ``select`` lifts a positive remainder
        off zero itself.
        """
        return self._end - time.monotonic()

    def usable(self) -> float | None:
        """Seconds left to start a spawn or a write, or None when it must not.

        A one-second request may start: it is the minimum budget, and the
        clock has already moved. A longer request with under one second
        left must not. A spent clock must not. The returned number is
        ``left()``.
        """
        remaining = self.left()
        if remaining <= 0 or self.budget_sec < MIN_REQUEST_SEC:
            return None
        # A one-second budget is the minimum and may start with less than
        # that left. A longer budget with under one second left must not.
        # Spawning is a separate check: a few milliseconds is not a handshake.
        if self.budget_sec > MIN_REQUEST_SEC and remaining < MIN_REQUEST_SEC:
            return None
        return remaining


def _spawn_budget(deadline: Deadline) -> float | None:
    """Seconds for a new handshake, or None when a child must not start.

    ``usable()`` still returns a short remainder for a one-second budget
    so formula and vision can lift it to ``MIN_REQUEST_SEC``. Starting a
    process on that remainder does not.

    What was wrong: a one-second clock with about 20ms left passed
    ``usable()``, ``Popen`` ran, and the ready read was given those 20ms.
    The child was then killed. The lift belongs in the pool, before
    ``execute`` builds a fresh clock. This floor is only the spawn.
    """
    remaining = deadline.usable()
    if remaining is None or remaining < _MIN_SPAWN_REMAINING_SEC:
        return None
    return remaining


class Child:
    """One spawned process: pipes, stderr tail, and a single reap.

    The slot publishes this object or not at all. A spawn that loses the
    adopt closes its own instance. ``signal_from`` is safe from any thread:
    it only sends SIGKILL. Stdin and stdout close on the thread that was
    inside the read or write, or on the caller when nobody is. Stderr stays
    open for the drain thread, which closes it at EOF.

    ``_signaled`` and ``_io_owner`` are plain attributes. The handshake is
    correct while those reads and writes stay sequentially consistent,
    which CPython's GIL provides. A free-threaded build would need a lock.
    """

    managed: ManagedSubprocess
    worker_name: str
    worker_id: int
    _io_owner: int | None
    _signaled: bool
    _settled: bool
    _abandoned: bool
    _once: threading.Lock

    def __init__(self, managed: ManagedSubprocess, *, worker_name: str, worker_id: int) -> None:
        self.managed = managed
        self.worker_name = worker_name
        self.worker_id = worker_id
        self._io_owner = None
        self._signaled = False
        self._settled = False
        self._abandoned = False
        self._once = threading.Lock()

    def begin_io(self) -> None:
        """Mark this thread as the one inside a ready read or a request."""
        self._io_owner = threading.get_ident()

    def end_io(self) -> bool:
        """Drop the owner mark. True when the slot should drop this child.

        The mark is cleared before the signaled flag is read. A kill that
        lands in that gap sees no owner and reaps here. A kill that already
        saw the owner returns without waiting, and this call reaps.
        """
        if self._io_owner != threading.get_ident():
            return self._settled
        self._io_owner = None
        if not self._signaled:
            return False
        return self._finish()

    def signal_from(self) -> bool:
        """SIGKILL. Close and wait only when this thread owns the pipes, or nobody does.

        True when the slot should drop this child (reaped, or abandoned to
        the module reaper). False when another thread is inside the read or
        write: that thread's ``end_io`` does the close and the wait.
        """
        self._signaled = True
        self.managed.kill_tree()
        me = threading.get_ident()
        # Two loads of _io_owner. The flag is already set, and end_io reads
        # it after clearing the mark, so the owner still finishes if we
        # return on the first look. The second load is a new read: the
        # owner may have cleared between them, and this thread then reaps.
        # _once keeps the two _finish calls to one.
        owner = self._io_owner
        if owner not in (None, me):
            owner = self._io_owner
            if owner not in (None, me):
                return False
        return self._finish()

    def diagnostic_tail(self) -> str:
        """Last stderr characters. Join the drain only after a finished reap.

        An abandoned child is still holding the pipe. Joining that drain
        would wait out the timeout for text that cannot arrive yet.
        """
        if self.managed.stderr_tail is None:
            return ""
        if self._abandoned or not self._settled:
            raw = self.managed.stderr_text()
        else:
            raw = self.managed.join_stderr(1.0)
        text = raw.strip()
        if not text:
            return ""
        return text[-_STDERR_SNIPPET:]

    def _finish(self) -> bool:
        """Close stdio and wait once. True when the slot should drop this child.

        What was wrong: kill() on another thread closed stdin and stdout
        while this thread was between select and read. The next call raised
        ValueError or EBADF, and a reused fd number could be read as the
        response. The non-owner now only signals. This method runs after
        the owner has left, or when there is no owner.
        """
        with self._once:
            if self._settled:
                return True
            me = threading.get_ident()
            if self._io_owner not in (None, me):
                return False
            self.managed.close_stdio()
            reaped = self.managed.wait_once()
            self._settled = True
            if reaped:
                self.managed.join_stderr(1.0)
                self.managed.unregister()
                return True
            self._abandoned = True
            pid = getattr(self.managed.popen, "pid", None)
            log.warning("%s #%s pid=%s still alive after SIGKILL; abandoning", self.worker_name, self.worker_id, pid)
            self.managed.abandon()
            return True


class BaseProcessWorker:
    """One slot. ``child`` is the live process, or None when the slot is empty.

    A child that survives ``wait(1s)`` is not kept here. The module reaper
    owns it, and the next ``respawn`` may start another process. Formula
    sessions store the ``Child`` object. A replaced child is a lost kernel
    even if that process has not exited yet.
    """

    worker_id: int
    script_path: str
    worker_name: str
    executable: str
    max_payload_bytes: int
    lock: threading.Lock
    _lifecycle_lock: threading.Lock
    tasks_executed: int
    child: Child | None
    _shutting_down: bool
    pending_exit: bool
    _child_env: dict[str, str] | None
    _ready_timeout_sec: float
    _unpacker: Callable[[bytes], Any] | None

    def __init__(self, worker_id: int, script_path: str, worker_name: str = "Worker", *, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES, executable: str | None = None, env: dict[str, str] | None = None, ready_timeout_sec: float | None = None, unpacker: Callable[[bytes], Any] | None = None, start: bool = True) -> None:
        self.worker_id = worker_id
        self.script_path = script_path
        self.worker_name = worker_name
        self.max_payload_bytes = max_payload_bytes
        self.executable = sys.executable if executable is None else executable
        self._child_env = env
        # None copies the module constant here, in __init__. Patching it after
        # this returns does not change the handshake.
        self._ready_timeout_sec = _SPAWN_READY_TIMEOUT_SEC if ready_timeout_sec is None else ready_timeout_sec
        # None is stock pickle.
        self._unpacker = unpacker
        self.child = None
        self.lock = threading.Lock()
        # Serializes adopt and drop. Distinct from ``lock``, which execute
        # holds across spawn, so reap must not take ``lock``. Do not take
        # this from execute: a caller that already holds the pool condition
        # would deadlock with a drop that waited on that condition.
        self._lifecycle_lock = threading.Lock()
        self.tasks_executed = 0
        # shutdown calls request_shutdown() before kill(). execute and respawn
        # read the flag without self.lock: kill takes _lifecycle_lock, not the
        # execute lock, so a flag under self.lock would stay invisible until
        # the cell finished. The write is under _lifecycle_lock so adopt either
        # sees shutdown or publishes a child that kill() still signals.
        # CPython's GIL publishes this bool. A free-threaded build would need
        # the read under _lifecycle_lock. The pool may hold its condition
        # while it reads the flag, and adopt holds this lock, so the read
        # stays unlocked.
        self._shutting_down = False
        self.pending_exit = False
        # start=True spawns in __init__.
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
            ManagedSubprocess.wrap_existing(proc, label=f"{self.worker_name} #{self.worker_id}"),
            worker_name=self.worker_name,
            worker_id=self.worker_id,
        )

    def _drain_name(self) -> str:
        """Thread name so a log line shows which pool owns the drain.

        What was wrong: every child used a generic compute prefix, including
        Kokoro and vision. A stuck drain could not be told apart from a
        formula worker.
        """
        parts = "".join(ch if ch.isalnum() else "-" for ch in self.worker_name.lower()).split("-")
        cleaned = "-".join(part for part in parts if part)
        if not cleaned:
            cleaned = "worker"
        return f"wa-{cleaned}-w{self.worker_id}-stderr"

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

    def _drop_child(self, child: Child) -> None:
        """Clear the slot when it still points at *child*."""
        with self._lifecycle_lock:
            if self.child is child:
                self.child = None

    def _end_child_io(self, child: Child) -> None:
        """Leave the pipe section and drop the child when that reap finished."""
        if child.end_io():
            self._drop_child(child)

    def _adopt(self, child: Child) -> bool:
        """Publish *child*, or refuse.

        Shutdown and a second live child are the same refusal. ``execute``
        holds ``self.lock`` across ``respawn``, but ``__init__(start=True)``
        and Kokoro call ``respawn`` without that lock, and the lock is not
        re-entrant, so ``respawn`` cannot take it. Two calls can both pass
        the previous reap and both ``Popen``. The loser closes its own
        ``Child`` and does not touch the one already published.

        What was wrong: the second adopt assigned over a child the first
        call had already published. That Popen was dropped with no kill.
        """
        with self._lifecycle_lock:
            if self._shutting_down or self.child is not None:
                return False
            self.child = child
            return True

    def respawn(self, timeout_sec: float | None = None, *, deadline: Deadline | None = None) -> None:
        """Spawn worker subprocess and await readiness handshake.

        Returns without a new child when the pool is stopping, when another
        thread is still inside the previous child's pipes, or when *deadline*
        is already too late to start one. A previous child that will not die
        is abandoned. The slot is then empty and this call may ``Popen``.
        Recycle only ``kill()``s and leaves the slot dead. The next lease's
        ``execute`` is what calls this. ``_shutting_down`` stops that spawn.

        *deadline* is the caller's request clock. The reap wait is inside that
        clock: budgeting the handshake from the time left before the reap let
        a call run about a second past its timeout. ``None`` is startup, which
        has no request clock. Pass *timeout_sec* or *deadline*, not both: the
        handshake budget is ``_spawn_budget`` after ``Popen``, so an explicit
        *timeout_sec* would be discarded.
        """
        # What was wrong: both arguments were accepted and deadline silently
        # replaced timeout_sec. A caller that set a handshake cap next to a
        # request clock thought the cap applied.
        if deadline is not None and timeout_sec is not None:
            raise ValueError("respawn accepts timeout_sec or deadline, not both")
        if timeout_sec is None:
            timeout_sec = self._ready_timeout_sec
        if self._shutting_down:
            return
        if not self.kill():
            return
        if self._shutting_down or self.child is not None:
            return
        # What was wrong: spawn_budget was fixed before this wait, and
        # _REAP_WAIT_SEC can block for a second. A longer request with under
        # a second left still started a child. The handshake timeout is
        # computed after Popen, which can itself spend the clock.
        # A one-second budget with a few milliseconds left also used to
        # pass usable() and spawn. _spawn_budget refuses that.
        if deadline is not None and _spawn_budget(deadline) is None:
            return
        managed = ManagedSubprocess(
            [self.executable, self.script_path],
            base_env=None if self._child_env is None else self._child_env,
            scrub_env=self._child_env is None,
            optimize_pipes=True,
            stderr_tail_chars=_STDERR_LOG_CAP,
            stderr_name=self._drain_name(),
            label=f"{self.worker_name} #{self.worker_id}",
        )
        spawned: Child | None = None
        published = False
        try:
            managed.start()
            spawned = Child(managed, worker_name=self.worker_name, worker_id=self.worker_id)
            # Do not hold _lifecycle_lock across start() or the handshake read.
            # kill() takes that lock only to drop the pointer, and the
            # handshake can block for the spawn budget.
            if not self._adopt(spawned):
                spawned.signal_from()
                return
            published = True
            proc = managed.popen
            # Popen itself is not on the clock. If it ran past the deadline,
            # or left less than the spawn floor, kill the child here. A
            # handshake read of a spent clock used to floor at 0.01s and
            # then SIGKILL.
            if deadline is not None:
                spawn_for = _spawn_budget(deadline)
                if spawn_for is None:
                    self.kill()
                    return
                timeout_sec = min(self._ready_timeout_sec, spawn_for)
            # The ready read is the pipe owner. Stop can signal during it.
            # end_io closes after the read returns, including on timeout.
            spawned.begin_io()
            try:
                ready_data: Any = None
                if proc is not None and proc.stdout is not None:
                    ready_data = read_pickle_frame(
                        proc.stdout,
                        timeout_sec,
                        is_alive=self.is_alive,
                        max_payload_bytes=self.max_payload_bytes,
                        require_dict=True,
                        unpacker=self._unpacker,
                    )
                # Only status "ready" counts. EOF or any other dict would mark the
                # worker idle, and the next execute would wait out the full
                # timeout before EMPTY_RESPONSE.
                if not isinstance(ready_data, dict) or ready_data.get("status") != "ready":
                    status = ready_data.get("status") if isinstance(ready_data, dict) else None
                    self.kill()
                    self._log_spawn_outcome(
                        f"{self.worker_name} #{self.worker_id} spawn handshake was not ready (status={status!r})",
                        spawned,
                    )
                    return
                log.info(
                    "%s #%d spawned (pid=%s, status=%s)",
                    self.worker_name,
                    self.worker_id,
                    ready_data.get("pid", getattr(proc, "pid", None)),
                    ready_data.get("status"),
                )
                self.tasks_executed = 0
                self.pending_exit = False
            except (subprocess.TimeoutExpired, IpcPartialFrameTimeout):
                # No-byte hang is TimeoutExpired. A deadline after the first
                # handshake byte is IpcPartialFrameTimeout (a ConnectionError).
                # That used to be logged as "Failed to spawn". The child is
                # killed either way; the next lease respawns the slot.
                rc = proc.poll() if proc is not None else None
                self.kill()
                self._log_spawn_outcome(
                    f"{self.worker_name} #{self.worker_id} spawn handshake timed out (returncode={rc})",
                    spawned,
                )
            finally:
                self._end_child_io(spawned)
        except Exception as exc:
            message = f"Failed to spawn {self.worker_name} #{self.worker_id}: {exc}"
            # What was wrong: this kill() reaped self.child. A second respawn
            # can raise before adopt while the first call's child is already
            # published, and that child was healthy. Only the Child this call
            # constructed is signaled on that path.
            if not published:
                if self._shutting_down:
                    log.info("%s", message)
                else:
                    log.error("%s", message)
                if spawned is not None:
                    spawned.signal_from()
            else:
                self.kill()
                self._log_spawn_outcome(message, spawned)

    def is_alive(self) -> bool:
        """True only while the current child's ``poll()`` is None.

        False means there is no child, or ``poll()`` has already reaped it.
        A child handed to the module reaper is not current, so this is False
        and the slot can be spawned again.
        """
        # kill() assigns None under _lifecycle_lock while execute's select
        # loop calls this. A second read of the child could be None and
        # raise AttributeError, which the write handler reported as
        # REQUEST_NOT_SERIALIZABLE. Snapshot once before poll().
        child = self.child
        if child is None:
            return False
        try:
            return child.managed.is_alive()
        except Exception:
            log.debug("%s #%s poll failed", self.worker_name, self.worker_id, exc_info=True)
            return False

    def is_shutting_down(self) -> bool:
        """Whether ``request_shutdown`` has published that this slot must not start work.

        Read unlocked. Adopt holds ``_lifecycle_lock``, and the pool may
        hold its condition while it calls this. CPython's GIL publishes the bool.
        """
        return self._shutting_down

    def request_shutdown(self) -> None:
        """Publish shutdown so execute and respawn will not start a child.

        The flag shares ``_lifecycle_lock`` with process publish. Adopt then
        either sees the flag or publishes a child ``kill`` still signals.
        """
        with self._lifecycle_lock:
            self._shutting_down = True

    def kill(self) -> bool:
        """Signal the current child. Wait and close when this thread may.

        True when the slot is empty afterwards: the child was reaped, or it
        was abandoned to the module reaper. False when another thread is
        inside the pipes. That thread's ``end_io`` drops the child.
        A test stand-in whose ``kill`` returns None counts as finished.
        """
        child = self.child
        if child is None:
            return True
        if not child.signal_from():
            return False
        self._drop_child(child)
        return self.child is not child

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
        # What was wrong: a read ValueError, ConnectionResetError, or
        # TimeoutExpired while the pool was stopping was WORKER_IPC_ERROR,
        # WORKER_CRASHED, or EXECUTION_TIMEOUT. Windows raises
        # TimeoutExpired from is_alive before the read sees EOF. The
        # child is already being reaped, so this must not kill again.
        # kill=False stays put: the clock was spent before a frame was sent.
        if kill and self._shutting_down and code in _SHUTDOWN_INSTEAD:
            return self._shutdown_error()
        # What was wrong: kill=False still read the stderr tail. That is
        # shutdown, a clock spent before the frame was sent, or a spawn
        # that already logged its own tail. The read could attach a
        # previous child's text to an unrelated error. The tail belongs
        # on the failure that is about to kill the child that wrote it.
        # The drain is joined by kill() when the reap finishes, so the
        # snippet is taken after that.
        child = self.child
        if kill:
            self.kill()
            snippet = child.diagnostic_tail() if child is not None else ""
            if snippet:
                msg = f"{msg}\n{snippet}"
            log.warning("%s request failed (%s) on worker #%d; terminating pid=%s", self.worker_name, code, self.worker_id, pid)
        message = msg if code in _MIRROR_MESSAGE else None
        return error_dict(code, msg, message=message)

    def _shutdown_error(self) -> dict[str, Any]:
        """The slot is stopping. Do not kill, and do not read stderr.

        The shutdown thread has already called ``kill``, or is about to.
        A crash code here was HTTP 500 for a stop the client can retry.
        """
        return error_dict("SERVICE_SHUTDOWN", f"{self.worker_name} #{self.worker_id} is shutting down.")

    def _timeout_message(self, deadline: Deadline) -> str:
        """Error text for a spent budget. Sub-second budgets are refused, not reported as 0.

        ``int`` of a 1.9s budget used to report "1 seconds". A whole 1s
        budget used to say "1 seconds" as well.
        """
        seconds = deadline.budget_sec
        if seconds < 1:
            return "Execution timeout must be at least 1 second."
        whole = int(seconds)
        shown: int | float = whole if seconds == whole else seconds
        unit = "second" if shown == 1 else "seconds"
        return f"Execution exceeded maximum timeout of {shown} {unit}."

    def _fail_timeout(self, deadline: Deadline, pid: int | None, *, kill: bool = True) -> dict[str, Any]:
        """``EXECUTION_TIMEOUT`` for *deadline*. ``kill`` is false when no child should die."""
        return self._fail_request("EXECUTION_TIMEOUT", self._timeout_message(deadline), pid=pid, kill=kill)

    def _fail_if_unusable(self, deadline: Deadline, proc: Any) -> dict[str, Any] | None:
        """``EXECUTION_TIMEOUT`` when a spawn or a write must not start, else None.

        Does not kill. The frame has not been sent. The check after a
        successful write is separate: that one kills, because the child
        already has the frame.
        """
        if deadline.usable() is not None:
            return None
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
            if _spawn_budget(deadline) is None:
                return self._fail_timeout(deadline, pid, kill=False)
            return None

        if proc is None or proc.poll() is not None:
            refused = _refused_dead_slot()
            if refused is not None:
                return refused
            # respawn recomputes the handshake timeout from _spawn_budget
            # after Popen. A budget taken before the reap wait can disagree
            # with the time left, and the argument is unused when deadline is set.
            self.respawn(deadline=deadline)
            proc = self.process
            if proc is None or proc.poll() is not None:
                refused = _refused_dead_slot()
                if refused is not None:
                    return refused
                return self._fail_request("WORKER_SPAWN_FAILED", f"{self.worker_name} #{self.worker_id} could not be started.", pid=proc.pid if proc is not None else None, kill=False)

        stdin = proc.stdin
        stdout = proc.stdout
        if stdin is None or stdout is None:
            return self._fail_request("WORKER_SPAWN_FAILED", f"{self.worker_name} #{self.worker_id} has no stdio pipes.", pid=proc.pid, kill=False)
        return proc, stdin, stdout

    def execute(self, payload: dict[str, Any], timeout_sec: float, *, req_id: Any = None) -> dict[str, Any]:
        """Send request to worker process and await response with timeout.

        ``req_id`` is copied onto the returned dict when it is not None.
        Pools used to patch ``id`` afterward, so an error frame could leave
        without it. ``None`` still omits the key.
        """
        res = self._execute(payload, timeout_sec)
        if req_id is not None and isinstance(res, dict):
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
            spent = self._fail_if_unusable(deadline, self.process)
            if spent is not None:
                return spent
            try:
                # Pack before spawn and before any pipe write. An oversized or
                # unpicklable payload used to start a child and then return.
                # IpcFrameError and pickle errors mean no byte was written.
                # What was wrong: ValueError and TypeError were caught around
                # the whole write. A pipe that raised them was reported as
                # REQUEST_NOT_SERIALIZABLE and left alive on a desynced frame.
                frame = pack_pickle_frame(payload, max_payload_bytes=self.max_payload_bytes)
            except IpcFrameError as exc:
                # Raised before any byte is written. No child was started for
                # this payload. An already-live child stays: killing it would
                # drop every shared session.
                return error_dict("PAYLOAD_TOO_LARGE", str(exc))
            except (pickle.PicklingError, TypeError, ValueError, RecursionError) as exc:
                # pickle.dumps used to escape execute. ValueError and
                # RecursionError did too, and the HTTP handler turned them
                # into an unhandled 500. The child is still frame-aligned, so
                # it stays. AttributeError is not included: a shutdown race on
                # self.process used to land here and skip the kill.
                # MemoryError is not included: the process may be out of memory.
                return error_dict("REQUEST_NOT_SERIALIZABLE", str(exc))
            spent = self._fail_if_unusable(deadline, self.process)
            if spent is not None:
                # Packing spent the clock. Do not spawn, and do not kill a
                # child that was sent nothing.
                return spent
            ensured = self._ensure_live_process(deadline)
            if isinstance(ensured, dict):
                return ensured
            proc, stdin, stdout = ensured
            # proc is live here. The nullable pid helper stays in
            # _ensure_live_process, where the slot may still be empty.
            pid = proc.pid
            # Spawn or packing spent the clock. Writing the floored 0.01s
            # used to time out and kill a child that was sent nothing.
            # One usable() call: the float is the write budget.
            ready = deadline.usable()
            if ready is None:
                return self._fail_timeout(deadline, pid, kill=False)
            # Owner for the write and the read. Shutdown may only signal
            # until this finally closes. A successful response leaves the
            # pipes open: end_io closes only after a signal.
            child = self.child
            if child is not None and child.managed.popen is proc:
                child.begin_io()
            else:
                child = None
            try:
                try:
                    # The write shares the request deadline. It holds self.lock, so a
                    # child that stopped reading stdin would never return and the
                    # slot would stay leased. A partial frame is desynchronized,
                    # so the child is killed.
                    write_packed_frame_with_timeout(stdin, frame, ready, is_alive=self.is_alive)
                except subprocess.TimeoutExpired:
                    return self._fail_timeout(deadline, pid)
                except (OSError, TypeError, ValueError) as exc:
                    # TypeError and ValueError are no longer pickle failures. A
                    # pipe that raises them may already have written part of the
                    # frame, so the child is killed. During shutdown
                    # _fail_request reports SERVICE_SHUTDOWN and does not kill
                    # again: the child is already being reaped.
                    return self._fail_request("WORKER_PIPE_BROKEN", f"Failed to send request to {self.worker_name} #{self.worker_id}: {exc}", pid=pid)

                remaining = deadline.left()
                if remaining <= 0:
                    # The frame was written. Do not start a read on a spent clock.
                    return self._fail_timeout(deadline, pid)
                try:
                    resp = read_pickle_frame(stdout, remaining, is_alive=self.is_alive, max_payload_bytes=self.max_payload_bytes, unpacker=self._unpacker)
                except subprocess.TimeoutExpired:
                    # The frame never arrived. Kill rather than read it later:
                    # the next caller would consume this response as its own.
                    return self._fail_timeout(deadline, pid)
                except IpcPartialFrameTimeout:
                    # A deadline after the first byte is a timeout, not a crash.
                    # The next read would consume the rest of this frame as a
                    # new response, so the child is killed.
                    return self._fail_timeout(deadline, pid)
                except ValueError as exc:
                    # What was wrong: an invalid or oversized response frame was
                    # WORKER_CRASHED. IpcFrameError is a ValueError. The
                    # restricted unpickler raises ValueError after those bytes
                    # were consumed. The pipe is not on a frame the next request
                    # can read, so the child is killed. During shutdown this is
                    # SERVICE_SHUTDOWN, from _fail_request.
                    return self._fail_request("WORKER_IPC_ERROR", f"{self.worker_name} rejected a response frame: {exc}", pid=pid)
                except Exception as exc:
                    return self._fail_request("WORKER_CRASHED", f"{self.worker_name} error: {exc}", pid=pid)
                if not isinstance(resp, dict):
                    # Shutdown kills the child while this read is in flight. EOF
                    # used to be EMPTY_RESPONSE, which counts a task and looks
                    # like a crash. _fail_request maps that to SERVICE_SHUTDOWN
                    # when the slot is stopping. The child is already being reaped.
                    return self._fail_request("EMPTY_RESPONSE", f"No response returned from {self.worker_name}.", pid=pid)
                if resp.get("exiting") is True:
                    self.pending_exit = True
                    resp.pop("exiting", None)
                self.tasks_executed += 1
                return resp
            finally:
                if child is not None:
                    self._end_child_io(child)
