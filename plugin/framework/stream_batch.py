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
``async_stream`` re-exports the enum.
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


class _ReusableBurstTimer:
    """One daemon thread for a batcher. ``arm`` sets the deadline; it does not start a new thread.

    ``threading.Timer`` cannot be restarted, so each burst used to construct
    another one. This thread waits until the deadline, fires once, then waits
    again. ``cancel`` clears the deadline without leaving the thread.
    """

    _interval: float
    _owner: weakref.ReferenceType["BatchingStreamQueue"]
    _cv: threading.Condition
    _deadline: float | None
    _started: bool
    _stopped: bool

    def __init__(self, owner: "BatchingStreamQueue", interval: float) -> None:
        # crosshair: off
        self._interval = interval
        # Weak so a finished send can drop the batcher. The thread would
        # otherwise keep it alive through the flush callback.
        self._owner = weakref.ref(owner)
        self._cv = threading.Condition()
        self._deadline = None
        self._started = False
        self._stopped = False

    def arm(self) -> None:
        """Start the interval if idle. A live deadline is left alone."""
        # crosshair: off
        with self._cv:
            if self._stopped or self._deadline is not None:
                return
            if not self._started:
                # Infinite wait: dedicated, not a pool slot. Start before the
                # deadline is visible so a failed start can be retried.
                run_in_background(self._run, name="batch-stream-timer", dedicated=True)
                self._started = True
            self._deadline = time.monotonic() + self._interval
            self._cv.notify()

    def cancel(self) -> None:
        """Drop the deadline. The thread stays for the next burst."""
        # crosshair: off
        with self._cv:
            self._deadline = None
            self._cv.notify()

    def stop(self) -> None:
        """Wake the thread so it exits. Used when the batcher is released."""
        # crosshair: off
        with self._cv:
            self._stopped = True
            self._deadline = None
            self._cv.notify()

    def _run(self) -> None:
        # crosshair: off
        # Catch here: run_in_background ends the thread on Exception, and a
        # later burst would then have no timer.
        while True:
            with self._cv:
                while self._deadline is None and not self._stopped:
                    self._cv.wait()
                if self._stopped:
                    return
                deadline = self._deadline
                if deadline is None:
                    continue
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._cv.wait(timeout=remaining)
                    continue
                self._deadline = None
            owner = self._owner()
            if owner is None:
                return
            try:
                owner._timer_flush()
            except Exception:
                log.exception("BatchingStreamQueue timer flush failed")
            finally:
                # Drop the strong ref before waiting again. Holding it across
                # the wait would keep the batcher (and this thread) alive
                # after the send released it.
                del owner


class BatchingStreamQueue:
    """Producer-side batcher for chat display text (CHUNK / THINKING).

    Intended to be created in the background reader thread (LLM streaming loop,
    web research, librarian, ACP backends, etc.). Callers that produce small
    display deltas should feed them through this wrapper (via .put() or the
    convenience callbacks returned by content_cb() / thinking_cb()).

    Contract (per user direction 2026-05-25, refined 2026-05-25):
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
    - The consumer-side drain loop timeout (currently 0.1 s) is left unchanged.

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
        self._timer: _ReusableBurstTimer | None = None
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
            self._timer = _ReusableBurstTimer(self, self._interval)
        self._timer.arm()

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
        # One joined string per contiguous run, in the order the fragments
        # arrived. Queuing every CHUNK before every THINKING shows the reply
        # first when a burst started with thinking.
        for kind, parts in self._runs:
            self._raw.put((kind, "".join(parts)))
        self._runs.clear()
        self._cancel_timer()

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
        # Fast path for the two display kinds
        if isinstance(item, (list, tuple)) and len(item) >= 1:
            kind = item[0]
            if kind == StreamQueueKind.CHUNK or kind == StreamQueueKind.THINKING:
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
