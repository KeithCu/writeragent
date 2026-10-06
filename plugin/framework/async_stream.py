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
"""Unified async stream orchestration for WriterAgent.

Handles both simple streaming and complex tool-calling loops with thinking/status updates.
Runs blocking API calls on worker threads and drains results on the main
thread. When ``AsyncCallback`` exists, each slice handles the items already
queued and returns to the VCL loop; the next slice is armed with
``addCallback`` or a short idle delay. The blocking ``Queue.get`` loop remains
only when that callback cannot be armed.

Concurrency: the LLM/network work runs on a **background** thread so
LibreOffice’s UI does not freeze. That worker only ``put``s tuples onto a
``queue.Queue``. The **LibreOffice main (UI) thread** drains the queue and
updates widgets. The first element of each tuple must be a
``StreamQueueKind`` enum member (not a raw string) so the drain loop can
tell tokens from errors from “stream finished.” ``BatchingStreamQueue``
uses a small lock only while coalescing pending text chunks; it does not
make UNO calls under that lock. While a drain is active it is the single
owner of the UI pump — see ``async_drain_guard``. The event-driven drain
holds that owner across callbacks; it does not keep the main thread inside
one callback for the idle wait.
"""

from __future__ import annotations

import inspect
import json
import logging
import queue
import threading
import time
import weakref
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeAlias, Callable, cast

from plugin.framework.worker_pool import run_in_background
from plugin.framework.deal_shim import DEAL_MAX_TOKEN, UNDER_CROSSHAIR, ascii_bounded, deal
from plugin.framework.errors import format_error_payload
from plugin.framework.async_drain_guard import acquire_drain_owner, release_drain_owner
from plugin.framework.queue_executor import NestedDrainOwnerError, _marshal_thread_tag, async_callback_for_drain_rearm, default_executor, drain_owner_scope, get_drain_owner, pump_ui_idle

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


class BlockingPumpKind(str, Enum):
    """Tags for :func:`run_blocking_in_thread` queue (not the stream drain protocol)."""

    DONE = "done"
    ERROR = "error"


class BlockingWaitStopped(Exception):
    """``stop_checker`` fired while waiting for a background func (no VCL pump)."""


@deal.pre(lambda prefix, data: ascii_bounded(prefix, DEAL_MAX_TOKEN, min_len=1))
@deal.post(lambda result: isinstance(result, str) and result.startswith("\n") and result.endswith("\n"))
@deal.ensure(lambda prefix, data, result=None: result is not None and prefix in result)
def _format_agent_tool_stream_line(prefix: str, data: Any) -> str:
    """Serialize ACP tool_call / tool_result payloads for chat display."""
    # data: Any (json.dumps / str) is an unbounded payload; prefix is already capped.
    # crosshair: off
    try:
        if UNDER_CROSSHAIR:
            body = str(data)
        elif isinstance(data, (dict, list)):
            body = json.dumps(data, ensure_ascii=False)
        else:
            body = str(data) if data is not None else ""
    except Exception:
        body = str(data)
    return "\n%s %s\n" % (prefix, body)


StreamQueueItem: TypeAlias = tuple[StreamQueueKind, ...]
BlockingPumpQueueItem: TypeAlias = tuple[BlockingPumpKind, Any]


def put_stream_queue_stopped(q: queue.Queue[Any]) -> None:
    """Enqueue a user-stopped signal. Always uses (kind, payload); do not use a 1-tuple."""
    # crosshair: off
    q.put((StreamQueueKind.STOPPED, None))


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
        # What was wrong: CHUNK was always queued before THINKING, so a burst
        # that started with thinking showed the reply first. Why: one joined
        # string per contiguous run, in the order the fragments arrived.
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

        # Any other kind (including bare kinds or control tuples) is a boundary
        self.flush()
        with self._lock:
            if self._dropped:
                return
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

        What was wrong: the tool-loop ``finally`` cleared the host's batcher
        reference while the 250 ms timer could still flush those runs. After
        Stop or a new send that flush applied the first turn's tail onto the
        next queue. Discard under the same lock as emit, and stay dropped so
        a later ``put`` cannot arm the timer again.
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


@dataclass(slots=True)
class _DrainState:
    """Mutable state for :func:`run_stream_drain_loop` (main thread only)."""

    q: queue.Queue[Any]
    apply_chunk_fn: Callable[[str, bool], None]
    on_stream_done: Callable[..., Any]
    on_stopped: Callable[[], None]
    on_error: Callable[[Any], Any]
    on_status_fn: Callable[[str], None] | None
    on_approval_required: Callable[..., None] | None
    show_search_thinking: bool
    job_done: list[bool]
    current_content: list[Any] = field(default_factory=list)
    current_thinking: list[Any] = field(default_factory=list)
    thinking_open: list[bool] = field(default_factory=lambda: [False])
    # NEXT_TOOL is not terminal. A true on_stream_done return is applied
    # only after the rest of this already-pulled batch (see _process_batch).
    defer_next_tool_exit: bool = False

    def close_thinking(self) -> None:
        # crosshair: off
        if self.thinking_open[0]:
            self.apply_chunk_fn(" /thinking\n", True)
            self.thinking_open[0] = False

    def flush_buffers(self) -> None:
        # crosshair: off
        if self.current_thinking:
            if not self.thinking_open[0]:
                self.apply_chunk_fn("[Thinking] ", True)
                self.thinking_open[0] = True
            self.apply_chunk_fn("".join(self.current_thinking), True)
            self.current_thinking.clear()
        if self.current_content:
            self.close_thinking()
            self.apply_chunk_fn("".join(self.current_content), False)
            self.current_content.clear()


def _drain_batch(q: queue.Queue[Any], timeout: float) -> list[Any]:
    """Block up to *timeout* for one item, then drain any immediately available extras."""
    # crosshair: off
    items: list[Any] = []
    try:
        items.append(q.get(timeout=timeout))
    except queue.Empty:
        return items
    try:
        while True:
            items.append(q.get_nowait())
    except queue.Empty:
        pass
    return items


def _handle_chunk(state: _DrainState, data: Any, _item: Any) -> None:
    # crosshair: off
    if state.current_thinking:
        state.flush_buffers()
    state.current_content.append(data)


def _handle_thinking(state: _DrainState, data: Any, _item: Any) -> None:
    # crosshair: off
    if state.current_content:
        state.flush_buffers()
    state.current_thinking.append(data)


def _handle_status(state: _DrainState, data: Any, _item: Any) -> None:
    # crosshair: off
    if state.on_status_fn:
        state.on_status_fn(data)


def _handle_stream_done_like(state: _DrainState, _data: Any, item: Any) -> None:
    # crosshair: off
    state.flush_buffers()
    state.close_thinking()
    if state.on_stream_done(item):
        state.job_done[0] = True


def _handle_next_tool(state: _DrainState, _data: Any, item: Any) -> None:
    """Advance a tool round. Do not end the drain in the middle of this batch.

    What was wrong: NEXT_TOOL used ``_handle_stream_done_like``. A true
    ``on_stream_done`` return set ``job_done`` and ``_process_batch`` broke,
    so items already pulled (the next chunk, ``STREAM_DONE``) were discarded
    and the pump stopped. The generic worker wrapper always returned true,
    so any ``NEXT_TOOL`` looked like a finished stream.
    How: the dispatch table mapped ``NEXT_TOOL`` to the terminal handler.
    Why: notify once and keep this batch going. A true return is applied
    only after that batch, so the tail runs in this drain instead of being
    dropped for a later one. A false return leaves ``job_done`` clear and
    the loop keeps pumping.
    """
    # crosshair: off
    state.flush_buffers()
    state.close_thinking()
    state.defer_next_tool_exit = bool(state.on_stream_done(item))


def _handle_tool_thinking(state: _DrainState, data: Any, _item: Any) -> None:
    # crosshair: off
    if state.show_search_thinking:
        if state.current_content:
            state.flush_buffers()
        state.current_thinking.append(data)


def _handle_tool_call_line(state: _DrainState, data: Any, _item: Any) -> None:
    # crosshair: off
    state.flush_buffers()
    state.close_thinking()
    state.apply_chunk_fn(_format_agent_tool_stream_line("[Tool call]", data), False)


def _handle_tool_result_line(state: _DrainState, data: Any, _item: Any) -> None:
    # crosshair: off
    state.flush_buffers()
    state.close_thinking()
    state.apply_chunk_fn(_format_agent_tool_stream_line("[Tool result]", data), False)


def _set_approval_event(item: Any) -> None:
    """Unblock ``wait_for_approval`` when the UI handler cannot finish the dialog."""
    if not isinstance(item, (tuple, list)):
        return
    for part in item:
        if isinstance(part, threading.Event):
            part.set()
            return


def _handle_approval_required(state: _DrainState, _data: Any, item: Any) -> None:
    # crosshair: off
    state.flush_buffers()
    state.close_thinking()
    if not state.on_approval_required:
        return
    try:
        state.on_approval_required(item)
    except Exception:
        # What was wrong: this logged the exception and returned. job_done
        # stayed false, and the worker stayed in wait_for_approval because
        # only the handler sets that event. Why: re-raise so the batch
        # on_error path ends the drain, and set the event so the worker
        # is not parked after the UI has already unblocked.
        _set_approval_event(item)
        raise


def _handle_stopped(state: _DrainState, _data: Any, _item: Any) -> None:
    # crosshair: off
    state.flush_buffers()
    state.close_thinking()
    state.on_stopped()
    state.job_done[0] = True


def _handle_error(state: _DrainState, data: Any, _item: Any) -> None:
    # crosshair: off
    state.flush_buffers()
    state.close_thinking()
    recovered = state.on_error(data) is True
    if not recovered:
        state.job_done[0] = True


_DISPATCH: dict[StreamQueueKind, Callable[[_DrainState, Any, Any], None]] = {
    StreamQueueKind.CHUNK: _handle_chunk,
    StreamQueueKind.THINKING: _handle_thinking,
    StreamQueueKind.STATUS: _handle_status,
    StreamQueueKind.STREAM_DONE: _handle_stream_done_like,
    StreamQueueKind.TOOL_DONE: _handle_stream_done_like,
    StreamQueueKind.FINAL_DONE: _handle_stream_done_like,
    StreamQueueKind.NEXT_TOOL: _handle_next_tool,
    StreamQueueKind.TOOL_THINKING: _handle_tool_thinking,
    StreamQueueKind.TOOL_CALL: _handle_tool_call_line,
    StreamQueueKind.TOOL_RESULT: _handle_tool_result_line,
    StreamQueueKind.APPROVAL_REQUIRED: _handle_approval_required,
    StreamQueueKind.STOPPED: _handle_stopped,
    StreamQueueKind.ERROR: _handle_error,
}


def _stream_item_kind_data(item: Any) -> tuple[Any, Any]:
    """Kind and payload. A bare kind or a length-1 tuple has no payload."""
    # crosshair: off
    if isinstance(item, (tuple, list)):
        kind = item[0]
        data = item[1] if len(item) > 1 else None
        return kind, data
    return item, None


def _apply_display_item(state: _DrainState, item: Any) -> None:
    """Apply one CHUNK or THINKING. Other kinds are left undispatched."""
    # crosshair: off
    raw_kind, data = _stream_item_kind_data(item)
    if raw_kind == StreamQueueKind.CHUNK:
        _handle_chunk(state, data, item)
    elif raw_kind == StreamQueueKind.THINKING:
        _handle_thinking(state, data, item)


def _apply_queued_display(state: _DrainState) -> None:
    """Apply CHUNK/THINKING already on the queue. Drop other kinds.

    Stop used to break without reading them, so text flushed from the 250ms
    batcher never reached the sidebar. Control items from the stopped attempt
    (STREAM_DONE, ERROR) are discarded with the get.
    """
    # crosshair: off
    while True:
        try:
            item = state.q.get_nowait()
        except queue.Empty:
            return
        _apply_display_item(state, item)


def _finish_on_stop(state: _DrainState, flush_pending: Callable[[], None] | None, pending_items: list[Any] | None = None) -> None:
    """Show text Stop would otherwise drop, then close thinking.

    What was wrong: the idle path called on_stopped without flushing
    buffers or closing thinking, and text still inside the 250ms batcher
    was dropped. A stop after ``_drain_batch`` only read ``state.q``.
    CHUNK and THINKING already pulled into the local batch were gone,
    including the whole batch when Stop tripped on the first item. Why:
    apply that unconsumed display tail, flush the producer batcher, apply
    what it just queued, then close thinking. Control items in the tail
    are not dispatched.
    """
    # crosshair: off
    if pending_items:
        for item in pending_items:
            _apply_display_item(state, item)
    if flush_pending is not None:
        try:
            flush_pending()
        except Exception:
            log.exception("flush_pending before Stop failed")
    _apply_queued_display(state)
    state.flush_buffers()
    state.close_thinking()
    state.on_stopped()
    state.job_done[0] = True


def _process_batch(state: _DrainState, items: list[Any], stop_checker: Callable[[], bool] | None, flush_pending: Callable[[], None] | None = None) -> None:
    # crosshair: off
    # Trailing flush_buffers() must still raise on the success path (outer catch
    # / test_stream_drain_loop_processing_error). Skip it after stop and after
    # an inner handler failure: those paths already flushed, and a second raise
    # would call on_error again.
    state.defer_next_tool_exit = False
    skip_trailing_flush = False
    for index, item in enumerate(items):
        if stop_checker and stop_checker():
            log.info("run_stream_drain_loop: Stop requested via checker.")
            # This tail is already off state.q, so the queue read inside
            # _finish_on_stop cannot see it.
            _finish_on_stop(state, flush_pending, items[index:])
            # What was wrong: this break left skip_trailing_flush False, so
            # the trailing flush ran after _finish_on_stop had already flushed.
            # Why: that second flush is not the success-path flush.
            skip_trailing_flush = True
            break

        raw_kind, data = _stream_item_kind_data(item)

        try:
            if not isinstance(raw_kind, StreamQueueKind):
                ek = TypeError("stream queue item kind must be StreamQueueKind, got %s" % (type(raw_kind).__name__,))
                log.error("Invalid stream queue tag: %s", ek)
                state.flush_buffers()
                state.close_thinking()
                state.on_error(format_error_payload(ek))
                state.job_done[0] = True
                break

            _DISPATCH[raw_kind](state, data, item)
        except Exception as loop_e:
            # Dispatch handler (chunk/thinking UI) raised. Re-queuing ERROR used
            # to continue the batch: later CHUNKs still applied, STREAM_DONE ran
            # as success, and on_error never ran. Call on_error inline.
            # Recovery drops the rest of this batch and leaves job_done clear
            # for the replacement worker. A fatal error sets job_done. Do not
            # also call on_stream_done (Writer restore vs finish).
            error_payload = format_error_payload(loop_e)
            log.exception("Stream processing failed")
            try:
                state.flush_buffers()
                state.close_thinking()
            except Exception:
                log.exception("Stream buffer flush after handler failure also failed")
            recovered = False
            try:
                recovered = state.on_error(error_payload) is True
            except Exception:
                log.exception("on_error after stream handler failure also failed")
            if recovered:
                # Tail items already pulled belong to the failed attempt.
                # Continuing used to run STREAM_DONE and set job_done, so the
                # replacement worker's chunks were ignored. Leave job_done
                # clear so the drain waits for that worker.
                # What was wrong: this break left skip_trailing_flush False.
                # The trailing flush_buffers() could raise, and
                # run_stream_drain_loop then called on_error a second time
                # for the same failure. The fatal path already skipped it.
                # Why: on_error already ran; do not flush again.
                skip_trailing_flush = True
                break
            state.job_done[0] = True
            skip_trailing_flush = True
            break

        if state.job_done[0] or raw_kind == StreamQueueKind.ERROR:
            # A recovered ERROR keeps the drain alive but must not apply the
            # rest of this batch (same reason as the handler-raise path).
            # NEXT_TOOL does not set job_done here, so a tail already pulled
            # (chunk, STREAM_DONE) still runs in this pass.
            break

    if not skip_trailing_flush:
        state.flush_buffers()
    # What was wrong: a true NEXT_TOOL return used to break above and drop
    # the tail. Why: honor that return only after the pulled batch is done,
    # and not after stop or a recovered error (those already decided).
    if state.defer_next_tool_exit and not state.job_done[0] and not skip_trailing_flush:
        state.job_done[0] = True


# Idle re-arm. Same cadence as the old ``Queue.get(0.1)`` wait, inside the
# 50–100 ms band. Immediate re-arm is used only when items are already queued,
# so an empty stream does not spin the main thread.
_DRAIN_IDLE_REARM_SEC = 0.1
_IDLE_REARM_RETRY_MAX_SEC = 1.0

# LibreOffice evidence: on Qt (vcl/qt5/QtInstance.cxx ImplYield), macOS
# (vcl/osx/salinst.cxx DoYield) and Windows (PostMessage SAL_MSG_USEREVENT
# retrieved before input/paint) continuously re-posted user events can starve
# input and paint; on GTK they can starve VCL repaint idles (user events at
# G_PRIORITY_HIGH_IDLE+30, scheduler at G_PRIORITY_LOW, vcl/unx/gtk3/gtkdata.cxx).
_DRAIN_MAX_IMMEDIATE_REARMS = 3
_DRAIN_YIELD_GAP_SEC = 0.02

# The event-driven session, main thread only. Callers register epilogues on it
# after ``run_stream_drain_loop`` returns and before the VCL callback unwinds.
_event_drain: Any = None


def clear_drain_capture() -> None:
    """Forget the drain that :func:`defer_until_drain_done` would attach to.

    What was wrong: ``_event_drain`` stays set while a drain is open, across
    VCL callbacks. With two documents streaming, document B's send could
    return before starting a drain of its own (Stop before the drain, an
    error, a nested-owner refusal) and then defer its completion onto
    document A's drain, so B's buttons and status waited for A to finish.
    Why here: a send callback calls this first, and ``run_stream_drain_loop``
    calls it on entry, so a deferral can only attach to a drain started in
    the same synchronous call. An open drain keeps its own epilogue list.
    """
    # crosshair: off
    global _event_drain
    _event_drain = None


def defer_until_drain_done(fn: Callable[[], None]) -> None:
    """Run *fn* when the current event-driven drain finishes.

    The blocking drain has already returned, so *fn* runs now. An event-driven
    drain returns before the stream ends; *fn* is queued and runs on the main
    thread after the terminal slice, in registration order. Send completion
    (``abort_turn``, ``SEND_COMPLETED``) has to go through here or it runs
    while tokens are still arriving.
    """
    # crosshair: off
    session = _event_drain
    if session is None or session.closed:
        fn()
        return
    session.epilogues.append(fn)


def _drain_ready(q: queue.Queue[Any], max_items: int = 50) -> list[Any]:
    """Up to ``max_items`` items already queued. Does not block.

    A blocking ``get`` here would sit inside the VCL callback. That holds
    SolarMutex for the whole timeout. See :func:`run_stream_drain_loop`.
    The cap keeps one slice short when a fast producer floods the queue; the
    slice re-arms immediately while items remain.
    """
    # crosshair: off
    items: list[Any] = []
    try:
        while len(items) < max_items:
            items.append(q.get_nowait())
    except queue.Empty:
        pass
    return items


class _IdleRearmThread:
    """One dedicated thread that pokes the main thread after a delay.

    A fresh thread per idle would churn for the whole stream. ``arm`` only
    moves the deadline. The thread never touches UNO objects it created;
    the fire callable (``addCallback``) is the same one workers already use.
    """

    _cv: threading.Condition
    _deadline: float | None
    _fire: Callable[[], None] | None
    _generation: int
    _failures: int
    _started: bool
    _stopped: bool

    def __init__(self) -> None:
        # crosshair: off
        self._cv = threading.Condition()
        self._deadline = None
        self._fire = None
        self._generation = 0
        self._failures = 0
        self._started = False
        self._stopped = False

    def arm(self, delay: float, fire: Callable[[], None]) -> None:
        # crosshair: off
        with self._cv:
            if self._stopped:
                return
            self._generation += 1
            self._fire = fire
            self._deadline = time.monotonic() + delay
            if not self._started:
                run_in_background(self._run, name="drain-rearm", dedicated=True)
                self._started = True
            self._cv.notify()

    def cancel(self) -> None:
        # crosshair: off
        with self._cv:
            self._generation += 1
            self._deadline = None
            self._fire = None
            self._cv.notify()

    def stop(self) -> None:
        # crosshair: off
        with self._cv:
            self._stopped = True
            self._deadline = None
            self._fire = None
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
                if generation != self._generation:
                    continue
                self._deadline = None
                self._fire = None
            try:
                fire()
                with self._cv:
                    self._failures = 0
            except Exception:
                # Why: a lost idle re-arm means the drain never runs another
                # slice (owner held, Send stuck on Stop, Stop cannot recover).
                # Re-queue the same fire with capped backoff unless the drain
                # stopped or re-armed meanwhile.
                delay = None
                failures = 0
                with self._cv:
                    if not self._stopped and self._deadline is None and self._generation == generation:
                        self._failures += 1
                        failures = self._failures
                        delay = min(_IDLE_REARM_RETRY_MAX_SEC, _DRAIN_IDLE_REARM_SEC * 2**min(failures - 1, 4))
                        self._deadline = time.monotonic() + delay
                        self._fire = fire
                        self._cv.notify()
                if delay is None:
                    log.debug("drain idle re-arm failed after stop/re-arm; not retrying", exc_info=True)
                elif failures == 1:
                    log.exception("drain idle re-arm failed")
                else:
                    log.warning("drain idle re-arm failed %d times, retrying in %.2fs", failures, delay)


def _new_xcallback(fn: Callable[[], None]) -> Any:
    """UNO ``XCallback`` whose ``notify`` runs *fn* on the main thread."""
    # crosshair: off
    import unohelper
    from com.sun.star.awt import XCallback

    class _SliceCallback(unohelper.Base, XCallback):
        def notify(self, aData: Any) -> None:
            del aData
            fn()

    return _SliceCallback()


class _AsyncCallbackRearm:
    """Re-arm a drain slice via ``AsyncCallback.addCallback``.

    ``post`` must not run *fn* on the caller. ``QueueExecutor.post`` does that
    under ``WRITERAGENT_TESTING``, which would put the loop back on this stack.
    ``addCallback`` queues a user event and returns (LibreOffice
    ``AsyncCallback``). The idle path sleeps on a dedicated thread, then
    calls ``addCallback`` so the wait is not inside the VCL callback.

    Do not put these slices on the send-scoped work queue. Stop's
    ``cancel_pending_work`` would drop the next slice, and the drain would
    never see the stop checker or run its epilogue.
    """

    _service: Any
    _lock: threading.Lock
    _target: Callable[[], None] | None
    _callback: Any
    _timer: _IdleRearmThread

    def __init__(self, service: Any) -> None:
        # crosshair: off
        self._service = service
        self._lock = threading.Lock()
        self._target = None
        # Created on the main thread. The timer thread only calls addCallback.
        self._callback = _new_xcallback(self._notify)
        self._timer = _IdleRearmThread()

    def _notify(self) -> None:
        # crosshair: off
        with self._lock:
            target = self._target
        if target is not None:
            target()

    def _poke(self) -> None:
        # crosshair: off
        # The idle timer thread can poke while close() runs on the main
        # thread; read both under the lock so a closed rearm is a no-op.
        with self._lock:
            callback = self._callback
            service = self._service
        if callback is None or service is None:
            return
        service.addCallback(callback, None)

    def post(self, fn: Callable[[], None]) -> None:
        # crosshair: off
        self._timer.cancel()
        with self._lock:
            self._target = fn
        self._poke()

    def post_after(self, delay: float, fn: Callable[[], None]) -> None:
        # crosshair: off
        def _fire() -> None:
            with self._lock:
                self._target = fn
            self._poke()

        self._timer.arm(delay, _fire)

    def close(self) -> None:
        # crosshair: off
        self._timer.stop()
        with self._lock:
            self._target = None
            self._callback = None
            self._service = None


class _EventDrain:
    """One stream drain split across VCL callbacks.

    ``scheduler.post`` / ``post_after`` must not call the slice inline.
    Tests pass a recording scheduler. Production uses
    :class:`_AsyncCallbackRearm`.
    """

    _state: _DrainState
    _scheduler: Any
    _stop_checker: Callable[[], bool] | None
    _flush_pending: Callable[[], None] | None
    epilogues: list[Callable[[], None]]
    closed: bool
    _held: bool
    _previous_owner: str | None
    _generation: int
    _immediate_streak: int

    def __init__(self, state: _DrainState, scheduler: Any, stop_checker: Callable[[], bool] | None, flush_pending: Callable[[], None] | None) -> None:
        # crosshair: off
        self._state = state
        self._scheduler = scheduler
        self._stop_checker = stop_checker
        self._flush_pending = flush_pending
        self.epilogues = []
        self.closed = False
        self._held = False
        self._previous_owner = None
        self._generation = 0
        self._immediate_streak = 0

    def start(self) -> None:
        """Take the pump owner and arm the first slice. Does not process items."""
        # crosshair: off
        global _event_drain
        self._previous_owner = acquire_drain_owner("stream")
        self._held = True
        _event_drain = self
        try:
            self._schedule_next(idle=False)
        except Exception as exc:
            log.exception("event drain failed to arm")
            self._report_slice_error(exc)
            self._state.job_done[0] = True
            self._finish()

    def _schedule_next(self, *, idle: bool, delay: float | None = None) -> None:
        # crosshair: off
        if self.closed:
            return
        self._generation += 1
        generation = self._generation

        def _run() -> None:
            # A superseded idle callback must not apply items from a later turn.
            if self.closed or generation != self._generation:
                return
            self._slice()

        if idle:
            self._scheduler.post_after(_DRAIN_IDLE_REARM_SEC, _run)
        elif delay is not None:
            self._scheduler.post_after(delay, _run)
        else:
            self._scheduler.post(_run)

    def _report_slice_error(self, exc: BaseException) -> None:
        # crosshair: off
        try:
            self._state.on_error(format_error_payload(exc))
        except Exception:
            log.exception("event drain on_error failed")

    def _slice(self) -> None:
        """Process the ready batch, then return. Do not wait here.

        Why this must not loop: the slice runs inside a VCL callback, which
        already holds SolarMutex (recursive). ``VCLXToolkit::processEventsToIdle``
        takes another ``SolarMutexGuard`` for the whole call
        (toolkit/source/awt/vclxtoolkit.cxx). GTK's ``Yield`` releases that
        mutex only during ``g_main_context_iteration``, and the drain used to
        spend the rest of each idle in ``Queue.get(0.1)`` back in Python, so
        the acquire count was non-zero again. A worker GC that drops a PyUNO
        proxy takes SolarMutex from the C++ destructor while that worker holds
        the GIL; the main thread then cannot leave ``get`` or enter the next
        yield. Returning to the top-level VCL loop drops both. Do not put the
        ``while`` back, and do not call ``processEventsToIdle`` from this
        slice — paints that create hidden documents keep that nested loop from
        returning, and the executor's own ``AsyncCallback`` runs marshaled UNO
        once this callback has returned.
        """
        # crosshair: off
        if self.closed:
            return
        state = self._state
        t0 = time.monotonic()
        try:
            if self._stop_checker and self._stop_checker():
                log.info("run_stream_drain_loop: Stop requested via checker.")
                _finish_on_stop(state, self._flush_pending)
                self._finish()
                return
            try:
                items = _drain_ready(state.q)
            except Exception as exc:
                log.exception("Stream queue drain failed")
                self._report_slice_error(exc)
                state.job_done[0] = True
                self._finish()
                return
            if items:
                try:
                    _process_batch(state, items, self._stop_checker, self._flush_pending)
                except Exception as exc:
                    log.exception("run_stream_drain_loop batch processing failed")
                    state.job_done[0] = True
                    self._report_slice_error(exc)
                    self._finish()
                    return
            if state.job_done[0]:
                self._finish()
                return
            # apply_chunk can be slow enough for the worker to queue the next
            # batch. Take that on the next callback. An empty queue waits,
            # otherwise a quiet stream busy-spins addCallback.
            try:
                pending = state.q.qsize()
            except Exception:
                pending = 0

            elapsed = time.monotonic() - t0
            if elapsed > 0.1:
                log.debug("event drain slice took %.3fs", elapsed)

            if pending == 0:
                self._immediate_streak = 0
                self._schedule_next(idle=True)
            elif self._immediate_streak >= _DRAIN_MAX_IMMEDIATE_REARMS:
                self._immediate_streak = 0
                self._schedule_next(idle=False, delay=_DRAIN_YIELD_GAP_SEC)
            else:
                self._immediate_streak += 1
                self._schedule_next(idle=False)
        except Exception as exc:
            log.exception("event drain slice failed")
            self._report_slice_error(exc)
            state.job_done[0] = True
            self._finish()

    def _finish(self) -> None:
        """Release the pump owner, then run epilogues. Later slices are no-ops."""
        # crosshair: off
        global _event_drain
        if self.closed:
            return
        self.closed = True
        self._generation += 1
        if _event_drain is self:
            _event_drain = None
        closer = getattr(self._scheduler, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:
                log.exception("drain re-arm close failed")
        if self._held:
            self._held = False
            # Idle callbacks see a free pump, same as the blocking loop exiting
            # its ``with`` before the caller continues.
            release_drain_owner(self._previous_owner)
        epilogues = self.epilogues
        self.epilogues = []
        for fn in epilogues:
            try:
                fn()
            except Exception:
                log.exception("drain epilogue failed")


def run_stream_drain_loop(q: Any, toolkit: Any, job_done: Any, apply_chunk_fn: Any, on_stream_done: Any, on_stopped: Any, on_error: Any, on_status_fn: Any = None, show_search_thinking: bool = False, on_approval_required: Any = None, stop_checker: Any = None, flush_pending: Any = None, *, rearm: Any = None) -> None:
    """
    Main-thread drain: batches items from the queue, manages thinking/chunk
    buffers, and dispatches to callbacks.

    When *rearm* is passed, or ``AsyncCallback`` can be armed, process the
    items already queued and return to the VCL loop. The next slice is an
    ``addCallback`` (queue non-empty) or a ~100 ms idle re-arm (queue empty).
    Callers that used to run after this function returns must use
    :func:`defer_until_drain_done` so that work still waits for the terminal
    slice. Without a callback (unit tests, eval harness, force-marshal) the
    blocking loop below is unchanged, including ``pump_ui_idle``.

    Do not fold the slices back into one ``while`` inside the callback. That
    holds SolarMutex across the idle wait; a worker freeing a PyUNO proxy
    then blocks in the proxy destructor until this callback returns. See
    :meth:`_EventDrain._slice`.

    Supported queue items (kind, *args); kind must be :class:`StreamQueueKind`:
    - (CHUNK, text): Applied via apply_chunk_fn(text, is_thinking=False).
    - (THINKING, text): Applied via apply_chunk_fn(text, is_thinking=True).
    - (STATUS, text): Passed to on_status_fn(text).
    - (STREAM_DONE, response): Calls on_stream_done(item). Returns True if job finished.
    - (NEXT_TOOL,): Internal trigger for multi-round loops. Calls
      on_stream_done(item) but does not stop the drain mid-batch. A true
      return is applied only after items already pulled are handled. A false
      return keeps the drain pumping for the next round.
    - (TOOL_DONE, call_id, func_name, args_str, res): Handled by orchestration (if used).
    - (TOOL_THINKING, text): Thinking tokens from a tool (e.g. web search).
    - (FINAL_DONE, text): Final non-tool response.
    - (APPROVAL_REQUIRED, ...): HITL; call on_approval_required(item).
    - (STOPPED, ignored): Calls on_stopped() (second element unused).
    - (ERROR, payload): Calls on_error(payload). If on_error returns True, the
      drain keeps running (handler recovered, e.g. STT fallback spawned a new
      worker on this queue) but drops the rest of the batch already pulled.
      Any other return value ends the loop. A dispatch handler that raises is
      the same contract (inline on_error, no re-queue, no on_stream_done).
    - (TOOL_CALL, payload): Agent-backend tool block; shown as text via apply_chunk_fn.
    - (TOOL_RESULT, payload): Agent-backend tool result block; shown as text via apply_chunk_fn.
    """
    # crosshair: off
    state = _DrainState(q=q, apply_chunk_fn=apply_chunk_fn, on_stream_done=on_stream_done, on_stopped=on_stopped, on_error=on_error, on_status_fn=on_status_fn, on_approval_required=on_approval_required, show_search_thinking=show_search_thinking, job_done=job_done)
    log.debug("run_stream_drain_loop start %s", _marshal_thread_tag())
    # Deferrals after this call belong to this drain (or run now if it is
    # blocking or refused), never to an older drain still open elsewhere.
    clear_drain_capture()
    scheduler = rearm if rearm is not None else _make_drain_rearm()
    if scheduler is None:
        _run_stream_drain_blocking(state, toolkit, stop_checker, flush_pending)
        return
    try:
        # Same-name nesting is legal for a peer execute under an existing scope.
        # A different drain owner (e.g. MCP) must be rejected so pumps do not conflict.
        existing_owner = get_drain_owner()
        if existing_owner is not None and existing_owner != "stream":
            raise NestedDrainOwnerError(f"Nested stream drain while {existing_owner!r} already owns the UI pump")
        _EventDrain(state, scheduler, stop_checker, flush_pending).start()
    except NestedDrainOwnerError as exc:
        error_payload = format_error_payload(exc)
        log.exception("Nested stream drain rejected")
        try:
            on_error(error_payload)
        except Exception:
            log.exception("Failed to notify error handler for nested drain")
        job_done[0] = True
    except Exception as exc:
        error_payload = format_error_payload(exc)
        log.exception("Stream drain loop crashed")
        try:
            on_error(error_payload)
        except Exception:
            log.exception("Failed to notify error handler")
        job_done[0] = True


def _make_drain_rearm() -> _AsyncCallbackRearm | None:
    """Production re-arm, or None when the blocking loop must be used."""
    # crosshair: off
    service = async_callback_for_drain_rearm()
    if service is None:
        return None
    try:
        return _AsyncCallbackRearm(service)
    except Exception:
        log.exception("AsyncCallback re-arm unavailable; blocking drain")
        return None


def _run_stream_drain_blocking(state: _DrainState, toolkit: Any, stop_checker: Any, flush_pending: Any) -> None:
    """Blocking drain used when ``AsyncCallback`` cannot take the next slice.

    Unit tests and the eval harness have no VCL callback to return to.
    Do not use this loop when ``addCallback`` works. See :meth:`_EventDrain._slice`.
    """
    # crosshair: off
    q = state.q
    job_done = state.job_done
    on_error = state.on_error
    try:
        # What was wrong: commit 8ea060d0d rejected any existing_owner even when it was
        # "stream", breaking dual-deck peer send drains with NestedDrainOwnerError.
        # How it happened: get_drain_owner() was checked for any truthy value.
        # Why this change: only reject when existing_owner != "stream". Same-name nesting
        # is handled by drain_owner_scope (depth counter) and pump_ui_idle (skips nested VCL).
        existing_owner = get_drain_owner()
        if existing_owner is not None and existing_owner != "stream":
            raise NestedDrainOwnerError(f"Nested stream drain while {existing_owner!r} already owns the UI pump")
        with drain_owner_scope("stream"):
            while not job_done[0]:
                if stop_checker and stop_checker():
                    log.info("run_stream_drain_loop: Stop requested via checker.")
                    _finish_on_stop(state, flush_pending)
                    break

                try:
                    items = _drain_batch(q, 0.1)
                except Exception as e:
                    error_payload = format_error_payload(e)
                    log.exception("Stream queue drain failed")
                    on_error(error_payload)
                    job_done[0] = True
                    break

                if not items:
                    marshal_depth = default_executor.pending_work_count()
                    if toolkit:
                        pump_ui_idle(toolkit)
                    if marshal_depth > 0:
                        remaining = default_executor.pending_work_count()
                        # pump_ui_idle drains one item. A healthy backlog of 2+
                        # still has remaining > 0; that is not a blocked worker.
                        if remaining >= marshal_depth:
                            log.warning("drain_idle: marshal queue_depth=%d after pump (worker may be blocked) %s", remaining, _marshal_thread_tag())
                        else:
                            log.debug("drain_idle: stream queue empty, marshal depth %d -> %d %s", marshal_depth, remaining, _marshal_thread_tag())
                    continue

                try:
                    _process_batch(state, items, stop_checker, flush_pending)
                except Exception as e:
                    error_payload = format_error_payload(e)
                    log.exception("run_stream_drain_loop batch processing failed")
                    job_done[0] = True
                    try:
                        on_error(error_payload)
                    except Exception:
                        log.exception("Failed to notify error handler for batch processing failure")

                if toolkit:
                    pump_ui_idle(toolkit)

            if toolkit:
                pump_ui_idle(toolkit)

    except NestedDrainOwnerError as e:
        error_payload = format_error_payload(e)
        log.exception("Nested stream drain rejected")
        try:
            on_error(error_payload)
        except Exception:
            log.exception("Failed to notify error handler for nested drain")
        job_done[0] = True

    except Exception as e:
        error_payload = format_error_payload(e)
        log.exception("Stream drain loop crashed")

        try:
            on_error(error_payload)
        except Exception:
            log.exception("Failed to notify error handler")

        job_done[0] = True


def _call_item_or_zero_arg(fn: Callable[..., None], item: Any) -> None:
    """Call ``fn(item)`` or ``fn()`` once, from the signature.

    What was wrong: a ``TypeError`` whose text contained "positional argument"
    was treated as an arity mismatch and the callback was called again with
    no arguments. A ``TypeError`` raised inside the body can contain that
    text, so ``on_done`` ran twice. Why: choose the call before invoking
    the callback. An exception from the body is not a retry.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        fn(item)
        return
    takes_item = False
    for param in signature.parameters.values():
        if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
            takes_item = True
            break
    if takes_item:
        fn(item)
    else:
        fn()


_TERMINAL_WATCH_KINDS = frozenset((
    StreamQueueKind.STREAM_DONE,
    StreamQueueKind.ERROR,
    StreamQueueKind.STOPPED,
    StreamQueueKind.FINAL_DONE,
))
_terminal_watch_lock = threading.Lock()


def _watch_queue_terminal(real_q: Any, saw_terminal: list[bool]) -> None:
    """Count this caller on the queue's put wrapper.

    What was wrong: each drain saved ``Queue.put`` and assigned that saved
    method back in ``finally``. A second drain on the same queue captured the
    first wrapper as the original. Whichever call restored first either
    dropped the live wrapper or left the finished call's wrapper installed.
    Why: one wrapper, a list of per-call flags, restore only when the last
    watcher leaves. Flags are removed by identity (``list.remove`` treats
    equal ``[False]`` cells as the same).
    """
    # crosshair: off
    with _terminal_watch_lock:
        state = getattr(real_q, "_wa_terminal_watch", None)
        if isinstance(state, dict):
            existing = state.get("flags")
            if isinstance(existing, list):
                existing.append(saw_terminal)
                return
        orig_put = real_q.put
        flags: list[list[bool]] = [saw_terminal]

        def _watched_put(item: Any, *args: Any, **kwargs: Any) -> None:
            if isinstance(item, tuple) and item and item[0] in _TERMINAL_WATCH_KINDS:
                with _terminal_watch_lock:
                    active = list(flags)
                for flag in active:
                    flag[0] = True
            orig_put(item, *args, **kwargs)

        real_q.put = _watched_put
        real_q._wa_terminal_watch = {"orig": orig_put, "flags": flags, "watched": _watched_put}


def _unwatch_queue_terminal(real_q: Any, saw_terminal: list[bool]) -> None:
    """Drop this caller's flag. Restore ``Queue.put`` when none remain."""
    # crosshair: off
    with _terminal_watch_lock:
        state = getattr(real_q, "_wa_terminal_watch", None)
        if not isinstance(state, dict):
            return
        flags: list[list[bool]] = state["flags"]
        for index, flag in enumerate(flags):
            if flag is saw_terminal:
                del flags[index]
                break
        else:
            return
        if flags:
            return
        watched = state.get("watched")
        orig = state.get("orig")
        if real_q.put is watched and orig is not None:
            real_q.put = orig
        try:
            delattr(real_q, "_wa_terminal_watch")
        except AttributeError:
            return


def run_async_worker_with_drain(
    ctx: Any,
    worker_fn: Callable[[queue.Queue[Any]], None],
    apply_chunk_fn: Callable[[str, bool], None] | None,
    on_done_fn: Callable[..., None] | None,
    on_error_fn: Callable[[Any], None] | None,
    on_status_fn: Callable[[str], None] | None = None,
    stop_checker: Callable[[], bool] | None = None,
    on_stopped_fn: Callable[[], None] | None = None,
    name: str = "async-worker",
    q: queue.Queue[Any] | BatchingStreamQueue | None = None,
    on_approval_required: Callable[[Any], None] | None = None,
) -> None:
    """Run a background worker and drain its queue on the main thread.

    ``worker_fn`` is a callable that accepts the queue and produces
    :class:`StreamQueueKind` tuples. It does not need to post a terminal
    ``STREAM_DONE`` — the wrapper does so after the worker returns so the drain loop
    always unblocks. Any exception raised by ``worker_fn`` is converted
    into an ``ERROR`` payload, and that path does not also post
    ``STREAM_DONE``: ``on_error`` returning ``True`` keeps the drain alive
    for a replacement worker (native-audio STT fallback).

    Callback defaults: ``on_error_fn`` and ``on_stopped_fn`` fall back to
    ``on_done_fn`` or a no-op so the drain loop never fails on a missing
    handler.
    """
    # crosshair: off
    if q is None:
        q = queue.Queue()
    job_done = [False]

    # Support BatchingStreamQueue transparently for producer-side batching
    _batched: BatchingStreamQueue | None = q if isinstance(q, BatchingStreamQueue) else None
    _real_q: queue.Queue[Any] = cast("queue.Queue[Any]", _batched.raw if _batched is not None else q)

    # What was wrong: _TerminalWatch only saw puts through the wrapper object
    # passed to worker_fn. send_handlers closes over the real queue and puts
    # ERROR/STREAM_DONE there, so finally always posted a second STREAM_DONE.
    # That can end a recovered drain (on_error True) on a later iteration.
    # Why: watch the real queue's put for this worker. Overlapping drains on
    # one queue share the wrapper; see _watch_queue_terminal.
    saw_terminal = [False]
    real_any: Any = _real_q

    def _flush_producer_batch() -> None:
        if _batched is not None:
            _batched.flush()

    def worker_wrapper() -> None:
        # What was wrong: ``finally`` always queued STREAM_DONE after ERROR.
        # A handler that returns True (keep draining, e.g. STT fallback) then
        # saw that sentinel and ended the job before the replacement worker's
        # chunks. Skip the sentinel when this wrapper or the worker already
        # queued a terminal item.
        #
        # What was wrong: the error path flushed the batcher before putting
        # ERROR, and flushed again before unwatch, with no try. A raising
        # flush skipped that put, left the failure flag set so the
        # STREAM_DONE fallback was suppressed, and left the put wrapper
        # installed. Why: log the flush error, still queue ERROR, and
        # unwatch from finally.
        # What was wrong: if the worker already queued a terminal item (such
        # as ERROR) and then raised, a second ERROR was queued unconditionally.
        # A recovery on_error (returning True) would then execute a second time
        # against the replacement worker. Skip ERROR if saw_terminal[0] is set.
        error_item: tuple[Any, Any] | None = None
        _watch_queue_terminal(real_any, saw_terminal)
        try:
            try:
                # Pass the real queue (or batcher). Puts go through the patched put.
                worker_fn(cast("queue.Queue[Any]", q))
            except BaseException as e:
                error_item = (StreamQueueKind.ERROR, format_error_payload(e))
            try:
                _flush_producer_batch()
            except Exception:
                log.exception("BatchingStreamQueue flush before terminal failed")
            if error_item is not None and not saw_terminal[0]:
                real_any.put(error_item)
            elif not saw_terminal[0]:
                real_any.put((StreamQueueKind.STREAM_DONE, None))
        finally:
            _unwatch_queue_terminal(real_any, saw_terminal)

    from plugin.framework.uno_context import get_toolkit

    toolkit = get_toolkit(ctx)
    if toolkit is None:
        from plugin.framework.errors import UnoObjectError

        err = UnoObjectError(f"Failed to create toolkit for {name}")
        if on_error_fn:
            try:
                on_error_fn(format_error_payload(err))
            except Exception:
                log.exception("Failed to notify error handler for toolkit creation failure")
        return

    # What was wrong: the nested-owner check lived inside the drain loop,
    # after this worker was already started. A second Send from
    # processEventsToIdle raised NestedDrainOwnerError and left the worker
    # writing to a queue nobody reads. Refuse before spawn.
    existing_owner = get_drain_owner()
    if existing_owner is not None:
        nested = NestedDrainOwnerError(f"Nested stream drain while {existing_owner!r} already owns the UI pump")
        if on_error_fn:
            try:
                on_error_fn(format_error_payload(nested))
            except Exception:
                log.exception("Failed to notify error handler for nested drain")
        return

    run_in_background(worker_wrapper, daemon=True, name=name, dedicated=True)

    def on_stream_done_wrapper(item: Any) -> bool:
        if on_done_fn:
            _call_item_or_zero_arg(on_done_fn, item)
        # Return True so _handle_stream_done_like sets job_done[0] and the
        # drain loop exits. This is the sole exit path now that the worker
        # thread no longer sets job_done directly (see worker_wrapper comment).
        # NEXT_TOOL is not that exit. Returning true here used to stop the
        # pump before the next tool round. STREAM_DONE / FINAL_DONE /
        # TOOL_DONE still end the drain.
        kind, _payload = _stream_item_kind_data(item)
        if kind == StreamQueueKind.NEXT_TOOL:
            return False
        return True

    def _noop_error(_payload: Any) -> None:
        return None

    def _noop_stopped() -> None:
        return None

    def _noop_chunk(_text: str, _is_thinking: bool) -> None:
        return None

    resolved_apply_chunk = apply_chunk_fn or _noop_chunk
    resolved_on_error = on_error_fn or _noop_error

    def _call_done_on_stopped() -> None:
        # Mirror on_stream_done_wrapper: try with a sentinel item first, then
        # fall back to zero-arg for callbacks that don't accept arguments.
        # Without this, a TypeError from on_done_fn() propagates out of on_stopped()
        # uncaught, turning a clean Stop into a spurious error in the drain loop.
        # _done_fn is narrowed to non-None by the guard below (if on_done_fn).
        _done_fn = on_done_fn
        assert _done_fn is not None
        _call_item_or_zero_arg(_done_fn, None)

    resolved_on_stopped = on_stopped_fn or (_call_done_on_stopped if on_done_fn else _noop_stopped)

    # Chat's tool loop passes flush_pending so Stop emits text still inside
    # the 250ms batcher. This helper accepted a batcher and only flushed it
    # in the worker finally, so Stop could return with up to one interval
    # of already-produced text still buffered.
    run_stream_drain_loop(_real_q, toolkit, job_done, resolved_apply_chunk, on_stream_done=on_stream_done_wrapper, on_stopped=resolved_on_stopped, on_error=resolved_on_error, on_status_fn=on_status_fn, on_approval_required=on_approval_required, stop_checker=stop_checker, flush_pending=_flush_producer_batch if _batched is not None else None)


def _run_client_stream(
    ctx: Any,
    client_call: Callable[..., None],
    apply_chunk_fn: Callable[[str, bool], None] | None,
    on_done_fn: Callable[..., None] | None,
    on_error_fn: Callable[[Any], None] | None,
    on_status_fn: Callable[[str], None] | None = None,
    stop_checker: Callable[[], bool] | None = None,
    name: str = "stream-client",
    include_status: bool = False,
) -> None:
    """Shared adapter: run *client_call* in a worker streaming into the queue.

    ``client_call`` is a client method pre-bound with all positional args;
    it receives the standard streaming callback kwargs
    (``append_callback``, ``append_thinking_callback``, optional
    ``status_callback``, and ``stop_checker``).
    """
    # crosshair: off
    # Batch CHUNK/THINKING so Extend/Edit selection does not wake the drain
    # per token. STATUS still flushes (BatchingStreamQueue boundary).
    batched = BatchingStreamQueue(queue.Queue(), batch_interval=0.25)

    def worker(q: queue.Queue[Any]) -> None:
        kwargs: dict[str, Any] = {"append_callback": lambda t: q.put((StreamQueueKind.CHUNK, t)), "append_thinking_callback": lambda t: q.put((StreamQueueKind.THINKING, t)), "stop_checker": stop_checker}
        if include_status:
            kwargs["status_callback"] = lambda t: q.put((StreamQueueKind.STATUS, t))
        client_call(**kwargs)
        if stop_checker and stop_checker():
            put_stream_queue_stopped(q)

    run_async_worker_with_drain(
        ctx,
        worker,
        apply_chunk_fn=apply_chunk_fn,
        on_done_fn=on_done_fn,
        on_error_fn=on_error_fn,
        on_status_fn=on_status_fn,
        stop_checker=stop_checker,
        name=name,
        q=batched,
    )


def run_stream_completion_async(ctx: Any, client: Any, prompt: Any, system_prompt: Any, max_tokens: Any, apply_chunk_fn: Any, on_done_fn: Any, on_error_fn: Any, on_status_fn: Any = None, stop_checker: Any = None) -> None:
    """High-level helper for simple non-tool streams (always chat completions)."""
    # crosshair: off

    def client_call(**cb_kwargs: Any) -> None:
        client.stream_completion(prompt, system_prompt, max_tokens, **cb_kwargs)

    _run_client_stream(ctx, client_call, apply_chunk_fn=apply_chunk_fn, on_done_fn=on_done_fn, on_error_fn=on_error_fn, on_status_fn=on_status_fn, stop_checker=stop_checker, name="stream-completion", include_status=True)


def run_blocking_in_thread(ctx: Any, func: Any, *args: Any, pump_idle: bool = True, stop_checker: Callable[[], bool] | None = None, **kwargs: Any) -> Any:
    """
    Run a blocking function in a background thread.

    When *pump_idle* is True (default), pump UNO events on the caller thread so
    the UI stays responsive (STT). When False, wait without
    ``processEventsToIdle`` — required for Calc ``=PROMPT()`` because pumping
    inside recalc re-enters the formula engine (``#VALUE!``), and for notebook
    cell execute because ``LayoutIdle`` livelocks on documents with many
    in-flow form controls. ``=PY()`` already avoids this helper for the recalc
    reason.

    *stop_checker* (notebook Stop): poll the queue with a short timeout and
    return via :class:`BlockingWaitStopped` when the predicate is true. Never
    pumps VCL for that poll — same LayoutIdle livelock as ``pump_idle=False``.

    This wait is not the chat stream drain. Do not turn it into the
    event-driven slice scheduler: ``pump_idle=False`` (notebook cells,
    ``=PROMPT()``) must not return to the VCL loop, and ``pump_idle=True``
    still has to block the caller until *func* returns.

    Never runs *func* on the caller thread: a missing Toolkit used to fall back
    to a synchronous call, which blocked recalc with no worker isolation.

    The internal queue uses :class:`BlockingPumpKind` as the first tuple
    element only (same contract as :class:`StreamQueueKind` for the stream drain).
    """
    # crosshair: off
    q: "queue.Queue[BlockingPumpQueueItem]" = queue.Queue()

    def worker() -> None:
        try:
            result = func(*args, **kwargs)
            q.put((BlockingPumpKind.DONE, result))
        except BaseException as e:
            q.put((BlockingPumpKind.ERROR, e))

    toolkit = None
    if pump_idle:
        try:
            # What was wrong: the toolkit came from createInstanceWithContext on
            # ctx, with no main-thread check and no guard wrap.
            # How it happened: this helper built the toolkit inline instead of
            # going through get_toolkit().
            # Why this change: get_toolkit asserts the caller is the UI thread
            # and returns a guard_uno wrapper, same as the other UNO boundaries.
            from plugin.framework.uno_context import get_toolkit

            toolkit = get_toolkit(ctx)
        except Exception as e:
            log.warning("run_blocking_with_pump: Failed to create toolkit, waiting without pump. %s", e)
            toolkit = None

    run_in_background(worker, daemon=True, name="blocking-thread", dedicated=True)

    # Do not take drain_owner_scope here: this helper may run under an active stream
    # drain. pump_ui_idle remains the owner-safe VCL pump path.
    poll = (pump_idle and toolkit is not None) or stop_checker is not None
    while True:
        # What was wrong: ``raise data`` sat in this try. A worker that
        # raised queue.Empty was caught here. With poll=False the next
        # ``q.get(timeout=None)`` then blocked forever, because the worker
        # had already exited. Why: only the get waits on the queue. A
        # worker Empty is the function's exception and must propagate.
        try:
            item = q.get(timeout=0.1 if (poll or not pump_idle) else None)
        except queue.Empty:
            if stop_checker is not None and stop_checker():
                raise BlockingWaitStopped("stopped")
            if pump_idle and toolkit is not None:
                pump_ui_idle(toolkit)
            elif not pump_idle:
                from plugin.framework.queue_executor import pump_main_thread_work_queue
                pump_main_thread_work_queue()
            continue
        kind, data = item
        if not isinstance(kind, BlockingPumpKind):
            ek = TypeError("blocking pump queue item kind must be BlockingPumpKind, got %s" % (type(kind).__name__,))
            log.error("Invalid blocking pump tag: %s", ek)
            raise ek
        if kind == BlockingPumpKind.DONE:
            return data
        if kind == BlockingPumpKind.ERROR:
            raise data


# ── Streaming Delta Accumulation (OpenAI-Compatible) ───────────────


# Portions below copied from openai-python (https://github.com/openai/openai-python)
# src/openai/lib/streaming/_deltas.py
# License: Apache 2.0 (https://github.com/openai/openai-python/blob/main/LICENSE)


@deal.pre(lambda acc, delta: type(acc) is dict and type(delta) is dict)
@deal.post(lambda result: isinstance(result, dict))
@deal.raises(TypeError, RuntimeError)
def accumulate_delta(acc: dict[object, object], delta: dict[object, object]) -> dict[object, object]:
    """Merge a streaming chunk delta into an accumulated message/snapshot.

    Required for tool-calling: used in stream_request_with_tools to build the full
    assistant message from SSE chunks. Content and tool_calls (with partial
    function.arguments) are merged by index; strings are concatenated.
    """
    # Recursive merge of unbounded nested dicts/strings hangs deep check even with
    # a top-level len cap. Pytest still runs @deal; check-all skips this entry.
    # crosshair: off
    if type(acc) is not dict or type(delta) is not dict:
        raise TypeError("accumulate_delta requires plain dict acc and delta")
    for key, delta_value in delta.items():
        if key not in acc:
            acc[key] = delta_value
            continue

        acc_value = acc[key]
        if acc_value is None:
            acc[key] = delta_value
            continue

        # the `index` property is used in arrays of objects so it should
        # not be accumulated like other values e.g.
        # [{'foo': 'bar', 'index': 0}]
        #
        # the same applies to `type` properties as they're used for
        # discriminated unions
        if key == "index" or key == "type":
            acc[key] = delta_value
            continue

        if isinstance(acc_value, str) and isinstance(delta_value, str):
            acc_value += delta_value
        elif isinstance(acc_value, (int, float)) and isinstance(delta_value, (int, float)):
            acc_value += delta_value
        elif isinstance(acc_value, dict) and isinstance(delta_value, dict):
            acc_value = accumulate_delta(cast("dict[object, object]", acc_value), cast("dict[object, object]", delta_value))
        elif isinstance(acc_value, list) and isinstance(delta_value, list):
            # for lists of non-dictionary items we'll only ever get new entries
            # in the array, existing entries will never be changed
            if all(isinstance(x, (str, int, float)) for x in acc_value):
                cast("list[Any]", acc_value).extend(delta_value)
                continue

            for delta_entry in delta_value:
                if not isinstance(delta_entry, dict):
                    raise TypeError(f"Unexpected list delta entry is not a dictionary: {delta_entry}")

                try:
                    index = cast("dict[str, Any]", delta_entry)["index"]
                except KeyError as exc:
                    raise RuntimeError(f"Expected list delta entry to have an `index` key; {delta_entry}") from exc

                if not isinstance(index, int):
                    raise TypeError(f"Unexpected, list delta entry `index` value is not an integer; {index}")

                try:
                    acc_entry = cast("list[Any]", acc_value)[index]
                except IndexError:
                    cast("list[Any]", acc_value).insert(index, delta_entry)
                else:
                    if not isinstance(acc_entry, dict):
                        raise TypeError("not handled yet")

                    cast("list[Any]", acc_value)[index] = accumulate_delta(cast("dict[object, object]", acc_entry), cast("dict[object, object]", delta_entry))

        acc[key] = acc_value

    return acc


def coalesce_split_tool_calls(tool_calls: object) -> list[Any]:
    """Merge provider stream-split phantom tool_calls into the prior real call.

    OpenRouter/gpt-oss-120b:nitro streaming sometimes emits a second tool_call
    delta with a new ``index``, empty ``id``, empty ``function.name``, and the
    remainder of the previous call's JSON ``arguments``. Without coalescing,
    the empty-name call is executed (UNKNOWN_TOOL / ``tool_call_id: ""``) and
    the next API round 400s with ``tool_calls[n].function.name must be a
    non-empty string``.

    Walks ``tool_calls`` in order. Empty/missing ``function.name`` entries
    append their ``function.arguments`` string onto the previous kept call and
    are dropped. With no previous kept call, the empty-name entry is dropped.
    Kept calls are re-indexed from 0.
    """
    if not isinstance(tool_calls, list):
        return []
    kept: list[dict[str, Any]] = []
    # ty: isinstance(list) on object, then isinstance(dict) on items, infers
    # dict[Never, Never] so .get("function") is invalid-argument-type.
    for raw in cast("list[Any]", tool_calls):
        if not isinstance(raw, dict):
            continue
        tc = cast("dict[str, Any]", raw)
        fn_raw = tc.get("function")
        fn = cast("dict[str, Any]", fn_raw if isinstance(fn_raw, dict) else {})
        name = fn.get("name")
        if name:
            kept_tc = dict(tc)
            kept_fn = dict(fn)
            kept_tc["function"] = kept_fn
            kept.append(kept_tc)
            continue
        if not kept:
            continue
        args = fn.get("arguments")
        if args is None:
            args = ""
        elif not isinstance(args, str):
            args = str(args)
        prev_fn = kept[-1]["function"]
        prev_args = prev_fn.get("arguments")
        if prev_args is None:
            prev_args = ""
        elif not isinstance(prev_args, str):
            prev_args = str(prev_args)
        prev_fn["arguments"] = prev_args + args
    for i, tc in enumerate(kept):
        tc["index"] = i
    return kept
