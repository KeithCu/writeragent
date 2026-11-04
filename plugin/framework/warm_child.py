# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Process-wide supervisor for warm framed-stdio child workers."""

from __future__ import annotations

import logging
import pickle
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import IO, Any

from plugin.framework.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    IpcFrameError,
    IpcPartialFrameTimeout,
    pack_pickle_frame,
)
from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.framework.worker_pool import run_in_background

log = logging.getLogger(__name__)
_child_log = logging.getLogger("compute_service.worker")

SPAWN_READY_TIMEOUT_SEC = 15.0
_STDERR_SNIPPET = 500
_STDERR_LOG_CAP = 64 * 1024
MIN_REQUEST_SEC = 1.0
_MIN_SPAWN_REMAINING_SEC = 0.25
_OMIT_ID = object()
_SPAWN_SHUTDOWN = object()


def error_dict(code: str, error: str, *, req_id: Any = _OMIT_ID, message: str | None = None) -> dict[str, Any]:
    """Build ``{"status": "error", "code", "error"}``."""
    res: dict[str, Any] = {"status": "error", "code": code, "error": error}
    if req_id is not _OMIT_ID:
        res["id"] = req_id
    if message is not None:
        res["message"] = message
    return res


class Deadline:
    """Accept-time budget for a pool, or the worker clock started under its lock."""

    __slots__: tuple[str, ...] = ("budget_sec", "_end")
    budget_sec: float
    _end: float

    def __init__(self, timeout_sec: float, *, end: float | None = None) -> None:
        self.budget_sec = float(timeout_sec)
        self._end = float(end) if end is not None else time.monotonic() + self.budget_sec

    @classmethod
    def from_absolute(cls, requested_sec: float, end: float) -> Deadline:
        return cls(requested_sec, end=end)

    def left(self) -> float:
        return self._end - time.monotonic()

    def usable(self) -> float | None:
        remaining = self.left()
        if remaining <= 0 or self.budget_sec < MIN_REQUEST_SEC:
            return None
        if self.budget_sec > MIN_REQUEST_SEC and remaining < MIN_REQUEST_SEC:
            return None
        return remaining

    def run_for(self) -> float:
        return max(self.left(), MIN_REQUEST_SEC)


def spawn_budget(deadline: Deadline) -> float | None:
    """Seconds for a new handshake, or None when a child must not start."""
    remaining = deadline.usable()
    if remaining is None or remaining < _MIN_SPAWN_REMAINING_SEC:
        return None
    return remaining


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
        owner = self._io_owner
        if owner not in (None, me):
            owner = self._io_owner
            if owner not in (None, me):
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


def read_ready_frame_during_io(
    spawned: Child,
    proc: Any,
    timeout_sec: float,
    *,
    is_alive: Callable[[], bool],
    max_payload_bytes: int,
    unpacker: Callable[[bytes], Any] | None,
) -> tuple[str, Any]:
    """Read the spawn ready frame while *spawned* owns the pipes (between begin_io and end_io)."""
    if spawned.was_signaled():
        return ("interrupted", None)
    ready_data: Any = None
    if proc is not None and proc.stdout is not None:
        try:
            from plugin.framework import process_worker as pw

            ready_data = pw.read_pickle_frame(  # pyright: ignore[reportAttributeAccessIssue]
                proc.stdout,
                timeout_sec,
                is_alive=is_alive,
                max_payload_bytes=max_payload_bytes,
                require_dict=True,
                unpacker=unpacker,
            )
        except (subprocess.TimeoutExpired, IpcPartialFrameTimeout):
            return ("timeout", None)
    if spawned.was_signaled():
        return ("interrupted", None)
    # Only status "ready" counts. EOF or any other dict would mark the worker idle,
    # and the next execute would wait out the full timeout before EMPTY_RESPONSE.
    if not isinstance(ready_data, dict) or ready_data.get("status") != "ready":
        status = ready_data.get("status") if isinstance(ready_data, dict) else None
        return ("not_ready", status)
    return ("ready", ready_data)


def spawn_managed_child_with_handshake(
    managed: ManagedSubprocess,
    *,
    worker_name: str,
    worker_id: int,
    is_alive: Callable[[], bool],
    max_payload_bytes: int,
    unpacker: Callable[[bytes], Any] | None,
    start: Callable[[ManagedSubprocess], None],
    publish: Callable[[Child], bool],
    deadline: Deadline | None,
    cap_sec: float,
) -> tuple[str, Any, Child]:
    """Start *managed*, try *publish*, then read the spawn ready frame.

    Returns ``(tag, extra, spawned)``. *tag* is ``refused`` when *publish*
    returned False before any pipe I/O, ``budget_exhausted`` when the child was
    published but the post-start spawn budget is gone, or otherwise the tag
    from :func:`read_ready_frame_during_io`. Callers must pair ``begin_io``
    (inside this helper after publish) with ``end_io`` in their own ``finally``.
    """
    # Do not hold _lifecycle_lock across start() or the handshake read.
    # kill() takes that lock only to drop the pointer, and the
    # handshake can block for the spawn budget.
    start(managed)
    spawned = Child(managed, worker_name=worker_name, worker_id=worker_id)
    if not publish(spawned):
        # Only signal the unpublished child constructed in this call.
        spawned.signal_from()
        return ("refused", None, spawned)
    if deadline is None:
        timeout_sec = cap_sec
    else:
        spawn_for = spawn_budget(deadline)
        if spawn_for is None:
            return ("budget_exhausted", None, spawned)
        timeout_sec = min(cap_sec, spawn_for)
    proc = managed.popen
    spawned.begin_io()
    tag, extra = read_ready_frame_during_io(
        spawned,
        proc,
        timeout_sec,
        is_alive=is_alive,
        max_payload_bytes=max_payload_bytes,
        unpacker=unpacker,
    )
    return (tag, extra, spawned)


def framed_request_roundtrip(
    stdin: IO[bytes],
    stdout: IO[bytes],
    frame: bytes,
    deadline: Deadline,
    *,
    is_alive: Callable[[], bool],
    max_payload_bytes: int,
    unpacker: Callable[[bytes], Any] | None,
    on_fail: Callable[[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Write one packed frame and read the response dict under *deadline*."""
    from plugin.framework import process_worker as pw

    usable = deadline.usable()
    if usable is None:
        return on_fail("EXECUTION_TIMEOUT", "Execution budget exhausted.")
    try:
        pw.write_packed_frame_with_timeout(stdin, frame, usable, is_alive=is_alive)  # pyright: ignore[reportAttributeAccessIssue]
    except subprocess.TimeoutExpired:
        return on_fail("EXECUTION_TIMEOUT", "Write timed out.")
    except (OSError, TypeError, ValueError) as exc:
        return on_fail("WORKER_PIPE_BROKEN", f"Failed to send request: {exc}")
    remaining = deadline.left()
    if remaining <= 0:
        return on_fail("EXECUTION_TIMEOUT", "Execution budget exhausted.")
    try:
        resp = pw.read_pickle_frame(  # pyright: ignore[reportAttributeAccessIssue]
            stdout,
            remaining,
            is_alive=is_alive,
            max_payload_bytes=max_payload_bytes,
            unpacker=unpacker,
        )
    except (subprocess.TimeoutExpired, IpcPartialFrameTimeout):
        return on_fail("EXECUTION_TIMEOUT", "Read timed out.")
    except ValueError as exc:
        return on_fail("WORKER_IPC_ERROR", f"Invalid response frame: {exc}")
    except Exception as exc:
        return on_fail("WORKER_CRASHED", f"Worker error: {exc}")
    if not isinstance(resp, dict):
        return on_fail("EMPTY_RESPONSE", "No response returned from worker.")
    return resp


@dataclass(frozen=True)
class ChildSpec:
    """Everything the supervisor needs to spawn one warm child script."""

    name: str
    argv: list[str]
    env: dict[str, str]
    command_wrapper: Callable[..., list[str]] | None = None
    ready_timeout_sec: float = 20.0
    idle_ttl_sec: float | None = None
    stderr_tail_chars: int = _STDERR_LOG_CAP
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
    unpacker: Callable[[bytes], Any] | None = None
    # False keeps a missing interpreter as OSError (venv systemd-run retry depends on it).
    log_start_failure: bool = True


@dataclass(frozen=True)
class Death:
    returncode: int | None
    stderr_tail: str
    during: str


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

    def fail(self, code: str, msg: str) -> dict[str, Any]:
        return self._warm.fail_lease(code, msg)


class WarmChild:
    """One registered warm worker: spawn on the supervisor thread, framed IPC."""

    def __init__(
        self,
        spec: ChildSpec,
        *,
        supervisor: Supervisor | None = None,
        on_death: Callable[[Death], None] | None = None,
    ) -> None:
        self.spec: ChildSpec = spec
        self._supervisor: Supervisor = supervisor if supervisor is not None else get_supervisor()
        self._on_death: Callable[[Death], None] | None = on_death
        self._child: Child | None = None
        self._io_lock: threading.Lock = threading.Lock()
        self._lifecycle_lock: threading.Lock = threading.Lock()
        self._generation: int = 0
        self._last_active: float = time.monotonic()
        self._retired: bool = False
        self._killed_by_us: bool = False
        self._death_hook_fired: bool = False
        self._cancel_pending: bool = False
        self._supervisor._register(self)

    def is_alive(self) -> bool:
        child = self._child
        if child is None:
            return False
        try:
            return child.managed.is_alive()
        except Exception:
            return False

    def kill(self) -> None:
        self._killed_by_us = True
        child = self._child
        if child is None:
            with self._lifecycle_lock:
                self._cancel_pending = True
            return
        if not child.signal_from():
            return
        self._drop_child(child)

    def fail_lease(self, code: str, msg: str) -> dict[str, Any]:
        child = self._child
        if child is not None:
            self.kill()
            tail = child.diagnostic_tail()
            if tail:
                msg = f"{msg}\n{tail}"
        return error_dict(code, msg)

    def retire(self) -> None:
        with self._lifecycle_lock:
            self._retired = True
        self.kill()
        self._supervisor._unregister(self)

    @contextmanager
    def lease(self, deadline: Deadline, *, wait: float | None = None) -> Generator[Lease | dict[str, Any], None, None]:
        """Take the pipe lock, ensure a live child, yield a Lease or an error dict."""
        if wait is None:
            acquired = self._io_lock.acquire()
        elif wait == 0:
            acquired = self._io_lock.acquire(blocking=False)
        else:
            acquired = self._io_lock.acquire(timeout=wait)
        if not acquired:
            yield error_dict("PIPE_LOCK_BUSY", "Could not acquire warm child pipe lock.")
            return
        try:
            with self._lifecycle_lock:
                self._cancel_pending = False
            if self._supervisor.is_shutting_down() or self._retired:
                yield error_dict("SERVICE_SHUTDOWN", f"{self.spec.name} is shutting down.")
                return
            err = self._ensure_live(deadline)
            if err is not None:
                yield err
                return
            child = self._child
            assert child is not None
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
            child.begin_io()
            try:
                yield Lease(
                    stdin=stdin,
                    stdout=stdout,
                    pid=int(proc.pid),
                    generation=self._generation,
                    warm=self,
                )
            finally:
                if child.end_io():
                    self._drop_child(child)
                self._last_active = time.monotonic()
        finally:
            self._io_lock.release()

    def request(self, payload: dict[str, Any], deadline: Deadline) -> dict[str, Any]:
        with self.lease(deadline) as leased:
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

    def _drop_child(self, child: Child) -> None:
        with self._lifecycle_lock:
            if self._child is child:
                self._child = None

    def _stderr_name(self) -> str:
        parts = "".join(ch if ch.isalnum() else "-" for ch in self.spec.name.lower()).split("-")
        cleaned = "-".join(part for part in parts if part)
        if not cleaned:
            cleaned = "warm"
        return f"wa-{cleaned}-stderr"

    def _notify_unexpected_death(self, *, during: str) -> None:
        if self._death_hook_fired or self._killed_by_us:
            return
        child = self._child
        rc = None
        tail = ""
        if child is not None:
            popen = child.managed.popen
            if popen is not None:
                rc = popen.poll()
            tail = child.diagnostic_tail()
        death = Death(returncode=rc, stderr_tail=tail, during=during)
        self._death_hook_fired = True
        log.warning(
            "%s child died unexpectedly (returncode=%s, during=%s)%s",
            self.spec.name,
            rc,
            during,
            f" stderr={tail!r}" if tail else "",
        )
        if self._on_death is not None:
            self._on_death(death)

    def _ensure_live(self, deadline: Deadline) -> dict[str, Any] | None:
        child = self._child
        if child is not None and not self.is_alive():
            if not self._killed_by_us:
                self._notify_unexpected_death(during="idle")
            self._drop_child(child)
            child = None
        if child is not None:
            return None
        if self._retired:
            return error_dict("SERVICE_SHUTDOWN", f"{self.spec.name} is retired.")
        spawn_for = spawn_budget(deadline)
        if spawn_for is None:
            return error_dict("EXECUTION_TIMEOUT", "Insufficient time to spawn worker.")
        self._killed_by_us = False
        self._death_hook_fired = False
        cap_sec = min(self.spec.ready_timeout_sec, SPAWN_READY_TIMEOUT_SEC)
        managed = ManagedSubprocess(
            self.spec.argv,
            base_env=self.spec.env,
            env={},
            command_wrapper=self.spec.command_wrapper,
            scrub_env=False,
            optimize_pipes=True,
            stderr_tail_chars=self.spec.stderr_tail_chars,
            stderr_name=self._stderr_name(),
            label=self.spec.name,
            log_start_failure=self.spec.log_start_failure,
        )
        spawned: Child | None = None
        published = False
        refuse_error: dict[str, Any] | None = None

        def _publish(candidate: Child) -> bool:
            nonlocal published, refuse_error
            with self._lifecycle_lock:
                if self._retired or self._supervisor.is_shutting_down():
                    refuse_error = error_dict("SERVICE_SHUTDOWN", f"{self.spec.name} is shutting down.")
                    return False
                if self._cancel_pending:
                    refuse_error = error_dict("WORKER_SPAWN_FAILED", f"{self.spec.name} spawn was cancelled.")
                    return False
                if self._child is not None:
                    refuse_error = error_dict("WORKER_SPAWN_FAILED", f"{self.spec.name} already has a child.")
                    return False
                self._child = candidate
                published = True
                return True

        try:
            tag, extra, handshake_child = spawn_managed_child_with_handshake(
                managed,
                worker_name=self.spec.name,
                worker_id=0,
                is_alive=self.is_alive,
                max_payload_bytes=self.spec.max_payload_bytes,
                unpacker=self.spec.unpacker,
                start=self._supervisor.spawn,
                publish=_publish,
                deadline=deadline,
                cap_sec=cap_sec,
            )
            spawned = handshake_child
            if tag == "refused":
                assert refuse_error is not None
                return refuse_error
            if tag == "budget_exhausted":
                self.kill()
                return error_dict("EXECUTION_TIMEOUT", "Insufficient time to spawn worker.")
            try:
                if tag == "interrupted":
                    self._drop_child(spawned)
                    return error_dict("WORKER_SPAWN_FAILED", f"{self.spec.name} spawn was interrupted.")
                if tag == "not_ready":
                    self.kill()
                    status = extra
                    tail = spawned.diagnostic_tail()
                    msg = f"{self.spec.name} spawn handshake was not ready (status={status!r})"
                    if tail:
                        msg = f"{msg}\n{tail}"
                    return error_dict("WORKER_SPAWN_FAILED", msg)
                if tag == "timeout":
                    self.kill()
                    tail = spawned.diagnostic_tail()
                    msg = f"{self.spec.name} spawn handshake timed out"
                    if tail:
                        msg = f"{msg}\n{tail}"
                    return error_dict("EXECUTION_TIMEOUT", msg)
                self._generation += 1
                self._last_active = time.monotonic()
            finally:
                if spawned.end_io():
                    self._drop_child(spawned)
        except OSError as exc:
            if spawned is not None:
                if spawned.end_io():
                    self._drop_child(spawned)
            if not published:
                if spawned is not None:
                    spawned.signal_from()
                raise
            self.kill()
            tail = spawned.diagnostic_tail() if spawned is not None else ""
            msg = f"Failed to spawn {self.spec.name}: {exc}"
            if tail:
                msg = f"{msg}\n{tail}"
            return error_dict("WORKER_SPAWN_FAILED", msg)
        except Exception as exc:
            if spawned is not None:
                if spawned.end_io():
                    self._drop_child(spawned)
            if spawned is not None and not published:
                spawned.signal_from()
            elif published:
                self.kill()
            tail = spawned.diagnostic_tail() if spawned is not None else ""
            msg = f"Failed to spawn {self.spec.name}: {exc}"
            if tail:
                msg = f"{msg}\n{tail}"
            return error_dict("WORKER_SPAWN_FAILED", msg)
        return None


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
            self._spawner_ident = threading.get_ident()
            while True:
                item = self._spawn_queue.get()
                if item is _SPAWN_SHUTDOWN:
                    return
                managed, done, err_holder = item
                try:
                    managed.start()
                except BaseException as exc:
                    err_holder[0] = exc
                finally:
                    done.set()

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

    def spawn(self, managed: ManagedSubprocess) -> None:
        # PDEATHSIG follows the creating thread (see child_runtime.die_with_parent).
        if self._shutdown:
            raise RuntimeError("Supervisor is shut down")
        self._ensure_threads()
        done = threading.Event()
        err_holder: list[BaseException | None] = [None]
        self._spawn_queue.put((managed, done, err_holder))
        done.wait()
        if err_holder[0] is not None:
            raise err_holder[0]

    def shutdown_all(self) -> None:
        self._shutdown = True
        if self._threads_started:
            self._reaper_stop.set()
            self._spawn_queue.put(_SPAWN_SHUTDOWN)
            with self._registry_lock:
                children = list(self._registry)
            for warm in children:
                try:
                    warm.retire()
                except Exception:
                    log.exception("Error retiring warm child %s", warm.spec.name)
        from plugin.framework.managed_subprocess import shutdown_all as ms_shutdown_all

        # What was wrong: early return when the spawner never started skipped the managed sweep.
        # How it happened: managed_subprocess.shutdown_all lived only inside the threads guard.
        # Why this change: the terminate listener no longer calls managed_subprocess.shutdown_all;
        # registered peers (STT, ACP, CDP, tunnel AsyncProcess) must still die when no WarmChild
        # was ever built. _register starts threads before a child exists, so this unguarded call
        # is only for those peers when shutdown_all runs on an idle supervisor.
        ms_shutdown_all()


_module_supervisor: Supervisor | None = None


def get_supervisor() -> Supervisor:
    global _module_supervisor
    if _module_supervisor is None:
        _module_supervisor = Supervisor()
    return _module_supervisor


class _LazySupervisor:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_supervisor(), name)


supervisor = _LazySupervisor()

__all__ = [
    "Child",
    "ChildSpec",
    "Deadline",
    "Death",
    "Lease",
    "MIN_REQUEST_SEC",
    "SPAWN_READY_TIMEOUT_SEC",
    "Supervisor",
    "WarmChild",
    "error_dict",
    "framed_request_roundtrip",
    "get_supervisor",
    "read_ready_frame_during_io",
    "spawn_budget",
    "spawn_managed_child_with_handshake",
    "supervisor",
]
