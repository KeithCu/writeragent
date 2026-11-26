# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Process-wide supervisor for warm framed-stdio child workers."""

from __future__ import annotations

import functools
import logging
import pickle
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import IO, Any, Literal, Protocol, final

from plugin.framework.child_runtime import Deadline, error_dict, is_ready_payload
from plugin.framework.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    IpcFrameError,
    IpcPartialFrameTimeout,
    pack_pickle_frame,
    read_pickle_frame,
    write_packed_frame_with_timeout,
)
from plugin.framework.process_global import process_global
from plugin.framework import managed_subprocess
from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.framework.send_cancellation import SendCancellation, is_cancelled
from plugin.framework.worker_pool import run_in_background

log = logging.getLogger(__name__)
_child_log = logging.getLogger("compute_service.worker")

SPAWN_READY_TIMEOUT_SEC = 15.0
_STDERR_SNIPPET = 500
STDERR_LOG_CAP = 64 * 1024
_MIN_SPAWN_REMAINING_SEC = 0.25
FailPhase = Literal["before_send", "write_timeout", "pipe_broken", "after_send", "read_timeout", "bad_frame", "crashed", "empty"]
HandshakeTag = Literal["ready", "interrupted", "not_ready", "timeout"]
SpawnTag = Literal[HandshakeTag, "refused", "budget_exhausted", "failed"]
_SPAWN_SHUTDOWN = object()


class FramedRequestOnFail(Protocol):
    def __call__(self, code: str, detail: str, *, phase: FailPhase) -> dict[str, Any]: ...


def fail_phase_message(phase: FailPhase, detail: str) -> str:
    """User-facing error text for one ``framed_request_roundtrip`` failure (WarmChild lease path)."""
    if phase == "pipe_broken":
        return f"Failed to send request: {detail}"
    if phase == "bad_frame":
        return f"Invalid response frame: {detail}"
    if phase == "crashed":
        return f"Worker error: {detail}"
    if phase in ("before_send", "after_send"):
        return "Execution budget exhausted."
    if phase == "write_timeout":
        return "Write timed out."
    if phase == "read_timeout":
        return "Read timed out."
    if phase == "empty":
        return "No response returned from worker."
    raise AssertionError(f"unhandled FailPhase: {phase!r}")


def _error_with_tail(code: str, msg: str, child: Child | None) -> dict[str, Any]:
    """``error_dict`` with *child*'s stderr tail on its own line when there is one."""
    tail = child.diagnostic_tail() if child is not None else ""
    return error_dict(code, f"{msg}\n{tail}" if tail else msg)


def spawn_budget(deadline: Deadline) -> float | None:
    """Seconds for a new handshake, or None when a child must not start."""
    remaining = deadline.usable()
    if remaining is None or remaining < _MIN_SPAWN_REMAINING_SEC:
        return None
    return remaining


def stderr_thread_name(name: str, *, fallback: str, worker_id: int | None = None) -> str:
    """Thread name for a child's stderr drain: ``wa-<slug>[-w<id>]-stderr``; *fallback* when *name* has no letters or digits."""
    parts = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).split("-")
    cleaned = "-".join(part for part in parts if part)
    if not cleaned:
        cleaned = fallback
    if worker_id is not None:
        return f"wa-{cleaned}-w{worker_id}-stderr"
    return f"wa-{cleaned}-stderr"


class Child:
    """One spawned process: pipes, stderr tail, and a single reap."""

    def __init__(self, managed: ManagedSubprocess, *, worker_name: str, worker_id: int) -> None:
        self.managed: ManagedSubprocess = managed
        self.worker_name: str = worker_name
        self.worker_id: int = worker_id
        self._io_owner: int | None = None
        self._signaled: bool = False
        self._settled: bool = False
        self._once: threading.Lock = threading.Lock()

    def was_signaled(self) -> bool:
        return self._signaled

    def begin_io(self) -> None:
        self._io_owner = threading.get_ident()

    def end_io(self) -> bool:
        if self._io_owner != threading.get_ident():
            return self._settled
        self._io_owner = None
        if not self._signaled:
            return False
        return self._finish()

    def signal_from(self) -> bool:
        self._signaled = True
        self.managed.kill_tree()
        me = threading.get_ident()
        # One read after setting _signaled is safe: if another thread owns I/O now,
        # its end_io clears _io_owner before checking _signaled and runs _finish(),
        # which also re-checks _io_owner under _once.
        if self._io_owner not in (None, me):
            return False
        return self._finish()

    def diagnostic_tail(self) -> str:
        if self.managed.stderr_tail is None:
            return ""
        text = self.managed.stderr_text().strip()
        if not text:
            return ""
        return text[-_STDERR_SNIPPET:]

    def _finish(self) -> bool:
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
            pid = getattr(self.managed.popen, "pid", None)
            _child_log.warning("%s #%s pid=%s still alive after SIGKILL; abandoning", self.worker_name, self.worker_id, pid)
            self.managed.abandon()
            return True

    def abandon(self) -> None:
        """Drop this child on quit without waiting for stdio or reap.

        soffice exits right after the quit hooks, and the managed reaper finishes the reap.
        """
        self._signaled = True
        self._settled = True
        self.managed.kill_tree()
        self.managed.abandon()


def read_ready_frame_during_io(
    spawned: Child,
    proc: Any,
    timeout_sec: float,
    *,
    is_alive: Callable[[], bool],
    max_payload_bytes: int,
    unpacker: Callable[[bytes], Any] | None,
    cancel: SendCancellation | None = None,
) -> tuple[HandshakeTag, Any]:
    """Read the spawn ready frame while *spawned* owns the pipes (between begin_io and end_io)."""
    if spawned.was_signaled():
        return ("interrupted", None)
    ready_data: Any = None
    if proc is not None and proc.stdout is not None:
        try:
            ready_data = read_pickle_frame(
                proc.stdout,
                timeout_sec,
                is_alive=is_alive,
                max_payload_bytes=max_payload_bytes,
                require_dict=True,
                unpacker=unpacker,
                cancel=cancel,
            )
        except (subprocess.TimeoutExpired, IpcPartialFrameTimeout):
            # Stop also raises TimeoutExpired (see read_frame_payload); keep this
            # as "timeout" so _spawn_child kills the child and _ensure_live
            # reports CANCELLED from _publish_refusal.
            return ("timeout", None)
    if spawned.was_signaled():
        return ("interrupted", None)
    # Only status "ready" counts. EOF or any other dict would mark the worker idle,
    # and the next execute would wait out the full timeout before EMPTY_RESPONSE.
    if not is_ready_payload(ready_data):
        status = ready_data.get("status") if isinstance(ready_data, dict) else None
        return ("not_ready", status)
    return ("ready", ready_data)


@dataclass(frozen=True)
class SpawnOutcome:
    """Result of :meth:`ChildSlot._spawn_child`."""

    tag: SpawnTag
    detail: Any = None
    child: Child | None = None
    published: bool = False


class ChildSlot:
    """One child-process slot: the published ``child``, its lock, and the spawn protocol.

    ``WarmChild`` and the compute pool's ``BaseProcessWorker`` subclass this.
    ``_lifecycle_lock`` guards publish, drop, and each subclass's stop flags, so a
    publish either sees a stop or stores a child the stopper's ``kill`` still
    signals. The lock is never held across ``start()``, the handshake read, or the signal
    and reap in ``kill()``: it only covers reading, storing or dropping the pointer and the
    stop flags, and the handshake can block for the whole spawn budget.
    """

    child: Child | None
    _lifecycle_lock: threading.Lock

    def __init__(self) -> None:
        self.child = None
        self._lifecycle_lock = threading.Lock()

    def is_alive(self) -> bool:
        """True only while the current child's ``poll()`` is None.

        False means there is no child, or ``poll()`` has already reaped it.
        A child handed to the module reaper is not current, so this is False
        and the slot can be spawned again.
        """
        # kill() sets self.child to None under _lifecycle_lock while a caller's
        # select loop calls this. Read self.child once: a second read can see None
        # and raise AttributeError, which a write handler could misreport (the
        # compute pool's execute write handler reported it as REQUEST_NOT_SERIALIZABLE).
        child = self.child
        if child is None:
            return False
        try:
            return child.managed.is_alive()
        except Exception:
            _child_log.debug("%s #%s poll failed", child.worker_name, child.worker_id, exc_info=True)
            return False

    def kill(self) -> bool:
        """Signal the current child. Wait and close when this thread may.

        True when the slot is empty afterwards: the child was reaped, or it
        was abandoned to the module reaper. False when another thread is
        inside the pipes. That thread's ``end_io`` drops the child.
        """
        child = self.child
        if child is None:
            return True
        return self._signal_and_drop(child)

    def _signal_and_drop(self, child: Child) -> bool:
        """Signal *child*; drop it only when this thread finished the reap."""
        if not child.signal_from():
            return False
        self._drop_child(child)
        return self.child is not child

    def _drop_child(self, child: Child) -> None:
        with self._lifecycle_lock:
            if self.child is child:
                self.child = None

    def _end_child_io(self, child: Child) -> None:
        """Leave the pipe section and drop the child when that reap finished."""
        if child.end_io():
            self._drop_child(child)

    def _publish_refusal(self) -> dict[str, Any] | None:
        """Return why this slot may not start a child now, or None.

        ``_publish`` calls this under ``_lifecycle_lock`` and that answer decides
        whether the child is stored. Unlocked calls before a spawn save a wasted
        process, and after a failed one they report the stop as the reason.
        """
        return None

    def _publish(self, child: Child) -> dict[str, Any] | None:
        """Store *child* in the empty slot, or return the refusal. One lock hold decides.

        A stop and a second live child are both refusals. The caller signals only
        its own unpublished child, never the one already here.
        """
        with self._lifecycle_lock:
            refusal = self._publish_refusal()
            if refusal is None and self.child is not None:
                refusal = error_dict("WORKER_SPAWN_FAILED", f"{child.worker_name} already has a child.")
            if refusal is None:
                self.child = child
            return refusal

    def _spawn_child(
        self,
        managed: ManagedSubprocess,
        *,
        worker_name: str,
        worker_id: int,
        start: Callable[[ManagedSubprocess], None],
        deadline: Deadline | None,
        cap_sec: float,
        max_payload_bytes: int,
        unpacker: Callable[[bytes], Any] | None,
        cancel: SendCancellation | None = None,
    ) -> SpawnOutcome:
        """Start *managed*, publish it, read the ready frame, and clean up every failure.

        Every outcome except ``ready`` leaves the slot without this child. *start*
        must run ``Popen`` on the supervisor spawner thread (PDEATHSIG follows the
        creating thread). *cancel* ends the ready-frame read early; compute slots
        have none.
        """
        spawned: Child | None = None
        published = False
        try:
            start(managed)
            spawned = Child(managed, worker_name=worker_name, worker_id=worker_id)
            refusal = self._publish(spawned)
            if refusal is not None:
                spawned.signal_from()
                return SpawnOutcome("refused", refusal, spawned)
            published = True
            if deadline is None:
                timeout_sec = cap_sec
            else:
                spawn_for = spawn_budget(deadline)
                if spawn_for is None:
                    self.kill()
                    return SpawnOutcome("budget_exhausted", None, spawned, True)
                timeout_sec = min(cap_sec, spawn_for)
            popen = managed.popen
            spawned.begin_io()
            try:
                tag, detail = read_ready_frame_during_io(
                    spawned,
                    popen,
                    timeout_sec,
                    is_alive=self.is_alive,
                    max_payload_bytes=max_payload_bytes,
                    unpacker=unpacker,
                    cancel=cancel,
                )
                if tag == "interrupted":
                    self._drop_child(spawned)
                elif tag == "timeout":
                    detail = popen.poll() if popen is not None else None
                    self.kill()
                elif tag == "not_ready":
                    self.kill()
            finally:
                self._end_child_io(spawned)
            # Check after end_io so a kill after the last handshake check still kills this child.
            # was_signaled(), not self.child is not spawned: signal_from sets the flag first, and
            # the killer may not have dropped the child yet.
            if tag == "ready" and spawned.was_signaled():
                self._drop_child(spawned)
                tag, detail = "interrupted", None
            return SpawnOutcome(tag, detail, spawned, True)
        except Exception as exc:
            if spawned is not None:
                self._end_child_io(spawned)
            if published:
                self.kill()
            elif spawned is not None:
                spawned.signal_from()
            return SpawnOutcome("failed", exc, spawned, published)


def framed_request_roundtrip(
    stdin: IO[bytes],
    stdout: IO[bytes],
    frame: bytes,
    deadline: Deadline,
    *,
    is_alive: Callable[[], bool],
    max_payload_bytes: int,
    unpacker: Callable[[bytes], Any] | None,
    on_fail: FramedRequestOnFail,
) -> dict[str, Any]:
    """Write one packed frame and read the response dict under *deadline*."""
    usable = deadline.usable()
    if usable is None:
        return on_fail("EXECUTION_TIMEOUT", "", phase="before_send")
    try:
        write_packed_frame_with_timeout(stdin, frame, usable, is_alive=is_alive)
    except subprocess.TimeoutExpired:
        return on_fail("EXECUTION_TIMEOUT", "", phase="write_timeout")
    except (OSError, TypeError, ValueError) as exc:
        return on_fail("WORKER_PIPE_BROKEN", str(exc), phase="pipe_broken")
    remaining = deadline.left()
    if remaining <= 0:
        return on_fail("EXECUTION_TIMEOUT", "", phase="after_send")
    try:
        resp = read_pickle_frame(
            stdout,
            remaining,
            is_alive=is_alive,
            max_payload_bytes=max_payload_bytes,
            unpacker=unpacker,
        )
    except (subprocess.TimeoutExpired, IpcPartialFrameTimeout):
        return on_fail("EXECUTION_TIMEOUT", "", phase="read_timeout")
    except ValueError as exc:
        return on_fail("WORKER_IPC_ERROR", str(exc), phase="bad_frame")
    except Exception as exc:
        return on_fail("WORKER_CRASHED", str(exc), phase="crashed")
    if not isinstance(resp, dict):
        return on_fail("EMPTY_RESPONSE", "", phase="empty")
    return resp


@dataclass(frozen=True)
class ChildSpec:
    """Everything the supervisor needs to spawn one warm child script."""

    name: str
    argv: list[str]
    env: dict[str, str]
    command_wrapper: Callable[..., list[str]] | None = None
    idle_ttl_sec: float | None = None
    stderr_tail_chars: int = STDERR_LOG_CAP
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
    unpacker: Callable[[bytes], Any] | None = None
    # False keeps a missing interpreter as OSError (venv systemd-run retry depends on it).
    log_start_failure: bool = True


class Lease:
    """Pipe ownership for one warm-child request while ``begin_io`` is held."""

    __slots__: tuple[str, ...] = ("stdin", "stdout", "pid", "generation", "_warm")
    stdin: IO[bytes]
    stdout: IO[bytes]
    pid: int
    generation: int
    _warm: WarmChild

    def __init__(
        self,
        *,
        stdin: IO[bytes],
        stdout: IO[bytes],
        pid: int,
        generation: int,
        warm: WarmChild,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.pid = pid
        self.generation = generation
        self._warm = warm

    def is_alive(self) -> bool:
        return self._warm.is_alive()

    def fail(self, code: str, detail: str, *, phase: FailPhase) -> dict[str, Any]:
        return self._warm.fail_lease(code, detail, phase=phase)


class WarmChild(ChildSlot):
    """One registered warm worker: spawn on the supervisor thread, framed IPC."""

    def __init__(
        self,
        spec: ChildSpec,
        *,
        supervisor: Supervisor | None = None,
    ) -> None:
        super().__init__()
        self.spec: ChildSpec = spec
        self._supervisor: Supervisor = supervisor if supervisor is not None else _default_supervisor()
        self._io_lock: threading.Lock = threading.Lock()
        self._generation: int = 0
        self._last_active: float = time.monotonic()
        self._retired: bool = False
        self._killed_by_us: bool = False
        # Stop scope of the lease in progress; only the pipe-lock holder reads or writes it.
        self._lease_cancel: SendCancellation | None = None
        self._supervisor._register(self)

    def kill(self) -> bool:
        """Signal the child as ours.

        The child is read under the lock ``_publish`` holds while it checks the lease's
        ``SendCancellation`` and stores the child. A caller that cancels that scope before
        calling ``kill`` is therefore either seen by publish, or finds the child here and
        signals it. Without the lock, publish could check before the cancel and store after
        this read, and the cancelled request would keep its child.
        """
        with self._lifecycle_lock:
            self._killed_by_us = True
            child = self.child
        if child is None:
            return True
        return self._signal_and_drop(child)

    def _killed_before_lease(self) -> dict[str, Any]:
        return self._publish_refusal() or error_dict("WORKER_SPAWN_FAILED", f"{self.spec.name} was killed before the request started.")

    def _publish_refusal(self) -> dict[str, Any] | None:
        if self._retired or self._supervisor.is_shutting_down():
            return error_dict("SERVICE_SHUTDOWN", f"{self.spec.name} is shutting down.")
        if is_cancelled(self._lease_cancel):
            return error_dict("CANCELLED", f"{self.spec.name} request was cancelled.")
        return None

    def diagnostic_tail(self) -> str:
        child = self.child
        if child is None:
            return ""
        return child.diagnostic_tail()

    def io_owner(self) -> int | None:
        child = self.child
        if child is None:
            return None
        return child._io_owner

    def fail_lease(self, code: str, detail: str, *, phase: FailPhase) -> dict[str, Any]:
        child = self.child
        if child is not None:
            self.kill()
        return _error_with_tail(code, fail_phase_message(phase, detail), child)

    def abandon_for_quit(self) -> None:
        with self._lifecycle_lock:
            self._retired = True
            self._killed_by_us = True
            child = self.child
        if child is not None:
            self._drop_child(child)
            child.abandon()
        self._supervisor._unregister(self)

    def retire(self) -> None:
        with self._lifecycle_lock:
            self._retired = True
        self.kill()
        self._supervisor._unregister(self)

    @contextmanager
    def lease(
        self,
        deadline: Deadline,
        *,
        wait: float | None = None,
        cancel: SendCancellation | None = None,
        refuse: Callable[[], dict[str, Any] | None] | None = None,
        poll_sec: float = 0.05,
    ) -> Generator[Lease | dict[str, Any], None, None]:
        """Acquire the pipe lock (with optional wait polling), spawn if needed, yield ``Lease`` or an error dict.

        ``wait`` controls lock acquisition: ``None`` blocks until the lock is free (or ``cancel`` /
        ``refuse`` fires); ``0`` tries once; ``>0`` bounds total wait time. ``refuse`` is consulted
        only while waiting for the lock—not after it is held, because ``begin_io`` makes this
        thread the io owner and ``refuse`` would false-trigger self-reentry (see in-body comment).
        ``cancel`` is checked again once the lock is held, before any spawn, when a spawned child is published, during
        the spawn handshake, and when a spawn fails.
        """
        acquire_end: float | None = None
        if wait is not None and wait > 0:
            acquire_end = time.monotonic() + wait
        while True:
            if is_cancelled(cancel):
                yield error_dict("CANCELLED", "Lease wait cancelled.")
                return
            if refuse is not None:
                refusal = refuse()
                if refusal is not None:
                    yield refusal
                    return
            if wait == 0:
                if self._io_lock.acquire(blocking=False):
                    break
                yield error_dict("PIPE_LOCK_BUSY", "Could not acquire warm child pipe lock.")
                return
            if cancel is None and refuse is None and wait is None:
                self._io_lock.acquire()
                break
            if wait is None:
                slice_timeout = poll_sec
            else:
                assert acquire_end is not None
                remaining = acquire_end - time.monotonic()
                if remaining <= 0:
                    yield error_dict("PIPE_LOCK_BUSY", "Could not acquire warm child pipe lock.")
                    return
                slice_timeout = min(poll_sec, remaining)
            if self._io_lock.acquire(timeout=slice_timeout):
                break
        try:
            # refuse is not consulted after the lock is held: begin_io() makes this thread the
            # io_owner, so refuse would false-trigger self-reentry. _ensure_live checks cancel again.
            self._lease_cancel = cancel
            err = self._ensure_live(deadline)
            if err is not None:
                yield err
                return
            child = self.child
            if child is None:
                yield self._killed_before_lease()
                return
            # begin_io first: signal_from sets _signaled before it reads the io owner, so either
            # this check sees the kill or the killer leaves the reap to our end_io.
            child.begin_io()
            try:
                if child.was_signaled():
                    yield self._killed_before_lease()
                    return
                proc = child.managed.popen
                assert proc is not None
                stdin = proc.stdin
                stdout = proc.stdout
                if stdin is None or stdout is None:
                    yield error_dict(
                        "WORKER_SPAWN_FAILED",
                        f"{self.spec.name} has no stdio pipes.",
                    )
                    return
                yield Lease(
                    stdin=stdin,
                    stdout=stdout,
                    pid=int(proc.pid),
                    generation=self._generation,
                    warm=self,
                )
            finally:
                self._end_child_io(child)
                self._last_active = time.monotonic()
        finally:
            self._lease_cancel = None
            self._io_lock.release()

    def request(
        self,
        payload: dict[str, Any],
        deadline: Deadline,
        *,
        cancel: SendCancellation | None = None,
    ) -> dict[str, Any]:
        with self.lease(deadline, cancel=cancel) as leased:
            if isinstance(leased, dict):
                return leased
            try:
                frame = pack_pickle_frame(payload, max_payload_bytes=self.spec.max_payload_bytes)
            except IpcFrameError as exc:
                return error_dict("PAYLOAD_TOO_LARGE", str(exc))
            except (pickle.PicklingError, TypeError, ValueError, RecursionError) as exc:
                return error_dict("REQUEST_NOT_SERIALIZABLE", str(exc))
            return framed_request_roundtrip(
                leased.stdin,
                leased.stdout,
                frame,
                deadline,
                is_alive=self.is_alive,
                max_payload_bytes=self.spec.max_payload_bytes,
                unpacker=self.spec.unpacker,
                on_fail=leased.fail,
            )

    def _ensure_live(self, deadline: Deadline) -> dict[str, Any] | None:
        # Unlocked and early, so a stopped lease does not start a process only to kill it.
        # _publish asks again under _lifecycle_lock for a stop that lands after this.
        refusal = self._publish_refusal()
        if refusal is not None:
            return refusal
        child = self.child
        if child is not None and not self.is_alive():
            if not self._killed_by_us:
                popen = child.managed.popen
                assert popen is not None
                tail = child.diagnostic_tail()
                log.warning("%s child died unexpectedly while idle (returncode=%s)%s", self.spec.name, popen.poll(), f" stderr={tail!r}" if tail else "")
            self._drop_child(child)
            child = None
        if child is not None:
            return None
        spawn_for = spawn_budget(deadline)
        if spawn_for is None:
            return error_dict("EXECUTION_TIMEOUT", "Insufficient time to spawn worker.")
        self._killed_by_us = False
        managed = ManagedSubprocess(
            self.spec.argv,
            base_env=self.spec.env,
            env={},
            command_wrapper=self.spec.command_wrapper,
            scrub_env=False,
            optimize_pipes=True,
            stderr_tail_chars=self.spec.stderr_tail_chars,
            stderr_name=stderr_thread_name(self.spec.name, fallback="warm"),
            label=self.spec.name,
            log_start_failure=self.spec.log_start_failure,
        )
        outcome = self._spawn_child(
            managed,
            worker_name=self.spec.name,
            worker_id=0,
            start=functools.partial(self._supervisor.spawn, timeout_sec=spawn_for),
            deadline=deadline,
            cap_sec=SPAWN_READY_TIMEOUT_SEC,
            max_payload_bytes=self.spec.max_payload_bytes,
            unpacker=self.spec.unpacker,
            cancel=self._lease_cancel,
        )
        name = self.spec.name
        if outcome.tag == "ready":
            self._generation += 1
            self._last_active = time.monotonic()
            return None
        if outcome.tag == "refused":
            return outcome.detail
        # A stop sets its flag before it signals the child, so whatever the spawn saw, the stop is the reason.
        # Ahead of the OSError re-raise: a stopped lease must not start venv_worker's systemd-run retry.
        refusal = self._publish_refusal()
        if refusal is not None:
            return refusal
        if outcome.tag == "budget_exhausted":
            return error_dict("EXECUTION_TIMEOUT", "Insufficient time to spawn worker.")
        if outcome.tag == "interrupted":
            return error_dict("WORKER_SPAWN_FAILED", f"{name} spawn was interrupted.")
        if outcome.tag == "not_ready":
            return _error_with_tail("WORKER_SPAWN_FAILED", f"{name} spawn handshake was not ready (status={outcome.detail!r})", outcome.child)
        if outcome.tag == "timeout":
            return _error_with_tail("EXECUTION_TIMEOUT", f"{name} spawn handshake timed out", outcome.child)
        if not outcome.published and isinstance(outcome.detail, OSError):
            # ChildSpec.log_start_failure=False callers (such as venv_worker's systemd-run retry) retry a missing interpreter on OSError.
            raise outcome.detail
        return _error_with_tail("WORKER_SPAWN_FAILED", f"Failed to spawn {name}: {outcome.detail}", outcome.child)


@final
class _SpawnRequest:
    """One queued spawn. Whoever takes the lock first decides; only the spawner kills."""

    managed: ManagedSubprocess
    done: threading.Event
    error: BaseException | None
    abandoned: bool
    lock: threading.Lock

    def __init__(self, managed: ManagedSubprocess) -> None:
        self.managed = managed
        self.done = threading.Event()
        self.error = None
        self.abandoned = False
        self.lock = threading.Lock()

    def give_up(self) -> bool:
        """Called by waiter after a timeout. Returns True if abandoned, False if finished."""
        with self.lock:
            if self.done.is_set():
                return False
            self.abandoned = True
            return True

    def finish_spawn(self) -> None:
        """Called by spawner in finally after start(). Only spawner kills."""
        with self.lock:
            if self.abandoned and self.managed.popen is not None:
                self.managed.kill_tree()
                try:
                    self.managed.abandon()
                except Exception:
                    log.exception("Error abandoning managed subprocess on spawn timeout")
            self.done.set()


class Supervisor:
    """One spawner thread; warm-child registry, idle reaper, and shutdown.

    ``spawn`` also serves compute pool slots (``BaseProcessWorker``), which
    are not registered here — the pool owns their idle lifecycle and quit.
    """

    def __init__(self) -> None:
        self._registry_lock: threading.Lock = threading.Lock()
        self._registry: list[WarmChild] = []
        self._shutdown: bool = False
        self._threads_started: bool = False
        self._start_lock: threading.Lock = threading.Lock()
        self._spawn_gate: threading.Lock = threading.Lock()
        self._spawn_queue: queue.Queue[Any] = queue.Queue()
        self._spawner_ident: int | None = None
        self._reaper_stop: threading.Event = threading.Event()

    @property
    def spawner_ident(self) -> int | None:
        return self._spawner_ident

    def is_shutting_down(self) -> bool:
        return self._shutdown

    def _ensure_threads(self) -> None:
        if self._threads_started:
            return
        with self._start_lock:
            if self._threads_started:
                return
            self._start_spawner()
            self._start_idle_reaper()
            self._threads_started = True

    def _register(self, child: WarmChild) -> None:
        self._ensure_threads()
        with self._registry_lock:
            self._registry.append(child)

    def _unregister(self, child: WarmChild) -> None:
        with self._registry_lock:
            try:
                self._registry.remove(child)
            except ValueError:
                pass

    def _reaper_interval_sec(self) -> float:
        with self._registry_lock:
            ttls = [w.spec.idle_ttl_sec for w in self._registry if w.spec.idle_ttl_sec is not None]
        if not ttls:
            return 1.0
        return max(1.0, min(ttls))

    def _start_spawner(self) -> None:
        def _loop() -> None:
            # This thread must not exit except at quit. Children spawned here with
            # die_with_parent are SIGKILLed by the kernel when it does (PR_SET_PDEATHSIG
            # follows the creating thread).
            self._spawner_ident = threading.get_ident()
            while True:
                item = self._spawn_queue.get()
                if item is _SPAWN_SHUTDOWN:
                    return
                req: _SpawnRequest = item
                with req.lock:
                    if req.abandoned:
                        req.done.set()
                        continue
                try:
                    req.managed.start()
                except BaseException as exc:
                    req.error = exc
                finally:
                    req.finish_spawn()

        run_in_background(_loop, name="wa-warm-child-spawn", dedicated=True)

    def _start_idle_reaper(self) -> None:
        def _loop() -> None:
            while not self._reaper_stop.is_set():
                wait_sec = self._reaper_interval_sec()
                if self._reaper_stop.wait(wait_sec):
                    return
                self._evict_idle()

        run_in_background(_loop, name="wa-warm-child-idle-reaper", dedicated=True)

    def _evict_idle(self) -> None:
        with self._registry_lock:
            entries = list(self._registry)
        now = time.monotonic()
        for warm in entries:
            ttl = warm.spec.idle_ttl_sec
            if ttl is None or warm._retired:
                continue
            if not warm.is_alive():
                continue
            if now - warm._last_active < ttl:
                continue
            if not warm._io_lock.acquire(blocking=False):
                continue
            try:
                if warm._retired or not warm.is_alive():
                    continue
                if time.monotonic() - warm._last_active < ttl:
                    continue
                log.info("%s idle for >%.3fs; killing warm child", warm.spec.name, ttl)
                warm.kill()
            finally:
                warm._io_lock.release()

    def spawn(self, managed: ManagedSubprocess, timeout_sec: float | None = None) -> None:
        # PDEATHSIG follows the creating thread (see child_runtime.die_with_parent).
        # spawn waits under _spawn_gate with a bounded timeout and cleans up late spawns after shutdown.
        with self._spawn_gate:
            if self._shutdown:
                raise RuntimeError("Supervisor is shut down")
            self._ensure_threads()
            req = _SpawnRequest(managed)
            self._spawn_queue.put(req)

        wait_sec = SPAWN_READY_TIMEOUT_SEC if timeout_sec is None else timeout_sec
        if not req.done.wait(wait_sec) and req.give_up():
            raise RuntimeError("Supervisor spawn timed out")
        if req.error is not None:
            raise req.error

    def shutdown_all(self) -> None:
        with self._spawn_gate:
            self._shutdown = True
            if self._threads_started:
                self._reaper_stop.set()
                self._spawn_queue.put(_SPAWN_SHUTDOWN)
            with self._registry_lock:
                children = list(self._registry)
            for warm in children:
                try:
                    warm.abandon_for_quit()
                except Exception:
                    log.exception("Error abandoning warm child %s for quit", warm.spec.name)
        # Always call managed_subprocess.shutdown_all so STT/ACP/CDP peers die even without WarmChild threads.
        managed_subprocess.shutdown_all()


supervisor = process_global("warm_child_supervisor", Supervisor)


def _default_supervisor() -> Supervisor:
    """The module ``supervisor``. ``WarmChild.__init__`` cannot name it: its ``supervisor`` parameter shadows it."""
    return supervisor


__all__ = [
    "Child",
    "ChildSlot",
    "ChildSpec",
    "FailPhase",
    "FramedRequestOnFail",
    "Lease",
    "SPAWN_READY_TIMEOUT_SEC",
    "STDERR_LOG_CAP",
    "SpawnOutcome",
    "Supervisor",
    "WarmChild",
    "fail_phase_message",
    "framed_request_roundtrip",
    "read_ready_frame_during_io",
    "spawn_budget",
    "stderr_thread_name",
    "supervisor",
]
