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
    read_pickle_frame,
    write_packed_frame_with_timeout,
)
from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.framework.process_worker import (
    Child,
    Deadline,
    _SPAWN_READY_TIMEOUT_SEC,
    _spawn_budget,
    error_dict,
)
from plugin.framework.worker_pool import run_in_background

log = logging.getLogger("compute_service.worker")

_SPAWN_STOP = object()
_STDERR_LOG_CAP = 64 * 1024


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
        child = self._warm._child
        if child is not None:
            self._warm._killed_by_us = True
            self._warm.kill()
            tail = child.diagnostic_tail()
            if tail:
                msg = f"{msg}\n{tail}"
        return error_dict(code, msg)


class WarmChild:
    """One registered warm worker: spawn on the supervisor thread, framed IPC."""

    def __init__(self, spec: ChildSpec, *, on_death: Callable[[Death], None] | None = None) -> None:
        self.spec: ChildSpec = spec
        self._on_death: Callable[[Death], None] | None = on_death
        self._child: Child | None = None
        self._io_lock: threading.Lock = threading.Lock()
        self._lifecycle_lock: threading.Lock = threading.Lock()
        self._generation: int = 0
        self._last_active: float = time.monotonic()
        self._leased: bool = False
        self._retired: bool = False
        self._killed_by_us: bool = False
        self._death_hook_fired: bool = False
        self._handshaking: bool = False
        supervisor._register(self)

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
            return
        if not child.signal_from():
            return
        self._drop_child(child)

    def retire(self) -> None:
        with self._lifecycle_lock:
            self._retired = True
        self.kill()
        supervisor._unregister(self)

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
            yield error_dict("EXECUTION_TIMEOUT", "Could not acquire warm child pipe lock.")
            return
        try:
            if supervisor.is_shutting_down() or self._retired:
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
            self._leased = True
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
                self._leased = False
                if child.end_io():
                    self._drop_child(child)
                self._last_active = time.monotonic()
        finally:
            self._io_lock.release()

    def request(self, payload: dict[str, Any], deadline: Deadline) -> dict[str, Any]:
        with self.lease(deadline) as leased:
            if isinstance(leased, dict):
                return leased
            usable = deadline.usable()
            if usable is None:
                return error_dict("EXECUTION_TIMEOUT", "Execution budget exhausted.")
            try:
                frame = pack_pickle_frame(payload, max_payload_bytes=self.spec.max_payload_bytes)
            except IpcFrameError as exc:
                return error_dict("PAYLOAD_TOO_LARGE", str(exc))
            except (pickle.PicklingError, TypeError, ValueError, RecursionError) as exc:
                return error_dict("REQUEST_NOT_SERIALIZABLE", str(exc))
            try:
                write_packed_frame_with_timeout(
                    leased.stdin,
                    frame,
                    usable,
                    is_alive=self.is_alive,
                )
            except subprocess.TimeoutExpired:
                return leased.fail("EXECUTION_TIMEOUT", "Write timed out.")
            except (OSError, TypeError, ValueError) as exc:
                return leased.fail("WORKER_PIPE_BROKEN", f"Failed to send request: {exc}")
            remaining = deadline.left()
            if remaining <= 0:
                return leased.fail("EXECUTION_TIMEOUT", "Execution budget exhausted.")
            try:
                resp = read_pickle_frame(
                    leased.stdout,
                    remaining,
                    is_alive=self.is_alive,
                    max_payload_bytes=self.spec.max_payload_bytes,
                    unpacker=self.spec.unpacker,
                )
            except (subprocess.TimeoutExpired, IpcPartialFrameTimeout):
                return leased.fail("EXECUTION_TIMEOUT", "Read timed out.")
            except ValueError as exc:
                return leased.fail("WORKER_IPC_ERROR", f"Invalid response frame: {exc}")
            except Exception as exc:
                return leased.fail("WORKER_CRASHED", f"Worker error: {exc}")
            if not isinstance(resp, dict):
                return leased.fail("EMPTY_RESPONSE", "No response returned from worker.")
            return resp

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
        spawn_for = _spawn_budget(deadline)
        if spawn_for is None:
            return error_dict("EXECUTION_TIMEOUT", "Insufficient time to spawn worker.")
        self._killed_by_us = False
        self._death_hook_fired = False
        timeout_sec = min(self.spec.ready_timeout_sec, _SPAWN_READY_TIMEOUT_SEC, spawn_for)
        managed = ManagedSubprocess(
            self.spec.argv,
            base_env=None,
            env=self.spec.env,
            command_wrapper=self.spec.command_wrapper,
            scrub_env=True,
            optimize_pipes=True,
            stderr_tail_chars=self.spec.stderr_tail_chars,
            stderr_name=self._stderr_name(),
            label=self.spec.name,
        )
        spawned: Child | None = None
        published = False
        self._handshaking = True
        try:
            supervisor.spawn(managed)
            spawned = Child(
                managed,
                worker_name=self.spec.name,
                worker_id=0,
            )
            with self._lifecycle_lock:
                if self._retired or supervisor.is_shutting_down():
                    spawned.signal_from()
                    return error_dict("SERVICE_SHUTDOWN", f"{self.spec.name} is shutting down.")
                if self._child is not None:
                    spawned.signal_from()
                    return error_dict("WORKER_SPAWN_FAILED", f"{self.spec.name} already has a child.")
                self._child = spawned
                published = True
            proc = managed.popen
            spawned.begin_io()
            try:
                ready_data: Any = None
                if proc is not None and proc.stdout is not None:
                    ready_data = read_pickle_frame(
                        proc.stdout,
                        timeout_sec,
                        is_alive=self.is_alive,
                        max_payload_bytes=self.spec.max_payload_bytes,
                        require_dict=True,
                        unpacker=self.spec.unpacker,
                    )
                if spawned._signaled:
                    self._drop_child(spawned)
                    return error_dict("WORKER_SPAWN_FAILED", f"{self.spec.name} spawn was interrupted.")
                if not isinstance(ready_data, dict) or ready_data.get("status") != "ready":
                    status = ready_data.get("status") if isinstance(ready_data, dict) else None
                    self._killed_by_us = True
                    self.kill()
                    tail = spawned.diagnostic_tail()
                    msg = f"{self.spec.name} spawn handshake was not ready (status={status!r})"
                    if tail:
                        msg = f"{msg}\n{tail}"
                    return error_dict("WORKER_SPAWN_FAILED", msg)
                self._generation += 1
                self._last_active = time.monotonic()
            except (subprocess.TimeoutExpired, IpcPartialFrameTimeout):
                self._killed_by_us = True
                self.kill()
                tail = spawned.diagnostic_tail() if spawned is not None else ""
                msg = f"{self.spec.name} spawn handshake timed out"
                if tail:
                    msg = f"{msg}\n{tail}"
                return error_dict("EXECUTION_TIMEOUT", msg)
            finally:
                if spawned is not None:
                    if spawned.end_io():
                        self._drop_child(spawned)
        except Exception as exc:
            if spawned is not None and not published:
                spawned.signal_from()
            elif published:
                self._killed_by_us = True
                self.kill()
            tail = spawned.diagnostic_tail() if spawned is not None else ""
            msg = f"Failed to spawn {self.spec.name}: {exc}"
            if tail:
                msg = f"{msg}\n{tail}"
            return error_dict("WORKER_SPAWN_FAILED", msg)
        finally:
            self._handshaking = False
        return None


class Supervisor:
    """One spawner thread, registry, idle reaper, and shutdown for warm children."""

    def __init__(self) -> None:
        self._registry_lock: threading.Lock = threading.Lock()
        self._registry: list[WarmChild] = []
        self._shutdown: bool = False
        self._spawn_queue: queue.Queue[Any] = queue.Queue()
        self._spawner_ident: int | None = None
        self._reaper_stop: threading.Event = threading.Event()
        self._start_spawner()
        self._start_idle_reaper()

    @property
    def spawner_ident(self) -> int | None:
        return self._spawner_ident

    def is_shutting_down(self) -> bool:
        return self._shutdown

    def _register(self, child: WarmChild) -> None:
        with self._registry_lock:
            self._registry.append(child)

    def _unregister(self, child: WarmChild) -> None:
        with self._registry_lock:
            try:
                self._registry.remove(child)
            except ValueError:
                pass

    def _start_spawner(self) -> None:
        def _loop() -> None:
            self._spawner_ident = threading.get_ident()
            while True:
                item = self._spawn_queue.get()
                if item is _SPAWN_STOP:
                    continue
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
                if self._reaper_stop.wait(0.1):
                    return
                self._evict_idle()

        run_in_background(_loop, name="wa-warm-child-idle-reaper", dedicated=True)

    def _evict_idle(self) -> None:
        with self._registry_lock:
            entries = list(self._registry)
        now = time.monotonic()
        for warm in entries:
            ttl = warm.spec.idle_ttl_sec
            if ttl is None or warm._retired or warm._leased or warm._handshaking:
                continue
            if not warm.is_alive():
                continue
            if now - warm._last_active < ttl:
                continue
            log.info("%s idle for >%.3fs; killing warm child", warm.spec.name, ttl)
            warm._killed_by_us = True
            warm.kill()

    def spawn(self, managed: ManagedSubprocess) -> None:
        if self._shutdown:
            raise RuntimeError("Supervisor is shut down")
        done = threading.Event()
        err_holder: list[BaseException | None] = [None]
        self._spawn_queue.put((managed, done, err_holder))
        done.wait()
        if err_holder[0] is not None:
            raise err_holder[0]

    def shutdown_all(self) -> None:
        self._shutdown = True
        self._reaper_stop.set()
        with self._registry_lock:
            children = list(self._registry)
        for warm in children:
            try:
                warm.retire()
            except Exception:
                log.exception("Error retiring warm child %s", warm.spec.name)
        from plugin.framework.managed_subprocess import shutdown_all as ms_shutdown_all

        ms_shutdown_all()


supervisor = Supervisor()

__all__ = [
    "ChildSpec",
    "Death",
    "Lease",
    "Supervisor",
    "WarmChild",
    "supervisor",
]
