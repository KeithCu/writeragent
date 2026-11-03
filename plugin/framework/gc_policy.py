# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Cyclic GC runs only on the LibreOffice main thread.

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

Refcount frees on workers are not changed. Workers must not own UNO refs
(see uno-thread-safety.md).
"""

from __future__ import annotations

import gc
import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

_TICK_SEC = 0.5
_FULL_EVERY_SEC = 60.0
_REPOST_AFTER_SEC = 5.0  # a tick dropped by Stop/cancel must not wedge us

_main_ident: int | None = None
_posted_at = 0.0  # monotonic time of the outstanding post, 0 = none
_last_full = 0.0
_off_main: list[int] = []  # thread idents seen collecting off-main (sentinel)


def install(executor: Any) -> bool:
    """Call once, on the main thread, right after executor.set_context(ctx).

    Skipped when AsyncCallback is unavailable (eval harness, no VCL): then
    nothing could run the collect job, and automatic GC stays on as today.
    """
    global _main_ident
    if _main_ident is not None:
        return True
    if executor._get_async_callback() is None:
        return False
    _main_ident = threading.get_ident()
    gc.disable()
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=gc.enable)
    gc.callbacks.append(_sentinel)
    from plugin.framework.worker_pool import run_in_background

    run_in_background(_ticker_loop, executor, name="gc-ticker", daemon=True, dedicated=True)
    return True


def _sentinel(phase: str, info: dict[str, Any]) -> None:
    # Runs inside the collection on the collecting thread: no logging, no locks.
    if phase == "start" and threading.get_ident() != _main_ident:
        _off_main.append(threading.get_ident())


def _due(now: float) -> bool:
    return gc.get_count()[0] >= gc.get_threshold()[0] or now - _last_full >= _FULL_EVERY_SEC


def _ticker_loop(executor: Any) -> None:
    while True:
        time.sleep(_TICK_SEC)
        _ticker_iteration(executor)


def _ticker_iteration(executor: Any, *, now: float | None = None) -> None:
    """One ticker wake (test seam). Supply *now* to avoid sleeping."""
    global _posted_at
    if now is None:
        now = time.monotonic()
    if _posted_at and now - _posted_at < _REPOST_AFTER_SEC:
        return
    if not _due(now):
        return
    _posted_at = now
    try:
        executor.post(_collect_on_main, bound_scope=None)
    except Exception:
        _posted_at = 0.0


def _collect_on_main() -> None:
    """Runs on the main thread under SolarMutex (AsyncCallback notify)."""
    global _posted_at, _last_full
    _posted_at = 0.0
    if gc.isenabled():
        gc.disable()
        log.warning("gc_policy: automatic GC was re-enabled; disabled again")
    if _off_main:
        log.warning("gc_policy: %d collection(s) ran off the main thread", len(_off_main))
        _off_main.clear()
    now = time.monotonic()
    if now - _last_full >= _FULL_EVERY_SEC:
        gc.collect()
        _last_full = now
    else:
        count, thr = gc.get_count(), gc.get_threshold()
        gc.collect(1 if count[1] >= thr[1] else 0)
