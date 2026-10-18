# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Producer-side batcher for chat display text.

``CHUNK`` and ``THINKING`` are joined on a 250 ms burst timer. Every other
``StreamQueueKind`` flushes first. The drain loop never names this class.
Stop reaches it through ``flush_pending`` on ``run_async_worker_with_drain``.

``StreamQueueKind`` lives here so this module does not import ``async_stream``.
Callers import ``StreamQueueKind`` from this module.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import queue

from plugin.framework.worker_pool import run_in_background

log = logging.getLogger(__name__)


class StreamQueueKind(str, Enum):
    """First element of stream queue tuples (producers must use these enum members)."""

    CHUNK = "chunk"
    THINKING = "thinking"
    STATUS = "status"
    STREAM_DONE = "stream_done"
    NEXT_TOOL = "next_tool"
    TOOL_DONE = "tool_done"
    TOOL_THINKING = "tool_thinking"
    APPROVAL_REQUIRED = "approval_required"
    FINAL_DONE = "final_done"
    STOPPED = "stopped"
    ERROR = "error"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


class _DelayedPokeTimer:
    """One reusable thread waiting on a Condition until a monotonic deadline."""

    _name: str
    _replace: bool
    _retry: bool
    _logger: logging.Logger
    _max_failures: Callable[[], int] | None
    _retry_delay: Callable[[int], float] | None
    _cv: threading.Condition
    _deadline: float | None
    _fire: Callable[[], None] | None
    _on_give_up: Callable[[], None] | None
    _generation: int
    _failures: int
    _started: bool
    _stopped: bool

    def __init__(
        self,
        name: str,
        *,
        replace: bool,
        retry: bool,
        logger: logging.Logger,
        max_failures: Callable[[], int] | None = None,
        retry_delay: Callable[[int], float] | None = None,
    ) -> None:
        # crosshair: off
        self._name = name
        self._replace = replace
        self._retry = retry
        self._logger = logger
        self._max_failures = max_failures
        self._retry_delay = retry_delay

        self._cv = threading.Condition()
        self._deadline = None
        self._fire = None
        self._on_give_up = None
        self._generation = 0
        self._failures = 0
        self._started = False
        self._stopped = False

    def arm(
        self,
        delay: float,
        fire: Callable[[], None] | None = None,
        on_give_up: Callable[[], None] | None = None,
    ) -> None:
        # crosshair: off
        with self._cv:
            if self._stopped:
                return
            if not self._replace and self._deadline is not None:
                return
            if self._replace:
                self._generation += 1
                self._failures = 0
                self._fire = fire
                self._on_give_up = on_give_up
            elif fire is not None:
                self._fire = fire
            if not self._started:
                run_in_background(self._run, name=self._name, dedicated=True)
                self._started = True
            self._deadline = time.monotonic() + delay
            self._cv.notify()

    def cancel(self) -> None:
        # crosshair: off
        with self._cv:
            if self._replace:
                self._generation += 1
                self._failures = 0
                self._fire = None
                self._on_give_up = None
            self._deadline = None
            self._cv.notify()

    def stop(self) -> None:
        # crosshair: off
        with self._cv:
            self._stopped = True
            self._deadline = None
            self._fire = None
            self._on_give_up = None
            self._cv.notify()

    def _run(self) -> None:
        # crosshair: off
        while True:
            with self._cv:
                while self._deadline is None and not self._stopped:
                    self._cv.wait()
                if self._stopped:
                    return
                deadline = self._deadline
                generation = self._generation
                fire = self._fire
                if deadline is None or fire is None:
                    continue
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._cv.wait(timeout=remaining)
                    continue
                self._deadline = None
                if self._replace:
                    self._fire = None
            try:
                fire()
                if self._retry:
                    with self._cv:
                        self._failures = 0
            except Exception:
                if not self._retry:
                    self._logger.exception("Delayed poke timer fire failed")
                    continue
                give_up = False
                give_up_cb = None
                delay = None
                failures = 0
                with self._cv:
                    if not self._stopped and self._deadline is None and self._generation == generation:
                        self._failures += 1
                        failures = self._failures
                        max_fail = self._max_failures() if self._max_failures is not None else 8
                        if self._failures >= max_fail:
                            self._fire = None
                            give_up = True
                            give_up_cb = self._on_give_up
                        else:
                            delay = (
                                self._retry_delay(failures)
                                if self._retry_delay is not None
                                else min(1.0, 0.1 * 2 ** min(failures - 1, 4))
                            )
                            self._deadline = time.monotonic() + delay
                            self._fire = fire
                            self._cv.notify()
                if give_up:
                    self._logger.warning("drain idle re-arm failed %d times; giving up", failures)
                    if give_up_cb is not None:
                        try:
                            give_up_cb()
                        except Exception:
                            self._logger.exception("drain idle give-up callback failed")
                elif delay is None:
                    self._logger.debug("drain idle re-arm failed after stop/re-arm; not retrying", exc_info=True)
                elif failures == 1:
                    self._logger.exception("drain idle re-arm failed")
                else:
                    self._logger.warning("drain idle re-arm failed %d times, retrying in %.2fs", failures, delay)


class BatchingStreamQueue:
    """Producer-side batcher for chat display text (CHUNK / THINKING).

    Intended to be created in the background reader thread (LLM streaming loop,
    web research, librarian, ACP backends, etc.). Callers that produce small
    display deltas should feed them through this wrapper (via .put() or the
    convenience callbacks returned by content_cb() / thinking_cb()).

    Contract:
    - Simple append: internal buffers just do buf.append(delta).
    - **Hard 250 ms max latency ("every 250 ms max, or when done")**:
      The *first* display delta that starts a new burst arms the batcher's
      reusable timer for exactly `batch_interval` (default 0.25 s) from the
      moment that first fragment arrived. Subsequent deltas during the burst
      are appended but do *not* push the deadline, and they do not start
      another thread. When the timer fires we emit one joined string per
      contiguous CHUNK or THINKING run, in arrival order. This guarantees the
      UI sees an update at least every 250 ms during a long fast stream.
    - Explicit `.flush()`, or any control/boundary item (STREAM_DONE, ERROR,
      STOPPED, APPROVAL_REQUIRED, TOOL_*, NEXT_TOOL, FINAL_DONE, etc.),
      also causes immediate emission of whatever has accumulated so far
      (and cancels the pending timer).
    - No main-thread sleeps. All timer work happens in the producer thread(s).
    - The blocking drain and the idle re-arm use 0.1 s (`_DRAIN_IDLE_REARM_SEC` /
      `_drain_batch`); this class does not wait on that timeout.
    - `put` is for one producer thread. The lock makes one boundary `put` atomic
      with its flush; a second producer is not the supported use.

    Typical usage:
        raw_q = queue.Queue()
        batched = BatchingStreamQueue(raw_q, batch_interval=1.0)
        ...
        # pass batched.content_cb() as append_callback to the LLM client
        # or to any code that used to do lambda t: q.put((CHUNK, t))
        ...
        # before a boundary:
        #   batched.flush()
        #   raw_q.put((StreamQueueKind.STREAM_DONE, response))
        # (or simply do batched.put((StreamQueueKind.STREAM_DONE, response))
        #  which does the flush for you)
    """

    _raw: queue.Queue[Any]
    _interval: float
    _lock: threading.Lock
    _dropped: bool

    def __init__(self, raw_q: queue.Queue[Any], batch_interval: float) -> None:
        # crosshair: off
        self._raw = raw_q
        self._interval = batch_interval
        # Contiguous runs in arrival order. Emitting every CHUNK buffer before
        # every THINKING buffer showed the reply before [Thinking].
        self._runs: list[tuple[StreamQueueKind, list[str]]] = []
        self._lock = threading.Lock()
        self._timer: _DelayedPokeTimer | None = None
        self._dropped = False

    def __del__(self) -> None:
        # crosshair: off
        # The timer thread holds only a weakref, and only while flushing.
        # Stop it when this batcher is released so a chat send does not leave
        # a waiting thread behind.
        timer = getattr(self, "_timer", None)
        if timer is None:
            return
        try:
            timer.stop()
        except Exception:
            return

    def _cancel_timer(self) -> None:
        # crosshair: off
        # Clear the deadline only. Dropping the timer object used to force
        # the next burst to construct a new threading.Timer.
        if self._timer is not None:
            self._timer.cancel()

    def _schedule_timer(self) -> None:
        # crosshair: off
        # One deadline per burst. The first CHUNK and the first THINKING each
        # called this, and cancel-then-restart moved the 250 ms mark when the
        # other kind arrived. Leave an armed deadline alone. Caller holds _lock.
        # The thread itself is created once and reused.
        if self._timer is None:
            timer = _DelayedPokeTimer("batch-stream-timer", replace=False, retry=False, logger=log)
            self._timer = timer
            ref = weakref.ref(self)

            def _burst_fire() -> None:
                # crosshair: off
                owner = ref()
                if owner is None:
                    timer.stop()
                    return
                try:
                    owner._timer_flush()
                except Exception:
                    log.exception("BatchingStreamQueue timer flush failed")
                finally:
                    del owner

            timer.arm(self._interval, fire=_burst_fire)
        else:
            self._timer.arm(self._interval)

    def _timer_flush(self) -> None:
        # crosshair: off
        # Timer callback — runs in its own (daemon) thread
        self.flush()

    def _append_display_locked(self, kind: StreamQueueKind, data: str) -> None:
        """Append one display fragment. Caller holds lock. Arms the burst timer once."""
        # crosshair: off
        is_first = not self._runs
        if self._runs and self._runs[-1][0] == kind:
            self._runs[-1][1].append(data)
        else:
            self._runs.append((kind, [data]))
        if is_first:
            self._schedule_timer()

    def _emit_pending_locked(self) -> None:
        """Emit each contiguous display run, in arrival order. Caller holds lock."""
        # crosshair: off
        # Bugfix: a later "".join used to leave earlier runs in _runs after
        # they were already put, so the next flush duplicated them. Snapshot-first
        # makes emit atomic wrt _runs.
        runs = self._runs
        self._runs = []
        self._cancel_timer()
        for kind, parts in runs:
            try:
                self._raw.put((kind, "".join(parts)))
            except Exception:
                log.exception("BatchingStreamQueue emit run failed")

    def put(self, item: Any) -> None:
        """Put an item. CHUNK/THINKING are batched; everything else forces a flush first.

        Batching rule (the "every 250 ms max, or when done" contract):
        - The *first* delta that makes a buffer go from empty → non-empty arms
          a one-shot timer for exactly self._interval from *that instant*.
        - Later deltas in the same burst just append; they do not move the deadline.
        - The timer firing, an explicit flush(), or any boundary control item
          causes the accumulated text (one joined string per kind) to be emitted.
        """
        # crosshair: off
        # Fast path for the two display kinds.
        # Bugfix: StreamQueueKind is a str enum, so "chunk" == StreamQueueKind.CHUNK
        # evaluated to True. Bare string tags were batched into display runs instead of
        # falling through to the boundary/raw queue, causing a mismatch with the drain
        # which requires isinstance(kind, StreamQueueKind). Requiring isinstance here
        # lets bare strings fall through so the drain's existing TypeError path reports them.
        if isinstance(item, (list, tuple)) and len(item) >= 1:
            kind = item[0]
            if isinstance(kind, StreamQueueKind) and (kind is StreamQueueKind.CHUNK or kind is StreamQueueKind.THINKING):
                data = item[1] if len(item) > 1 else ""
                with self._lock:
                    # Abort discarded this batcher. A later put must not arm the timer.
                    if self._dropped:
                        return
                    self._append_display_locked(kind, data or "")
                return

        # Any other kind (including bare kinds or control tuples) is a boundary.
        # Emit, the dropped check, and the put share this lock. Releasing the
        # lock between flush and raw.put lets discard() set _dropped in that
        # gap, and the control item (STREAM_DONE) still lands on the queue.
        # discard() then either runs wholly before (item dropped) or wholly
        # after (item already queued). Unbounded Queue.put does not take
        # this lock, so the timer flush cannot deadlock.
        with self._lock:
            if self._dropped:
                return
            self._emit_pending_locked()
            self._raw.put(item)

    def flush(self) -> None:
        """Force immediate emission of any pending display text (one joined string per kind)."""
        # crosshair: off
        with self._lock:
            if self._dropped:
                return
            self._emit_pending_locked()

    def discard(self) -> None:
        """Drop pending display text without emitting it.

        The tool-loop ``finally`` can clear the host's batcher reference
        while the 250 ms timer can still flush those runs. After Stop or a
        new send that flush would apply the first turn's tail onto the next
        queue. Discard under the same lock as emit, and stay dropped so a
        later ``put`` cannot arm the timer again.
        """
        # crosshair: off
        with self._lock:
            self._dropped = True
            self._runs.clear()
            self._cancel_timer()

    # Convenience factories so existing lambda sites become one-liners
    def content_cb(self) -> Callable[[str], None]:
        """Return a callback suitable for append_callback=... that feeds through the batcher."""

        # crosshair: off
        def cb(text: str) -> None:
            self.put((StreamQueueKind.CHUNK, text))

        return cb

    def thinking_cb(self) -> Callable[[str], None]:
        """Return a callback suitable for append_thinking_callback=..."""

        # crosshair: off
        def cb(text: str) -> None:
            self.put((StreamQueueKind.THINKING, text))

        return cb

    @property
    def raw(self) -> queue.Queue[Any]:
        """The underlying raw queue (for the rare legacy direct use or for the drain loop itself)."""
        # crosshair: off
        return self._raw

    def __repr__(self) -> str:
        # crosshair: off
        with self._lock:
            pending_content = sum(len(parts) for kind, parts in self._runs if kind == StreamQueueKind.CHUNK)
            pending_thinking = sum(len(parts) for kind, parts in self._runs if kind == StreamQueueKind.THINKING)
            return f"BatchingStreamQueue(interval={self._interval}, pending_content={pending_content}, pending_thinking={pending_thinking})"
