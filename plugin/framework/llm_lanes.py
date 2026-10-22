# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""LLM request lane and concurrency gates.

Serializes HTTP requests to local or concurrency-limited LLM providers.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable

from plugin.framework.errors import BlockingWaitStopped
from plugin.framework.i18n import _
# One-way: queue_executor must not import this module. The moved functions
# look up get_current_send_cancellation in this module's globals; the function
# object still reads queue_executor's ContextVar via its own __globals__.
from plugin.framework.queue_executor import get_current_send_cancellation

if TYPE_CHECKING:
    from collections.abc import Generator

# Same logger name as before the move so warning records do not change logger.
log = logging.getLogger("writeragent.framework.queue_executor")

_LLM_REQUEST_LOCK = threading.Lock()
_GRAMMAR_INFLIGHT_LOCK = threading.Lock()
_GRAMMAR_INFLIGHT_CV = threading.Condition(_GRAMMAR_INFLIGHT_LOCK)
_GRAMMAR_INFLIGHT_COUNT = 0

# Stop is checked between acquires. Cap each acquire by the time left so a
# short timeout cannot block for a full poll slice.
_LLM_LANE_POLL_SEC = 0.25


@contextmanager
def llm_request_lane(timeout: float | None = None, status_callback: Callable[[str], None] | None = None, resume_status: str | None = None) -> Generator[None, None, None]:
    """Serialize LLM requests when callers choose to opt in.

    A single global lock exists for single-slot local servers (like Ollama or llama.cpp)
    that can only process one request at a time process-wide.
    """
    if timeout is None:
        from plugin.framework.config import get_config_int
        timeout = float(get_config_int("request_timeout") or 60)

    deadline = time.monotonic() + timeout
    acquired = False
    notified_status = False

    while True:
        cancellation = get_current_send_cancellation()
        if cancellation and cancellation.is_cancelled():
            raise BlockingWaitStopped("stopped")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if _LLM_REQUEST_LOCK.acquire(timeout=min(_LLM_LANE_POLL_SEC, remaining)):
            acquired = True
            break
        if not notified_status and status_callback is not None:
            status_callback(_("Waiting for another document's reply..."))
            notified_status = True

    if not acquired:
        log.warning("llm_request_lane timed out after %ss waiting for LLM lock", timeout)
        raise TimeoutError("Timed out waiting for LLM request lane lock after %ss" % timeout)

    try:
        # Bugfix: the resume status_callback ran after acquire and before this
        # try. A raise skipped _LLM_REQUEST_LOCK.release(), and every later
        # llm_request_lane wait (chat, grammar limit 1, compaction) timed out
        # for the rest of the process. The callback stays inside the hold so
        # another request cannot pass it; finally still releases on that raise,
        # and the with-body does not run.
        if notified_status and status_callback is not None:
            status_callback(resume_status or "")
        yield
    finally:
        _LLM_REQUEST_LOCK.release()


@contextmanager
def grammar_llm_request_gate(max_in_flight: int, timeout: float = 60.0) -> Generator[None, None, None]:
    """Gate grammar proofreader HTTP: limit=1 uses global lane; limit>1 allows N parallel grammar calls.

    Callers resolve the limit (Writer ``grammar_max_in_flight(ctx)``) and pass it in —
    this module must not import ``plugin.writer``.
    """
    limit = max_in_flight
    if limit <= 1:
        # Intentional: share the global LLM lane so grammar yields to chat.
        # Local models (llama.cpp, Ollama) can only serve one request at a
        # time; concurrent calls would queue at the server or OOM the GPU.
        with llm_request_lane(timeout=timeout):
            yield
        return
    global _GRAMMAR_INFLIGHT_COUNT
    deadline = time.monotonic() + timeout
    with _GRAMMAR_INFLIGHT_CV:
        # Bugfix: wait(timeout=timeout) restarted the full timeout whenever
        # a wakeup still saw a full gate (spurious wake, or a new caller
        # that took the slot before this waiter reacquired). The advertised
        # bound is one deadline from entry. A wakeup that finds a free slot
        # still takes it.
        while _GRAMMAR_INFLIGHT_COUNT >= limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not _GRAMMAR_INFLIGHT_CV.wait(timeout=remaining):
                log.warning("grammar_llm_request_gate timed out after %ss waiting for slot", timeout)
                raise TimeoutError("Timed out waiting for grammar request gate slot after %ss" % timeout)
        _GRAMMAR_INFLIGHT_COUNT += 1
    try:
        yield
    finally:
        with _GRAMMAR_INFLIGHT_CV:
            _GRAMMAR_INFLIGHT_COUNT = max(0, _GRAMMAR_INFLIGHT_COUNT - 1)
            # One release frees one slot, so wake one waiter. notify_all() woke
            # every waiter; the extras rechecked the count and slept again.
            # CPython's condition deque is FIFO, so the oldest waiter runs.
            _GRAMMAR_INFLIGHT_CV.notify()
