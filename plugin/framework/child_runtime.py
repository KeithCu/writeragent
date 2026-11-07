# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Child-side helpers for warm worker subprocesses (stdlib only).

``Deadline`` / ``MIN_REQUEST_SEC`` are the shared request clock (importable
without the compute host pool). ``error_dict`` and ``die_with_parent`` stay
stdlib-only. ``announce_ready`` is the only lazy import of ``ipc`` here.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import IO, Any

log = logging.getLogger(__name__)

MIN_REQUEST_SEC = 1.0
_OMIT_ID = object()
_prctl: Any | None = None
PR_SET_PDEATHSIG = 1
SIGKILL = 9


def bind_pdeathsig() -> None:
    """Resolve libc prctl once in the parent process before fork.

    Child preexec functions must not import modules or call dlopen, which
    can deadlock on internal locks held by other threads during fork.
    No-op on non-Linux platforms.
    """
    global _prctl
    if sys.platform != "linux" or _prctl is not None:
        return
    import ctypes

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    _prctl = libc.prctl


def _arm_pdeathsig() -> bool:
    """Arm PR_SET_PDEATHSIG in the current process.

    Assumes bind_pdeathsig() was called in the parent before fork.
    Delivers SIGKILL if the parent thread dies, and kills itself immediately
    if the parent died before prctl executed. No imports or logging here.
    """
    if _prctl is None:
        return False
    ppid = os.getppid()
    if _prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0) != 0:
        return False
    if os.getppid() != ppid:
        os.kill(os.getpid(), SIGKILL)
    return True


def error_dict(code: str, error: str, *, req_id: Any = _OMIT_ID, **extra: Any) -> dict[str, Any]:
    """Build ``{"status": "error", "code", "error"}``. Pass req_id to set ``id``, including None."""
    res: dict[str, Any] = {"status": "error", "code": code, "error": error}
    if req_id is not _OMIT_ID:
        res["id"] = req_id
    if extra:
        res.update(extra)
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


def ready_payload() -> dict[str, int | str]:
    return {"status": "ready", "pid": os.getpid()}


def announce_ready(stream: IO[bytes]) -> None:
    from plugin.framework.ipc import write_pickle_frame

    write_pickle_frame(stream, ready_payload())


def is_ready_payload(data: object) -> bool:
    return isinstance(data, dict) and data.get("status") == "ready"


def die_with_parent() -> bool:
    """Kill this child if the parent process dies while we are in native code.

    Linux ``PR_SET_PDEATHSIG`` follows the thread that created the process,
    not the process as a whole. The ppid check covers the parent dying
    before prctl runs. After the parent dies, ``getppid()`` is the
    subreaper, not 1. No-op on non-Linux.

    Returns True when PDEATHSIG was configured (Linux and prctl succeeded).
    """
    if sys.platform != "linux":
        return False
    try:
        bind_pdeathsig()
        return _arm_pdeathsig()
    except Exception as exc:
        log.debug("Failed to configure PR_SET_PDEATHSIG: %s", exc)
        return False


def pdeathsig_preexec() -> None:
    """Arm PR_SET_PDEATHSIG in the child process immediately before exec.

    Linux PR_SET_PDEATHSIG delivers the signal when the thread that created the
    process exits, not when the parent process as a whole exits. For this reason,
    processes arming this hook must be spawned on a long-lived thread such as
    Supervisor.spawn rather than short-lived timer or sync worker threads.
    """
    if sys.platform == "linux":
        _arm_pdeathsig()
