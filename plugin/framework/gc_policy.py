# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Cyclic GC runs only on the LibreOffice main thread when the policy installs.

Scope: ``pythonloader`` is in ``sys.modules`` and AsyncCallback is available.
That means the UNO python loader hosted this process, not that VCL is present
(``uno.bin --singleaccept`` also loads pythonloader). URP clients such as
``testing_runner`` call the same bootstrap but keep automatic GC enabled.

Why: an automatic collection runs on whichever thread crosses the allocation
threshold. When it frees a cycle that holds a PyUNO proxy, PyUNO_del
(pyuno/source/module/pyuno.cxx) releases the office object, and the C++
destructor takes SolarMutex. If that worker is holding a Python lock the main
thread wants (logging._lock in Logger.isEnabledFor, a handler lock,
MainThread._claim_lock around Queue.put), the main thread, which is inside a
VCL callback that owns SolarMutex, waits forever. That is the mr1_t6 hang.

How: disable automatic GC once at bootstrap. A ticker thread watches the gen-0
count and posts _collect_on_main. That runs inside the AsyncCallback notify,
which already owns SolarMutex (recursive), so PyUNO_del cannot block. The
ticker itself never collects (GC is off) and only calls main.post.

Refcount frees on workers are safe only because workers must not own UNO refs
(see uno-thread-safety.md).
"""

from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import FrameType

log = logging.getLogger(__name__)

_TICK_SEC = 0.5
_FULL_EVERY_SEC = 60.0
_REPOST_AFTER_SEC = 5.0  # a tick dropped by Stop/cancel must not wedge us
_OFF_MAIN_SAMPLE_CAP = 16
_OFF_MAIN_STACK_LINES = 3
_STACK_FRAMES = 8

_main_ident: int | None = None
_posted_at = 0.0  # monotonic time of the outstanding post, 0 = none
_last_full = 0.0
_off_main_count = 0  # off-main collections seen by sentinel (all, not capped)
_off_main_samples: list[tuple[int, int, bool, tuple[tuple[str, int, str], ...]]] = []
_refused = 0  # _collect_on_main invoked off-main (AsyncCallback on wrong thread)
_reenabled = False  # ticker saw gc.enable() since last main collect


def install(executor: Any) -> bool:
    """Call once, on the main thread, right after executor.set_context(ctx).

    Skipped when pythonloader is not loaded or AsyncCallback is unavailable
    (eval harness, no VCL): automatic GC stays on as today.
    """
    global _main_ident
    if _main_ident is not None:
        return True
    # Cheap, import-time signal that the UNO python loader hosted us (not argv-
    # dependent). True for real soffice and uno.bin helpers; AsyncCallback below
    # is still required for a working main-thread collect path.
    if "pythonloader" not in sys.modules:
        log.info("gc_policy: skipped (pythonloader not loaded)")
        return False
    if not executor.has_async_callback():
        log.info("gc_policy: skipped (no AsyncCallback)")
        return False
    _main_ident = threading.get_ident()
    gc.disable()
    if hasattr(os, "register_at_fork"):  # absent on Windows
        # A forked child (multiprocessing 'fork') has no main-thread ticker.
        os.register_at_fork(after_in_child=gc.enable)
    gc.callbacks.append(_sentinel)
    from plugin.framework.worker_pool import run_in_background

    run_in_background(_ticker_loop, executor, name="gc-ticker", daemon=True, dedicated=True)
    log.info("gc_policy: active")
    return True


def _compact_stack() -> tuple[tuple[str, int, str], ...]:
    frames: list[tuple[str, int, str]] = []
    frame: FrameType | None = sys._getframe(1)
    while frame is not None and len(frames) < _STACK_FRAMES:
        code = frame.f_code
        frames.append((code.co_filename, frame.f_lineno, code.co_name))
        frame = frame.f_back
    return tuple(frames)


def _sentinel(phase: str, info: dict[str, Any]) -> None:
    # Runs inside the collection on the collecting thread: no logging, no locks.
    if phase == "start" and threading.get_ident() != _main_ident:
        global _off_main_count
        _off_main_count += 1
        if len(_off_main_samples) < _OFF_MAIN_SAMPLE_CAP:
            _off_main_samples.append(
                (threading.get_ident(), info["generation"], gc.isenabled(), _compact_stack())
            )


def _thread_name(ident: int) -> str:
    for thread in threading.enumerate():
        if thread.ident == ident:
            return thread.name
    return f"ident-{ident}"


def _log_off_main_samples() -> None:
    global _off_main_count, _off_main_samples
    if not _off_main_count:
        return
    log.warning("gc_policy: %d collection(s) ran off the main thread", _off_main_count)
    seen_stacks: set[tuple[tuple[str, int, str], ...]] = set()
    lines = 0
    for ident, generation, enabled, stack in _off_main_samples:
        if stack in seen_stacks:
            continue
        seen_stacks.add(stack)
        if lines >= _OFF_MAIN_STACK_LINES:
            break
        gc_state = "enabled=True" if enabled else "enabled=False"
        stack_text = " <- ".join(f"{name}:{lineno}({func})" for name, lineno, func in stack)
        log.warning(
            "gc_policy: off-main collector thread=%s gen=%s %s stack=%s",
            _thread_name(ident),
            generation,
            gc_state,
            stack_text,
        )
        lines += 1
    _off_main_count = 0
    _off_main_samples.clear()


def _due(now: float) -> bool:
    return gc.get_count()[0] >= gc.get_threshold()[0] or now - _last_full >= _FULL_EVERY_SEC


def _ticker_loop(executor: Any) -> None:
    while True:
        time.sleep(_TICK_SEC)
        _ticker_iteration(executor)


def _ticker_iteration(executor: Any, *, now: float | None = None) -> None:
    """One ticker wake (test seam). Supply *now* to avoid sleeping."""
    global _posted_at, _reenabled
    from plugin.framework import blocking_wait

    if gc.isenabled() and not blocking_wait._gc_suspend_depth:
        # Do not take blocking_wait._gc_suspend_lock here. While
        # automatic_gc_suspended is active, gc.enable() under that lock is
        # intentional; fighting it would set _reenabled and log a false warning.
        gc.disable()
        _reenabled = True
    if now is None:
        now = time.monotonic()
    if _posted_at and now - _posted_at < _REPOST_AFTER_SEC:
        return
    if not _due(now):
        return
    _posted_at = now
    try:
        executor.post(_collect_on_main, bound_scope=None)  # Stop must not drop it
    except Exception:
        _posted_at = 0.0  # TimeoutError etc.; retry next tick


def _collect_on_main() -> None:
    """Runs on the main thread under SolarMutex (AsyncCallback notify)."""
    global _posted_at, _last_full, _refused, _reenabled
    _posted_at = 0.0
    if threading.get_ident() != _main_ident:
        _refused += 1
        if _refused == 1:
            log.warning(
                "gc_policy: scheduled collection did not run on the main thread "
                "(AsyncCallback delivered off-main)"
            )
        return
    if _refused > 1:
        log.warning(
            "gc_policy: %d scheduled collection(s) did not run on the main thread",
            _refused,
        )
    _refused = 0
    if _reenabled or gc.isenabled():
        gc.disable()
        # Third-party gc.enable(), or automatic_gc_suspended restoring a state
        # recorded before install(). Put the policy back.
        log.warning("gc_policy: automatic GC was re-enabled; disabled again")
        _reenabled = False
    _log_off_main_samples()
    now = time.monotonic()
    if now - _last_full >= _FULL_EVERY_SEC:
        gc.collect()
        _last_full = now
    else:
        count, thr = gc.get_count(), gc.get_threshold()
        # Manual gc.collect(g) still bumps count[g+1] with GC disabled
        # (3.11-3.13 verified), so this matches CPython's own gen-1 schedule.
        # Gen 2 is time-based (above) instead of the long-lived heuristic.
        gc.collect(1 if count[1] >= thr[1] else 0)
