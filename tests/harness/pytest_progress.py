# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pytest progress and idle-heartbeat helpers for Make / CI runs."""

from __future__ import annotations

import os
import sys
import threading
import time

_pytest_progress_done = 0
_pytest_progress_last_nodeid = ""
_pytest_progress_inflight: set[str] = set()
_pytest_progress_last_emit = 0.0
_pytest_progress_stop: threading.Event | None = None
_pytest_progress_thread: threading.Thread | None = None

_PYTEST_PROGRESS_TAIL_AFTER = 7800
_PYTEST_PROGRESS_IDLE_SEC = 15.0
_PYTEST_PROGRESS_IDLE_LEFTOVER_CAP = 6


def format_idle_pytest_progress(done: int, last_nodeid: str, inflight: list[str]) -> str:
    """Idle heartbeat. leftover= is started-not-finished; last= is last reported."""
    if inflight:
        shown = ",".join(inflight[:_PYTEST_PROGRESS_IDLE_LEFTOVER_CAP])
        extra = len(inflight) - _PYTEST_PROGRESS_IDLE_LEFTOVER_CAP
        more = f" +{extra}" if extra > 0 else ""
        return f"pytest: {done} still-running leftover={shown}{more}"
    return f"pytest: {done} still-running last={last_nodeid or '-'}"


def should_emit_pytest_progress_count(done: int, *, failed: bool = False) -> bool:
    """When to print a ``pytest: N`` count heartbeat.

    Every 100 keeps the log small. After ``_PYTEST_PROGRESS_TAIL_AFTER`` also
    emit every 10 so the last 1% is not a silent gap.
    """
    if failed:
        return True
    if done <= 0:
        return False
    if done % 100 == 0:
        return True
    return done >= _PYTEST_PROGRESS_TAIL_AFTER and done % 10 == 0


def _emit_make_pytest_progress(msg: str) -> None:
    """Full lines on stderr. Pytest/xdist otherwise rewrite one ``\\r`` status line.

    Prefix a newline so the message is not glued onto a row of unwrapped dots
    when stdout and stderr are merged (Make ``2>&1``, some IDE captures).
    """
    global _pytest_progress_last_emit
    sys.stderr.write("\n" + msg + "\n")
    sys.stderr.flush()
    _pytest_progress_last_emit = time.monotonic()


def _make_pytest_progress_enabled() -> bool:
    return os.environ.get("WRITERAGENT_PYTEST_PROGRESS") == "1" and not os.environ.get(
        "PYTEST_XDIST_WORKER"
    )


def _idle_pytest_progress_loop() -> None:
    """Re-announce the last count if the tail goes quiet (one slow leftover test)."""
    stop = _pytest_progress_stop
    if stop is None:
        return
    while not stop.wait(_PYTEST_PROGRESS_IDLE_SEC):
        done = _pytest_progress_done
        nodeid = _pytest_progress_last_nodeid
        last = _pytest_progress_last_emit
        if done and (time.monotonic() - last) >= _PYTEST_PROGRESS_IDLE_SEC:
            leftover = sorted(_pytest_progress_inflight)
            _emit_make_pytest_progress(
                format_idle_pytest_progress(done, nodeid, leftover)
            )


def _start_idle_pytest_progress() -> None:
    global _pytest_progress_stop, _pytest_progress_thread
    _stop_idle_pytest_progress()
    _pytest_progress_stop = threading.Event()
    _pytest_progress_thread = threading.Thread(
        target=_idle_pytest_progress_loop,
        name="pytest-progress-idle",
        daemon=True,
    )
    _pytest_progress_thread.start()


def _stop_idle_pytest_progress() -> None:
    global _pytest_progress_stop, _pytest_progress_thread
    stop = _pytest_progress_stop
    if stop is not None:
        stop.set()
    thread = _pytest_progress_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=1.0)
    _pytest_progress_stop = None
    _pytest_progress_thread = None


def on_runtest_logstart(nodeid: str) -> None:
    """Record a started test nodeid so idle heartbeats name inflight tests."""
    global _pytest_progress_last_nodeid
    if _make_pytest_progress_enabled():
        # Remember the name for the 15s idle line without printing every start,
        # which can flood GHA controller log I/O during rapid parametrized tails.
        _pytest_progress_last_nodeid = nodeid
        _pytest_progress_inflight.add(nodeid)


def on_runtest_logfinish(nodeid: str) -> None:
    """Drop a finished test nodeid from the inflight set."""
    _pytest_progress_inflight.discard(nodeid)


def on_sessionstart() -> None:
    """Reset counters and start the idle heartbeat thread on the controller."""
    if _make_pytest_progress_enabled():
        global _pytest_progress_done, _pytest_progress_last_nodeid
        _pytest_progress_done = 0
        _pytest_progress_last_nodeid = ""
        _pytest_progress_inflight.clear()
        _emit_make_pytest_progress("pytest: starting (workers collecting…)")
        _start_idle_pytest_progress()


def on_collection_finish(item_count: int) -> None:
    """Emit the collected test count when progress output is enabled."""
    if _make_pytest_progress_enabled():
        _emit_make_pytest_progress(f"pytest: collected {item_count} tests")


def on_runtest_logreport(report: object) -> None:
    """Emit periodic count or failure heartbeats as test calls complete."""
    global _pytest_progress_done, _pytest_progress_last_nodeid
    if not _make_pytest_progress_enabled():
        return
    if getattr(report, "when", None) != "call":
        return
    _pytest_progress_done += 1
    nodeid = getattr(report, "nodeid", "")
    _pytest_progress_last_nodeid = nodeid
    failed = bool(getattr(report, "failed", False))
    if should_emit_pytest_progress_count(_pytest_progress_done, failed=failed):
        if failed:
            suffix = f" FAIL {nodeid}"
        elif _pytest_progress_done % 100 == 0:
            suffix = ""
        else:
            suffix = f" last={nodeid}"
        _emit_make_pytest_progress(f"pytest: {_pytest_progress_done}{suffix}")


def on_sessionfinish(exitstatus: int) -> None:
    """Stop the idle heartbeat thread and emit the final done summary."""
    if _make_pytest_progress_enabled():
        _stop_idle_pytest_progress()
        _emit_make_pytest_progress(
            f"pytest: done {_pytest_progress_done} exit={exitstatus}"
        )
