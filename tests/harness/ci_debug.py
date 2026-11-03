# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in pytest hang diagnostics for CI (WRITERAGENT_CI_DEBUG=1).

Off by default. When enabled, each xdist worker (or the controller) appends a
flushed ``start <nodeid>`` / ``end <nodeid>`` trail and schedules a
faulthandler dump at 240s. Windows CI 33447705893 lost gw3 at 261s with no
``Failed: Timeout`` line — pytest-timeout did not fire (thread method cannot
abort a native/subprocess block). Dump while the worker is still alive so a
later unclean death still names the hung test and dumps every thread's stack.

``arm_stderr_hang_dump`` is separate: opt-in CI stacks after 90s
(GHA 33703959362). Native tests use a silent 30s ``testing_runner`` abort.

This does not fix the Windows hang. ``--max-worker-restart=0`` (Makefile,
same flag) is what lets the session summary print
``worker 'gw3' crashed while running '<nodeid>'`` instead of replacing the
worker and wedging on re-collection.
"""

from __future__ import annotations

import faulthandler
import os
import sys
from typing import IO, Optional

# Observed hang-to-crash is ~261s (under --timeout=300, which did not fire).
# Dump at 240s while the worker is still alive. pytest-timeout only bounds a
# test function; it does not cover collection, sessionstart, or the xdist
# controller wait after "replacing crashed worker".
_FAULT_DUMP_SECONDS = 240

# GHA 33703959362: execute-done then 20 min silence until the 25m step died.
# Dump well before that timeout. dump_traceback_later is a watchdog thread on
# Windows (no gdb / cdb). Must write stderr — file-only log.info never reached
# Actions on 33699746211.
STDERR_HANG_DUMP_SECONDS = 90

_log_fh: Optional[IO[str]] = None
_stderr_hang_dump_armed = False


def ci_debug_enabled() -> bool:
    return os.environ.get("WRITERAGENT_CI_DEBUG") == "1"


def ci_debug_dir() -> str:
    return os.environ.get("WRITERAGENT_CI_DEBUG_DIR") or os.path.join("build", "ci-debug")


def ci_debug_worker_id() -> str:
    return os.environ.get("PYTEST_XDIST_WORKER") or "controller"


def ci_debug_log_path(directory: str | None = None, worker_id: str | None = None) -> str:
    return os.path.join(directory or ci_debug_dir(), f"{worker_id or ci_debug_worker_id()}.log")


def write_ci_debug_line(fh: IO[str], line: str) -> None:
    """Append one line and flush so a SIGKILL cannot lose the tail."""
    fh.write(line + "\n")
    fh.flush()
    try:
        os.fsync(fh.fileno())
    except OSError:
        pass


def start_ci_debug() -> Optional[IO[str]]:
    """Open the per-worker log and arm faulthandler. Idempotent."""
    global _log_fh
    if not ci_debug_enabled():
        return None
    if _log_fh is not None:
        return _log_fh
    directory = ci_debug_dir()
    os.makedirs(directory, exist_ok=True)
    _log_fh = open(ci_debug_log_path(directory), "a", encoding="utf-8", buffering=1)
    write_ci_debug_line(_log_fh, f"session pid={os.getpid()} worker={ci_debug_worker_id()}")
    faulthandler.dump_traceback_later(
        _FAULT_DUMP_SECONDS,
        repeat=True,
        exit=False,
        file=_log_fh,
    )
    return _log_fh


def log_ci_debug(line: str) -> None:
    if _log_fh is None:
        return
    write_ci_debug_line(_log_fh, line)


def stop_ci_debug() -> None:
    """Cancel the watchdog and close the log. Safe if start was never called."""
    global _log_fh
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    if _log_fh is not None:
        try:
            _log_fh.close()
        except Exception:
            pass
        _log_fh = None


def arm_stderr_hang_dump(timeout: float | None = None, *, label: str = "") -> None:
    """Dump every Python thread to stderr if still running after *timeout*.

    Always on (not gated on ``WRITERAGENT_CI_DEBUG``). Replaces any prior
    ``dump_traceback_later`` timer. In-process stacks only — no native debugger.
    """
    global _stderr_hang_dump_armed
    seconds = STDERR_HANG_DUMP_SECONDS if timeout is None else timeout
    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
    except Exception:
        pass
    faulthandler.dump_traceback_later(
        seconds,
        repeat=True,
        exit=False,
        file=sys.stderr,
    )
    _stderr_hang_dump_armed = True
    print(
        "hang dump armed timeout=%ss test=%s all_threads=True" % (seconds, label or "-"),
        file=sys.stderr,
        flush=True,
    )


def cancel_stderr_hang_dump(*, label: str = "") -> None:
    """Cancel the stderr watchdog. Quiet if nothing was armed (pytest fixtures)."""
    global _stderr_hang_dump_armed
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    was_armed = _stderr_hang_dump_armed
    _stderr_hang_dump_armed = False
    if was_armed or label:
        print(
            "hang dump disarmed test=%s" % (label or "-"),
            file=sys.stderr,
            flush=True,
        )
