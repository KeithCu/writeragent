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

Compute formula and vision pools and the Kokoro speech worker share this
supervisor. The pool that leases several slots stays in
``compute_service.worker_base``. This module does not import
``compute_service``: that package is not part of the WriterAgent OXT.

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
import os
import pickle
import subprocess
import sys
import tempfile
import threading
import time
from typing import IO, TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.framework.worker_pool import get_subprocess_creationflags
from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES, IpcFrameError, IpcPartialFrameTimeout, pack_pickle_frame, read_pickle_frame_with_timeout, write_packed_frame_with_timeout
from plugin.scripting.sandbox import optimize_popen_pipes, scrub_subprocess_env

__all__ = ["BaseProcessWorker", "Deadline", "MIN_REQUEST_SEC", "error_dict"]

# Same logger the compute tests capture. The class moved; the log name did not.
log = logging.getLogger("compute_service.worker")

_SPAWN_READY_TIMEOUT_SEC = 15.0
_STDERR_SNIPPET = 500
# One child appends for its whole life. The host keeps the tail; the
# snippet reader only uses the last _STDERR_SNIPPET characters.
_STDERR_LOG_CAP = 64 * 1024
# The child is already SIGKILL'd. wait() must not stall shutdown.
_REAP_WAIT_SEC = 1.0
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


class BaseProcessWorker:
    """Wrapper around one persistent child subprocess communicating via Pickle 5 frames.

    ``on_process_exit`` runs from ``_reap_previous_process`` while
    ``_lifecycle_lock`` is held. That lock is not re-entrant. ``kill``,
    ``respawn``, and ``request_shutdown`` raise ``RuntimeError`` when the
    caller is that callback's thread, before taking the lock. The callback
    may take the pool condition. Dropping the lock before the callback is
    not safe: ``respawn`` is the caller of the reap, so a callback that
    then respawned would start a second child.
    """

    worker_id: int
    script_path: str
    worker_name: str
    executable: str
    max_payload_bytes: int
    lock: threading.Lock
    _lifecycle_lock: threading.Lock
    tasks_executed: int
    on_process_exit: Callable[[BaseProcessWorker, int], None] | None
    _shutting_down: bool
    _reap_incomplete: bool
    _spawning: bool
    _reap_thread_id: int | None
    _child_env: dict[str, str] | None
    _ready_timeout_sec: float
    _unpacker: Callable[[bytes], Any] | None

    def __init__(self, worker_id: int, script_path: str, worker_name: str = "Worker", *, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES, on_process_exit: Callable[[BaseProcessWorker, int], None] | None = None, executable: str | None = None, env: dict[str, str] | None = None, ready_timeout_sec: float | None = None, unpacker: Callable[[bytes], Any] | None = None, start: bool = True) -> None:
        self.worker_id = worker_id
        self.script_path = script_path
        self.worker_name = worker_name
        self.max_payload_bytes = max_payload_bytes
        # Kokoro's venv interpreter and PYTHONPATH. Compute uses this
        # process's Python and a scrubbed copy of the parent environment.
        self.executable = sys.executable if executable is None else executable
        self._child_env = env
        # Kokoro passes 20s. None copies the module constant here, in
        # __init__. Patching it after this returns does not change the handshake.
        self._ready_timeout_sec = _SPAWN_READY_TIMEOUT_SEC if ready_timeout_sec is None else ready_timeout_sec
        # Compute passes the restricted unpickler. None is stock pickle,
        # which is what Kokoro's plain dict frames use.
        self._unpacker = unpacker
        # The callback receives this wrapper and the reaped pid. It runs
        # only after wait() has reaped the child, so a pid that is still
        # alive is not reported as exited. It runs while ``_lifecycle_lock``
        # is held and must not re-enter this worker. Formula matches the
        # wrapper: is_alive()'s poll() can free the pid before this runs, and
        # another slot may already be using that number.
        self.on_process_exit = on_process_exit
        self.process: subprocess.Popen[bytes] | None = None
        self.lock = threading.Lock()
        # Serializes kill/reap. Distinct from ``lock``, which execute holds
        # across spawn, so reap must not take ``lock``.
        self._lifecycle_lock = threading.Lock()
        self.tasks_executed = 0
        # Path of the current child's stderr file. Unlinked when that child
        # is reaped. None between children.
        self._stderr_path: str | None = None
        # shutdown calls request_shutdown() before kill(). execute and respawn
        # read the flag without self.lock: kill takes _lifecycle_lock, not the
        # execute lock, so a flag under self.lock would stay invisible until
        # the cell finished. The write is under _lifecycle_lock so adopt either
        # sees shutdown or publishes a child that kill() still reaps.
        # CPython's GIL publishes this bool. A free-threaded build would need
        # the read under _lifecycle_lock. ``is_shutting_down`` and
        # ``_reap_incomplete`` are the same kind of read: the pool may hold
        # its condition, and on_process_exit holds this lock while taking
        # that condition. Taking the lock in ``is_shutting_down`` deadlocked
        # that callback; kill, respawn, and request_shutdown already refuse
        # re-entry instead.
        self._shutting_down = False
        # True when kill() left the child alive. Release must not idle that slot.
        self._reap_incomplete = False
        # True during the ready read. is_alive must not unlink the log yet.
        self._spawning = False
        # Ident of the thread inside on_process_exit. Checked before the
        # lifecycle lock so a callback cannot deadlock on it.
        self._reap_thread_id = None
        # Kokoro publishes the object before the handshake so Stop can kill
        # it. Compute and direct callers spawn here.
        if start:
            self.respawn()

    def _stderr_log_prefix(self) -> str:
        """Temp-file prefix so ``/tmp`` shows which pool owns the log.

        What was wrong: every child used ``wa-compute-w{id}-``, including
        Kokoro and vision. A leaked file could not be told apart from a
        formula worker.
        """
        parts = "".join(ch if ch.isalnum() else "-" for ch in self.worker_name.lower()).split("-")
        cleaned = "-".join(part for part in parts if part)
        if not cleaned:
            cleaned = "worker"
        return f"wa-{cleaned}-w{self.worker_id}-"

    def _open_stderr_log(self) -> tuple[IO[bytes], str]:
        """Open a stderr file for the child about to be spawned.

        A pipe would need a drain thread so the kernel buffer cannot fill
        and stall the handshake. The file is read only when spawn or a
        request fails, then unlinked when the child is reaped. ``cap_stderr_log``
        keeps the last ``_STDERR_LOG_CAP`` bytes after a request, on each
        idle-reaper scan, and when a reap leaves the child alive.

        Always a new file. Reap clears ``_stderr_path`` before this runs,
        so there is no live path to reuse. Does not publish ``_stderr_path``.
        ``kill()`` unlinks only that published path, so a shutdown during
        ``Popen`` cannot delete this file before the child inherits the fd.
        ``respawn`` publishes the path and the ``Popen`` together after
        ``Popen`` returns.

        What was wrong: ``mkstemp`` was closed and the path reopened. That
        drops the exclusive fd, so a name swap in ``/tmp`` could replace
        the file, and only the second ``open(..., "ab")`` set ``O_APPEND``.
        ``cap_stderr_log`` truncates; the child must inherit append mode
        or its next write lands in the middle of the kept tail. The fd
        returned here is the ``O_EXCL`` create.
        """
        directory = tempfile.gettempdir()
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND
        prefix = self._stderr_log_prefix()
        attempts_left = 100
        while attempts_left:
            attempts_left -= 1
            path = os.path.join(directory, f"{prefix}{os.urandom(8).hex()}.stderr")
            try:
                fd = os.open(path, flags, 0o600)
            except FileExistsError:
                continue
            try:
                handle = os.fdopen(fd, "ab", buffering=0)
            except Exception:
                os.close(fd)
                raise
            # fdopen records the integer fd as ``.name``. Popen uses fileno(),
            # and the path returned with this handle is the unlink key.
            # Assigning ``.name`` is not a stable writable API on the older
            # CPython LibreOffice bundles.
            return handle, path
        raise OSError(f"could not create a stderr log for {self.worker_name} #{self.worker_id}")

    def _close_stderr_handle(self, handle: IO[bytes]) -> None:
        """Close the parent's copy of the child's stderr fd.

        What was wrong: unlink ran while this ``FileIO`` was still open.
        Windows will not delete that file, ``_safe_unlink`` swallowed the
        error, and the temp file stayed. The child already has its own fd,
        so this copy can close as soon as ``Popen`` returns. ``close`` is
        idempotent if a caller hits it twice.
        """
        try:
            handle.close()
        except Exception:
            log.debug("%s #%d could not close stderr log", self.worker_name, self.worker_id, exc_info=True)

    def _safe_unlink(self, path: str) -> None:
        """Remove *path*. A missing file is normal when shutdown already unlinked it."""
        try:
            os.unlink(path)
        except OSError:
            log.debug("%s #%d could not remove stderr log %s", self.worker_name, self.worker_id, path, exc_info=True)

    def _unlink_unpublished_stderr(self, path: str) -> None:
        """Remove a stderr file that was never stored on ``_stderr_path``.

        ``kill`` only unlinks the published path. A file Popen inherited and
        shutdown refused must be removed here or it leaks.
        """
        if self._stderr_path != path:
            self._safe_unlink(path)

    def _close_stderr_log(self) -> None:
        """Unlink the current stderr file. Caller holds ``_lifecycle_lock``."""
        path = self._stderr_path
        self._stderr_path = None
        if path is None:
            return
        self._safe_unlink(path)

    def cap_stderr_log(self) -> bytes | None:
        """Keep the last ``_STDERR_LOG_CAP`` bytes of this slot's stderr file.

        Returns that tail when the file was over the cap and the rewrite
        succeeded. Returns None when there is no file, the file is already
        within the cap (it is not read), or the read failed. The idle
        reaper calls this on every scan, so the short file stays a
        ``getsize``. ``_stderr_snippet`` uses a returned tail and does not
        open the file again.

        The child inherited an ``O_APPEND`` fd, so the next write goes to the
        new end after a truncate. On POSIX that flag is atomic in the kernel.
        On Windows the C runtime emulates append with an lseek before each
        write, so a child that inherited the handle can write at the old
        offset after this truncate and tear the diagnostic tail. That is
        not a crash. A write that lands during the rewrite can also drop
        a few bytes; this file is only a failure tail.

        The idle reaper calls this outside the pool condition, and a lease
        may cap or unlink the same path at the same time. That race can tear
        the tail. It cannot crash: ``OSError`` is caught. Do not take
        ``_lifecycle_lock`` here. ``kill`` holds that lock across ``wait()``.
        """
        path = self._stderr_path
        if not path:
            return None
        try:
            size = os.path.getsize(path)
            if size <= _STDERR_LOG_CAP:
                return None
            with open(path, "rb") as handle:
                handle.seek(size - _STDERR_LOG_CAP)
                tail = handle.read(_STDERR_LOG_CAP)
            # r+b is not O_APPEND. Truncate, then write the tail at offset 0.
            # The child's separate append fd writes at the new end.
            # What was wrong: FileIO.write is os.write and may return a short
            # count. Ignoring it left the file truncated and dropped the tail.
            with open(path, "r+b", buffering=0) as handle:
                handle.seek(0)
                handle.truncate(0)
                view = memoryview(tail)
                written = 0
                while written < len(view):
                    n = handle.write(view[written:])
                    if not isinstance(n, int) or n <= 0:
                        raise OSError("zero bytes written to stderr log")
                    written += n
            return bytes(tail)
        except OSError:
            log.debug("%s #%d could not cap stderr log %s", self.worker_name, self.worker_id, path, exc_info=True)
            return None

    def _stderr_snippet(self) -> str:
        """Last stderr bytes, decoded. Empty when this slot has no log file.

        An over-cap file is rewritten by ``cap_stderr_log``, which returns
        that tail. Reading it again used to open the file a second time.
        A file already under the cap is not read by the cap, so the open
        below is the only one.
        """
        capped = self.cap_stderr_log()
        if capped is not None:
            # Extra bytes so a multibyte character is not cut in half
            # before the character trim below.
            raw = capped[-_STDERR_SNIPPET * 4 :]
        else:
            path = self._stderr_path
            if not path:
                return ""
            try:
                with open(path, "rb") as handle:
                    handle.seek(0, os.SEEK_END)
                    size = handle.tell()
                    handle.seek(max(0, size - _STDERR_SNIPPET * 4))
                    raw = handle.read()
            except OSError:
                return ""
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return ""
        return text[-_STDERR_SNIPPET:]

    def _log_spawn_outcome(self, message: str) -> None:
        """Log *message*. A shutdown-interrupted handshake is not a failed spawn.

        Anything else is a failed spawn and includes the child's stderr
        tail, or ``stderr=<empty>`` when the file has none.
        """
        if self._shutting_down:
            log.info("%s", message)
            return
        snippet = self._stderr_snippet()
        extra = f" stderr={snippet!r}" if snippet else " stderr=<empty>"
        log.error("%s%s", message, extra)

    def _reap_previous_process(self) -> bool:
        """Wait on the Popen ``respawn`` is about to replace.

        Returns True when the slot is empty or ``wait()`` reaped the child.
        Returns False when the child is still alive after the wait: the slot
        stays on that ``Popen``, ``_reap_incomplete`` stays set, and
        ``on_process_exit`` does not run. The pool parks that slot and
        calls ``retry_reap``.

        ``poll()`` reaps an already-dead child so the next ``respawn`` does
        not leave a zombie. Kill first only when it is still running, so a
        reused pid is not signaled.
        """
        self._refuse_reentry()
        with self._lifecycle_lock:
            previous = self.process
            if previous is None:
                self._reap_incomplete = False
                self._close_stderr_log()
                return True
            pid = previous.pid
            # What was wrong: wait()'s timeout was swallowed, self.process was
            # already None, and on_process_exit still ran. The formula pool
            # dropped every session on a pid whose SIGKILL had not taken
            # effect (uninterruptible sleep).
            if not self._kill_and_wait(previous, pid, context=""):
                log.warning("%s #%d pid=%s still alive after reap; not reporting exit", self.worker_name, self.worker_id, pid)
                # The file stays with this child. Callers park the slot:
                # it is neither idle nor leased, and cold claim skips a
                # live process, so nothing else would call kill() again.
                self._reap_incomplete = True
                self.cap_stderr_log()
                return False
            self.process = None
            self._reap_incomplete = False
            # After wait(), the pid is reaped. Report this wrapper with it.
            # Matching the pid alone dropped another slot's session when
            # poll() had already freed this pid and the kernel reused it.
            # The callback runs under this lock. See BaseProcessWorker.
            if pid is not None and self.on_process_exit is not None:
                self._reap_thread_id = threading.get_ident()
                try:
                    self.on_process_exit(self, pid)
                except Exception:
                    log.exception("%s #%d process-exit callback failed for pid=%s", self.worker_name, self.worker_id, pid)
                finally:
                    self._reap_thread_id = None
            # Snippet readers run before kill(). Unlink only after the child
            # is reaped so a failure log still sees the tail.
            self._close_stderr_log()
            return True

    def respawn(self, timeout_sec: float | None = None, *, deadline: Deadline | None = None) -> None:
        """Spawn worker subprocess and await readiness handshake.

        Returns without a child when the pool is stopping, when the previous
        child is still alive after the reap wait, or when *deadline* is
        already too late to start one. Recycle only ``kill()``s and leaves the
        slot dead. The next lease's ``execute`` is what calls this.
        ``_shutting_down`` stops that spawn. The check after reap covers a
        shutdown that arrives while the previous child is reaped. The new
        ``Popen`` is published under ``_lifecycle_lock``, which ``kill()``
        also holds, so a shutdown during spawn still reaps it.

        *deadline* is the caller's request clock. The reap wait is inside that
        clock: budgeting the handshake from the time left before
        ``_reap_previous_process`` let a call run about a second past its
        timeout. ``None`` is startup, which has no request clock. Pass
        *timeout_sec* or *deadline*, not both: the handshake budget is
        ``_spawn_budget`` after ``Popen``, so an explicit *timeout_sec*
        would be discarded.
        """
        # What was wrong: both arguments were accepted and deadline silently
        # replaced timeout_sec. A caller that set a handshake cap next to a
        # request clock thought the cap applied.
        if deadline is not None and timeout_sec is not None:
            raise ValueError("respawn accepts timeout_sec or deadline, not both")
        if timeout_sec is None:
            timeout_sec = self._ready_timeout_sec
        self._refuse_reentry()
        if self._shutting_down:
            return
        if not self._reap_previous_process():
            return
        if self._shutting_down:
            return
        # What was wrong: spawn_budget was fixed before this wait, and
        # _REAP_WAIT_SEC can block for a second. A longer request with under
        # a second left still started a child. The handshake timeout is
        # computed after Popen, which can itself spend the clock.
        # A one-second budget with a few milliseconds left also used to
        # pass usable() and spawn. _spawn_budget refuses that.
        if deadline is not None and _spawn_budget(deadline) is None:
            return
        cmd = [self.executable, self.script_path]
        child_env = self._child_env if self._child_env is not None else scrub_subprocess_env(dict(os.environ))
        stderr_log: IO[bytes] | None = None
        stderr_path: str | None = None
        published_stderr = False
        try:
            # Scrub matches the venv host: drop PYTHONHOME / credential-like
            # names so the child does not inherit the parent's secret env.
            # **creationflags kwargs make the type checker treat this as Popen[str].
            # stderr is a file, not a pipe: nothing has to drain it.
            # The path stays unpublished until Popen returns, so kill() during
            # Popen cannot unlink it before the child inherits the fd.
            # A grandchild that inherits this stdout pipe holds the host read
            # open until the request deadline; that timeout kills the child.
            stderr_log, stderr_path = self._open_stderr_log()
            try:
                proc = cast("subprocess.Popen[bytes]", subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_log, bufsize=0, text=False, env=child_env, **get_subprocess_creationflags()))
            finally:
                # The child has inherited this fd, or Popen failed before
                # that. Close the parent copy before any unlink. Windows
                # will not delete a file we still have open, and
                # _safe_unlink would swallow that and leak the file.
                # What was wrong: this handle was closed after Popen, again
                # in the except (so the unlink could succeed), and again in
                # the outer finally. One close here runs before that unlink.
                if stderr_log is not None:
                    self._close_stderr_handle(stderr_log)
                    stderr_log = None
            # Do not hold _lifecycle_lock across Popen or the handshake read.
            # kill() takes that lock, and the handshake can block for the
            # spawn budget. Process and stderr path publish together, after
            # the child has inherited the fd.
            if not self._adopt_spawned_process(proc, stderr_path):
                self._drop_unadopted_spawn(proc, stderr_path)
                return
            published_stderr = True
            optimize_popen_pipes(proc)
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
            # What was wrong: the handshake passes is_alive, and on Windows
            # that runs before the read sees EOF. __init__ and Kokoro do not
            # hold self.lock, so a dead child unlinked the log and the
            # failure was stderr=<empty>. POSIX calls is_alive only when
            # select times out. The flag stays set until the outcome is logged.
            self._spawning = True
            try:
                ready_data: Any = None
                if proc.stdout is not None:
                    ready_data = read_pickle_frame_with_timeout(proc.stdout, timeout_sec, is_alive=self.is_alive, max_payload_bytes=self.max_payload_bytes, require_dict=True, unpacker=self._unpacker)
                # Only status "ready" counts. EOF or any other dict would mark the
                # worker idle, and the next execute would wait out the full
                # timeout before EMPTY_RESPONSE.
                if not isinstance(ready_data, dict) or ready_data.get("status") != "ready":
                    status = ready_data.get("status") if isinstance(ready_data, dict) else None
                    self._log_spawn_outcome(f"{self.worker_name} #{self.worker_id} spawn handshake was not ready (status={status!r})")
                    self.kill()
                    return
                log.info("%s #%d spawned (pid=%s, status=%s)", self.worker_name, self.worker_id, ready_data.get("pid", proc.pid), ready_data.get("status"))
                self.tasks_executed = 0
            except (subprocess.TimeoutExpired, IpcPartialFrameTimeout):
                # No-byte hang is TimeoutExpired. A deadline after the first
                # handshake byte is IpcPartialFrameTimeout (a ConnectionError).
                # That used to be logged as "Failed to spawn". The child is
                # killed either way; the next lease respawns the slot.
                spawned = self.process
                rc = spawned.poll() if spawned is not None else None
                self._log_spawn_outcome(f"{self.worker_name} #{self.worker_id} spawn handshake timed out (returncode={rc})")
                self.kill()
            finally:
                self._spawning = False
        except Exception as exc:
            message = f"Failed to spawn {self.worker_name} #{self.worker_id}: {exc}"
            # What was wrong: this kill() reaped self.process. A second
            # respawn can raise before adopt while the first call's child
            # is already published, and that child was healthy. The
            # unpublished file is still removed. kill() only unlinks
            # _stderr_path, so this path would leak it. The parent fd is
            # already closed; an unlink while it was open failed on Windows.
            if not published_stderr:
                if self._shutting_down:
                    log.info("%s", message)
                else:
                    log.error("%s", message)
                if stderr_path is not None:
                    self._unlink_unpublished_stderr(stderr_path)
            else:
                self._log_spawn_outcome(message)
                self.kill()

    def _adopt_spawned_process(self, proc: subprocess.Popen[bytes], stderr_path: str) -> bool:
        """Publish *proc* and its stderr file, or refuse.

        Both assignments share one ``_lifecycle_lock`` hold with
        ``_shutting_down``, which ``kill()`` holds across reap. Publishing
        the path in an earlier hold let ``kill()`` unlink it while
        ``self.process`` was still None, then adopt a live child whose
        diagnostics read as empty. Shutdown either sees this child and its
        file, or this method refuses and the caller kills the unpublished
        ``Popen`` and unlinks the file.

        A live ``self.process`` is the same refusal. ``execute`` holds
        ``self.lock`` across ``respawn``, but ``__init__(start=True)`` and
        Kokoro call ``respawn`` without that lock, and the lock is not
        re-entrant, so ``respawn`` cannot take it. Two calls can both pass
        the reap and both ``Popen``. Overwriting ``self.process`` leaked
        the first child: nothing stored it, so nothing reaped it.
        """
        with self._lifecycle_lock:
            if self._shutting_down:
                return False
            # What was wrong: the second adopt assigned over a child the
            # first call had already published. That Popen was dropped
            # with no kill. Refusing here leaves the first child in place.
            # The caller kills the unpublished one.
            if self.process is not None:
                return False
            self.process = proc
            self._stderr_path = stderr_path
            return True

    def _kill_and_wait(self, proc: subprocess.Popen[bytes], pid: int | None, *, context: str) -> bool:
        """SIGKILL *proc* only while ``poll()`` says it is running, then wait.

        Returns True when ``poll()`` is not None after the wait. A timeout
        leaves False: SIGKILL has not taken effect, and the pid must not be
        reported as exited.

        After ``wait()`` the pid can be reused and must not be signaled.
        *context* is ``""`` for a published child and ``"unadopted "`` for
        one adopt refused before ``self.process`` was assigned.
        """
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                log.debug("%s #%d kill of %spid=%s failed", self.worker_name, self.worker_id, context, pid, exc_info=True)
        try:
            proc.wait(timeout=_REAP_WAIT_SEC)
        except Exception:
            log.debug("%s #%d wait for %spid=%s failed", self.worker_name, self.worker_id, context, pid, exc_info=True)
        reaped = proc.poll() is not None
        if reaped:
            self._close_reaped_pipes(proc)
        return reaped

    def _close_reaped_pipes(self, proc: subprocess.Popen[bytes]) -> None:
        """Close the parent's stdin and stdout after the child has been reaped.

        What was wrong: ``kill`` and ``wait`` left those fds on the Popen.
        They closed only when the object was collected. A traceback, the
        exit callback, or a test double can keep it alive, so the pipes
        leaked, and on Windows the leaked handles keep the child's ends open.
        Close only after ``wait`` has reaped the child. An incomplete reap
        leaves them: a reader may still be in ``select``, and the next
        retry closes them once the pid is gone. stderr is the log file,
        already closed after ``Popen``; ``proc.stderr`` is None.
        """
        for stream in (getattr(proc, "stdin", None), getattr(proc, "stdout", None)):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                log.debug("%s #%d could not close a reaped pipe", self.worker_name, self.worker_id, exc_info=True)

    def _drop_unadopted_spawn(self, proc: subprocess.Popen[bytes], stderr_name: str) -> None:
        """Kill a child adopt refused, and drop the stderr file it inherited.

        Adopt publishes the process and the path together, so a refusal
        never stored *stderr_name*. ``kill`` only unlinks ``_stderr_path``.
        A child that survives this wait is neither tracked nor retried.
        """
        # What was wrong: a failed wait returned False and this method
        # continued as if the child were gone. Shutdown, or a second
        # respawn, had no handle left and never tried again. The published
        # reap already warns; this path only debug-logged the wait.
        if not self._kill_and_wait(proc, proc.pid, context="unadopted "):
            log.warning("%s #%d unadopted pid=%s still alive after reap; not tracked", self.worker_name, self.worker_id, proc.pid)
        self._unlink_unpublished_stderr(stderr_name)

    def is_alive(self) -> bool:
        """True only while ``poll()`` is None.

        False means there is no process, or ``poll()`` has already reaped
        it. A non-blocking lock miss on that dead path still returns False:
        it only skips unlinking the stderr file. Callers that drop an idle
        slot or its sessions on False are reading a dead child.
        """
        # kill() assigns None under _lifecycle_lock while execute's select
        # loop calls this. A second read of self.process could be None and
        # raise AttributeError, which the write handler reported as
        # REQUEST_NOT_SERIALIZABLE. Snapshot once before poll().
        proc = self.process
        if proc is None:
            return False
        # poll() reaps a zombie and does not run on_process_exit. A later
        # reap still reports that pid, which the kernel may already have
        # reused; the callback carries this wrapper so the formula pool
        # does not drop another slot's session. Idle prune and session
        # finalize call this while holding the pool condition; the callback
        # takes that condition, so it cannot run here. Formula drops those
        # sessions in _reap_dead_sessions_unlocked.
        if proc.poll() is None:
            return True
        # What was wrong: a startup crash called this before the handshake
        # logged. The lock is free for __init__ and Kokoro, so the unlink
        # ran and the failure reported stderr=<empty>. The request path
        # holds self.lock and already skips the unlink below.
        if self._spawning:
            return False
        # What was wrong: poll() reaped an idle child and left _stderr_path.
        # A slot that was never leased again kept its /tmp/wa-<name>-w<id>-* file
        # until the pool shut down. Unlink only when no request holds
        # self.lock — execute still reads the tail into the error dict —
        # and reap does not already hold _lifecycle_lock. Both acquires are
        # non-blocking. This thread may already hold self.lock (the IPC
        # loop calls is_alive from execute), and a caller may hold the pool
        # condition while kill() holds the lifecycle lock across wait().
        # Leave self.process set so a later kill still runs on_process_exit.
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if not self._lifecycle_lock.acquire(blocking=False):
                return False
            try:
                if self.process is proc:
                    self._close_stderr_log()
            finally:
                self._lifecycle_lock.release()
        finally:
            self.lock.release()
        return False

    def is_shutting_down(self) -> bool:
        """Whether ``request_shutdown`` has published that this slot must not start work.

        Read unlocked, same as ``respawn`` and ``execute``. ``on_process_exit``
        holds ``_lifecycle_lock``; taking it here deadlocked that callback.
        CPython's GIL publishes the bool.
        """
        return self._shutting_down

    def request_shutdown(self) -> None:
        """Publish shutdown so execute and respawn will not start a child.

        The flag shares ``_lifecycle_lock`` with process publish. Callers
        must not hold the pool condition: reap's ``on_process_exit`` takes
        that condition while this lock is held.
        """
        self._refuse_reentry()
        with self._lifecycle_lock:
            self._shutting_down = True

    def _refuse_reentry(self) -> None:
        """Raise when this thread is inside ``on_process_exit``.

        ``_lifecycle_lock`` is not re-entrant. A check after acquiring it
        never runs: the callback's thread already holds the lock, so
        ``kill`` would deadlock first. Another thread's ident does not
        match, and that thread waits on the lock.
        """
        if self._reap_thread_id == threading.get_ident():
            raise RuntimeError(f"{self.worker_name} #{self.worker_id} re-entered from on_process_exit")

    def reap_incomplete(self) -> bool:
        """True when the last reap left the child alive.

        Read unlocked, same as ``_shutting_down``. The pool may hold its
        condition here, and ``on_process_exit`` holds ``_lifecycle_lock``
        while taking that condition.
        """
        return self._reap_incomplete

    def kill(self) -> bool:
        """Terminate the child using the same rules as spawn's reap.

        True when the slot is empty or ``wait`` reaped the child. False
        when the child is still alive: ``reap_incomplete`` is set, and the
        slot must not be idled or reused. The pool still reads that flag.
        A test stand-in whose ``kill`` returns None counts as finished.
        """
        return self._reap_previous_process()

    def retry_reap(self, stop: threading.Event) -> None:
        """Call ``kill`` until the child is gone or *stop* is set.

        One ``kill`` can return while the pid is still alive (uninterruptible
        sleep, or a signal that did not land). The caller has already dropped
        this slot from idle and leased. Cold claim skips a live process, so
        ``on_process_exit`` would not run again. Each ``kill`` already waits
        up to ``_REAP_WAIT_SEC``.

        What was wrong: a ``wait`` that raises immediately makes ``kill``
        return at once. The loop called it again with no pause and logged
        a warning on every pass. ``stop.wait`` is that pause, and it
        returns as soon as shutdown sets the event. A reap that finishes
        does not wait.
        """
        while not stop.is_set() and self.reap_incomplete():
            self.kill()
            if not self.reap_incomplete():
                return
            stop.wait(0.1)

    def _fail_request(self, code: str, msg: str, *, pid: int | None, kill: bool = True) -> dict[str, Any]:
        """Build an error dict, then kill the child or leave it.

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
        # What was wrong: kill=False still read the stderr file. That is
        # shutdown, a clock spent before the frame was sent, or a spawn
        # that already logged its own tail. The read could attach a
        # previous child's text to an unrelated error. The tail belongs
        # on the failure that is about to kill the child that wrote it.
        if kill:
            snippet = self._stderr_snippet()
            if snippet:
                msg = f"{msg}\n{snippet}"
            log.warning("%s request failed (%s) on worker #%d; terminating pid=%s", self.worker_name, code, self.worker_id, pid)
            self.kill()
        message = msg if code in _MIRROR_MESSAGE else None
        return error_dict(code, msg, message=message)

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

    def _fail_if_unusable(self, deadline: Deadline, proc: subprocess.Popen[bytes] | None) -> dict[str, Any] | None:
        """``EXECUTION_TIMEOUT`` when a spawn or a write must not start, else None.

        Does not kill. The frame has not been sent. The check after a
        successful write is separate: that one kills, because the child
        already has the frame.
        """
        if deadline.usable() is not None:
            return None
        pid = proc.pid if proc is not None else None
        return self._fail_timeout(deadline, pid, kill=False)

    def _ensure_live_process(self, deadline: Deadline) -> tuple[subprocess.Popen[bytes], IO[bytes], IO[bytes]] | dict[str, Any]:
        """Return a live child and its stdio pipes, or an error dict.

        Caller holds ``self.lock``. ``kill()`` during shutdown sets
        ``self.process`` to None; reading ``.pid`` or ``.stdin`` on that
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
                return self._fail_request("SERVICE_SHUTDOWN", f"{self.worker_name} #{self.worker_id} is shutting down.", pid=pid, kill=False)
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
                # frame. What was wrong: a cleanup kill() that landed mid-write
                # was WORKER_PIPE_BROKEN. The read path already maps EOF during
                # shutdown to SERVICE_SHUTDOWN. The child is already being reaped.
                if self._shutting_down:
                    return self._fail_request("SERVICE_SHUTDOWN", f"{self.worker_name} #{self.worker_id} is shutting down.", pid=pid, kill=False)
                return self._fail_request("WORKER_PIPE_BROKEN", f"Failed to send request to {self.worker_name} #{self.worker_id}: {exc}", pid=pid)

            remaining = deadline.left()
            if remaining <= 0:
                # The frame was written. Do not start a read on a spent clock.
                return self._fail_timeout(deadline, pid)
            try:
                resp = read_pickle_frame_with_timeout(stdout, remaining, is_alive=self.is_alive, max_payload_bytes=self.max_payload_bytes, unpacker=self._unpacker)
            except subprocess.TimeoutExpired:
                # The frame never arrived. Kill rather than read it later:
                # the next caller would consume this response as its own.
                return self._fail_timeout(deadline, pid)
            except IpcPartialFrameTimeout:
                # A deadline after the first byte is a timeout, not a crash.
                # The next read would consume the rest of this frame as a
                # new response, so the child is killed.
                return self._fail_timeout(deadline, pid)
            except IpcFrameError as exc:
                # What was wrong: an invalid or oversized response frame was
                # WORKER_CRASHED. The child is still killed: the pipe is not
                # on a frame the next request can read. IpcFrameError is a
                # ValueError, so it has to be caught before that handler.
                return self._fail_request("WORKER_IPC_ERROR", f"{self.worker_name} rejected a response frame: {exc}", pid=pid)
            except ValueError as exc:
                # The restricted unpickler rejects the payload after those
                # bytes were consumed. Same kill, and not a process crash.
                return self._fail_request("WORKER_IPC_ERROR", f"{self.worker_name} rejected a response frame: {exc}", pid=pid)
            except Exception as exc:
                return self._fail_request("WORKER_CRASHED", f"{self.worker_name} error: {exc}", pid=pid)
            if resp is None or not isinstance(resp, dict):
                # Shutdown kills the child while this read is in flight. EOF
                # used to be EMPTY_RESPONSE, which counts a task and looks
                # like a crash. The pool is stopping, so this is
                # SERVICE_SHUTDOWN and the child is already being reaped.
                if self._shutting_down:
                    return self._fail_request("SERVICE_SHUTDOWN", f"{self.worker_name} #{self.worker_id} is shutting down.", pid=pid, kill=False)
                return self._fail_request("EMPTY_RESPONSE", f"No response returned from {self.worker_name}.", pid=pid)
            self.tasks_executed += 1
            self.cap_stderr_log()
            return resp

