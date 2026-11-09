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
# One-way import: send_cancellation must not import this module (cycle). Call its getter; the ContextVar stays in that module.
from plugin.framework.send_cancellation import get_current_send_cancellation

if TYPE_CHECKING:
    from collections.abc import Generator

log = logging.getLogger(__name__)

_LLM_REQUEST_LOCK = threading.Lock()
_GRAMMAR_INFLIGHT_CV = threading.Condition(threading.Lock())
_GRAMMAR_INFLIGHT_COUNT = 0

# Stop is checked between acquires. Cap each acquire by the time left so a
# short timeout cannot block for a full poll slice.
_LLM_LANE_POLL_SEC = 0.25


@contextmanager
def llm_request_lane(timeout: float | None = None, status_callback: Callable[[str], None] | None = None, resume_status: str | None = None) -> Generator[None, None, None]:
    """Serialize LLM requests when callers choose to opt in.

    A single global lock exists for single-slot local servers (like Ollama or llama.cpp)
    that can only process one request at a time process-wide.

    `status_callback` must not block, marshal (`main.call`/`main.run`/`main.post`),
    or take a lock another thread can hold while waiting on this lane (this
    lane's lock, or SolarMutex via marshalling). Short leaf locks such as a
    queue's mutex are fine. Waiting text runs after a failed acquire, while this
    call does not hold the lock (another thread may). Resume text runs after
    this call has acquired the lock and before `yield`; it means the request is
    starting. The lock is not reentrant, so acquiring it from the callback
    deadlocks. Callers only enqueue a status item or log.
    """
    if timeout is None:
        from plugin.framework.config import get_config
        timeout = float(get_config("request_timeout") or 60)

    deadline = time.monotonic() + timeout
    notified_status = False

    while True:
        cancellation = get_current_send_cancellation()
        if cancellation and cancellation.is_cancelled():
            raise BlockingWaitStopped("stopped")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning("llm_request_lane timed out after %ss waiting for LLM lock", timeout)
            raise TimeoutError("Timed out waiting for LLM request lane lock after %ss" % timeout)
        if _LLM_REQUEST_LOCK.acquire(timeout=min(_LLM_LANE_POLL_SEC, remaining)):
            break
        if not notified_status and status_callback is not None:
            status_callback(_("Waiting for another document's reply..."))
            notified_status = True

    try:
        # Run resume callback inside try so exceptions do not leak the acquired lock.
        if notified_status and status_callback is not None:
            status_callback(resume_status or "")
        yield
    finally:
        _LLM_REQUEST_LOCK.release()


@contextmanager
def grammar_llm_request_gate(max_in_flight: int, timeout: float = 60.0) -> Generator[None, None, None]:
    """Gate grammar proofreader HTTP: limit=1 uses global lane; limit>1 allows N parallel grammar calls.

    Callers resolve the limit (Writer ``grammar_max_in_flight()``) and pass it in —
    this module must not import ``plugin.writer``.
    """
    if max_in_flight <= 1:
        # Intentional: share the global LLM lane so grammar yields to chat.
        # Local models (llama.cpp, Ollama) can only serve one request at a
        # time; concurrent calls would queue at the server or OOM the GPU.
        with llm_request_lane(timeout=timeout):
            yield
        return
    global _GRAMMAR_INFLIGHT_COUNT
    with _GRAMMAR_INFLIGHT_CV:
        # wait_for enforces a single overall deadline across wakeups.
        if not _GRAMMAR_INFLIGHT_CV.wait_for(lambda: _GRAMMAR_INFLIGHT_COUNT < max_in_flight, timeout):
            log.warning("grammar_llm_request_gate timed out after %ss waiting for slot", timeout)
            raise TimeoutError("Timed out waiting for grammar request gate slot after %ss" % timeout)
        _GRAMMAR_INFLIGHT_COUNT += 1
    try:
        yield
    finally:
        with _GRAMMAR_INFLIGHT_CV:
            _GRAMMAR_INFLIGHT_COUNT -= 1
            # One release frees one slot, so wake one waiter. notify_all() woke
            # every waiter; the extras rechecked the count and slept again.
            # CPython's condition deque is FIFO, so the oldest waiter runs.
            _GRAMMAR_INFLIGHT_CV.notify()
