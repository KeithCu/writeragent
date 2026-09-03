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
Runs blocking API calls on worker threads and drains logic via a main-thread loop
to keep the LibreOffice UI responsive (pump_ui_idle: QueueExecutor + VCL).

Concurrency: the LLM/network work runs on a **background** thread so
LibreOffice’s UI does not freeze. That worker only ``put``s tuples onto a
``queue.Queue``. The **LibreOffice main (UI) thread** drains the queue and
updates widgets. The first element of each tuple must be a
``StreamQueueKind`` enum member (not a raw string) so the drain loop can
tell tokens from errors from “stream finished.” ``BatchingStreamQueue``
uses a small lock only while coalescing pending text chunks; it does not
make UNO calls under that lock. While the drain loop is pumping
LibreOffice events (``processEventsToIdle``), it is the single owner of
that pump — see ``async_drain_guard``.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeAlias, Callable, cast

from plugin.framework.worker_pool import run_in_background
from plugin.framework.deal_shim import DEAL_MAX_TOKEN, UNDER_CROSSHAIR, ascii_bounded, deal
from plugin.framework.errors import format_error_payload
from plugin.framework.queue_executor import (
    NestedDrainOwnerError,
    _marshal_thread_tag,
    default_executor,
    drain_owner_scope,
    pump_ui_idle,
)

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


def put_stream_queue_stopped(q: queue.Queue) -> None:
    """Enqueue a user-stopped signal. Always uses (kind, payload); do not use a 1-tuple."""
    # crosshair: off
    q.put((StreamQueueKind.STOPPED, None))


class BatchingStreamQueue:
    """Producer-side batcher for chat display text (CHUNK / THINKING).

    Intended to be created in the background reader thread (LLM streaming loop,
    web research, librarian, ACP backends, etc.). Callers that produce small
    display deltas should feed them through this wrapper (via .put() or the
    convenience callbacks returned by content_cb() / thinking_cb()).

    Contract (per user direction 2026-05-25, refined 2026-05-25):
    - Simple append: internal buffers just do buf.append(delta).
    - **Hard 250 ms max latency ("every 250 ms max, or when done")**:
      The *first* display delta that starts a new burst arms a one-shot timer
      for exactly `batch_interval` (default 0.25 s) from the moment that first
      fragment arrived. Subsequent deltas during the burst are appended but
      do *not* push the deadline. When the timer fires we emit exactly one
      joined string. This guarantees the UI sees an update at least every
      250 ms during a long fast stream.
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

    def __init__(self, raw_q: queue.Queue[Any], batch_interval: float):
        # crosshair: off
        self._raw = raw_q
        self._interval = batch_interval
        self._content_buf: list[str] = []
        self._thinking_buf: list[str] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def _cancel_timer(self):
        # crosshair: off
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _schedule_timer(self):
        # crosshair: off
        self._cancel_timer()
        self._timer = threading.Timer(self._interval, self._timer_flush)
        self._timer.daemon = True
        self._timer.start()

    def _timer_flush(self):
        # crosshair: off
        # Timer callback — runs in its own (daemon) thread
        self.flush()

    def _emit_pending_locked(self):
        """Emit any buffered content/thinking as single joined items. Caller holds lock."""
        # crosshair: off
        if self._content_buf:
            joined = "".join(self._content_buf)
            self._raw.put((StreamQueueKind.CHUNK, joined))
            self._content_buf.clear()
        if self._thinking_buf:
            joined = "".join(self._thinking_buf)
            self._raw.put((StreamQueueKind.THINKING, joined))
            self._thinking_buf.clear()
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
            if kind == StreamQueueKind.CHUNK:
                data = item[1] if len(item) > 1 else ""
                with self._lock:
                    is_first = len(self._content_buf) == 0
                    self._content_buf.append(data or "")
                    if is_first:
                        self._schedule_timer()  # deadline from the very first fragment of this burst
                return
            if kind == StreamQueueKind.THINKING:
                data = item[1] if len(item) > 1 else ""
                with self._lock:
                    is_first = len(self._thinking_buf) == 0
                    self._thinking_buf.append(data or "")
                    if is_first:
                        self._schedule_timer()  # deadline from the very first fragment of this burst
                return

        # Any other kind (including bare kinds or control tuples) is a boundary
        self.flush()
        self._raw.put(item)

    def flush(self) -> None:
        """Force immediate emission of any pending display text (one joined string per kind)."""
        # crosshair: off
        with self._lock:
            self._emit_pending_locked()

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
            return (f"BatchingStreamQueue(interval={self._interval}, "
                    f"pending_content={len(self._content_buf)}, "
                    f"pending_thinking={len(self._thinking_buf)})")


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


def _handle_approval_required(state: _DrainState, _data: Any, item: Any) -> None:
    # crosshair: off
    state.flush_buffers()
    state.close_thinking()
    if state.on_approval_required:
        try:
            state.on_approval_required(item)
        except Exception:
            log.exception("approval_required handler failed")


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
    StreamQueueKind.NEXT_TOOL: _handle_stream_done_like,
    StreamQueueKind.TOOL_THINKING: _handle_tool_thinking,
    StreamQueueKind.TOOL_CALL: _handle_tool_call_line,
    StreamQueueKind.TOOL_RESULT: _handle_tool_result_line,
    StreamQueueKind.APPROVAL_REQUIRED: _handle_approval_required,
    StreamQueueKind.STOPPED: _handle_stopped,
    StreamQueueKind.ERROR: _handle_error,
}


def _process_batch(state: _DrainState, items: list[Any], stop_checker: Callable[[], bool] | None) -> None:
    # crosshair: off
    # Trailing flush_buffers() must still raise on the success path (outer catch
    # / test_stream_drain_loop_processing_error). After a fatal inner handler
    # failure, skip it: a second raise would double-call on_error.
    skip_trailing_flush = False
    for item in items:
        if stop_checker and stop_checker():
            log.info("run_stream_drain_loop: Stop requested via checker.")
            state.flush_buffers()
            state.close_thinking()
            state.on_stopped()
            state.job_done[0] = True
            break

        raw_kind = item[0] if isinstance(item, (tuple, list)) else item
        data = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else None

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
            # as success, and on_error never ran (the re-queued ERROR sat behind
            # job_done). Call on_error inline like _handle_error; set job_done so
            # we do not hang after STREAM_DONE was already dequeued into items.
            # Do not also call on_stream_done (Writer restore vs finish).
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
                continue
            state.job_done[0] = True
            skip_trailing_flush = True
            break

        if state.job_done[0]:
            break

    if not skip_trailing_flush:
        state.flush_buffers()


def run_stream_drain_loop(q, toolkit, job_done, apply_chunk_fn, on_stream_done, on_stopped, on_error, on_status_fn=None, ctx=None, show_search_thinking=False, on_approval_required=None, stop_checker=None):
    """
    Main-thread drain loop: batches items from queue, manages thinking/chunk buffers,
    and dispatches to callbacks. Keeps UI responsive via pump_ui_idle (QueueExecutor + VCL).
    Includes comprehensive error handling to prevent UI thread crashes.

    Supported queue items (kind, *args); kind must be :class:`StreamQueueKind`:
    - (CHUNK, text): Applied via apply_chunk_fn(text, is_thinking=False).
    - (THINKING, text): Applied via apply_chunk_fn(text, is_thinking=True).
    - (STATUS, text): Passed to on_status_fn(text).
    - (STREAM_DONE, response): Calls on_stream_done(item). Returns True if job finished.
    - (NEXT_TOOL,): Internal trigger for multi-round loops.
    - (TOOL_DONE, call_id, func_name, args_str, res): Handled by orchestration (if used).
    - (TOOL_THINKING, text): Thinking tokens from a tool (e.g. web search).
    - (FINAL_DONE, text): Final non-tool response.
    - (APPROVAL_REQUIRED, ...): HITL; call on_approval_required(item).
    - (STOPPED, ignored): Calls on_stopped() (second element unused).
    - (ERROR, payload): Calls on_error(payload). If on_error returns True, the
      drain keeps running (handler recovered, e.g. STT fallback spawned a new
      worker on this queue). Any other return value ends the loop. A dispatch
      handler that raises is the same contract (inline on_error, no re-queue,
      no on_stream_done).
    - (TOOL_CALL, payload): Agent-backend tool block; shown as text via apply_chunk_fn.
    - (TOOL_RESULT, payload): Agent-backend tool result block; shown as text via apply_chunk_fn.
    """
    # crosshair: off
    state = _DrainState(q=q, apply_chunk_fn=apply_chunk_fn, on_stream_done=on_stream_done, on_stopped=on_stopped, on_error=on_error, on_status_fn=on_status_fn, on_approval_required=on_approval_required, show_search_thinking=show_search_thinking, job_done=job_done)
    log.debug("run_stream_drain_loop start %s", _marshal_thread_tag())
    first_batch_logged = [False]
    try:
        # One active drain owner: nested Send/drain must not start a second pump loop.
        with drain_owner_scope("stream"):
            while not job_done[0]:
                if stop_checker and stop_checker():
                    log.info("run_stream_drain_loop: Stop requested via checker.")
                    on_stopped()
                    job_done[0] = True
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
                    marshal_depth = default_executor._work_queue.qsize()
                    if toolkit:
                        pump_ui_idle(toolkit)
                    if marshal_depth > 0:
                        remaining = default_executor._work_queue.qsize()
                        if remaining > 0:
                            log.warning(
                                "drain_idle: marshal queue_depth=%d after pump (worker may be blocked) %s",
                                remaining,
                                _marshal_thread_tag(),
                            )
                        else:
                            log.debug(
                                "drain_idle: stream queue empty, marshal depth %d cleared by pump %s",
                                marshal_depth,
                                _marshal_thread_tag(),
                            )
                    continue

                if not first_batch_logged[0]:
                    first_batch_logged[0] = True

                try:
                    _process_batch(state, items, stop_checker)
                except Exception as e:
                    error_payload = format_error_payload(e)
                    log.exception("run_stream_drain_loop batch processing failed")
                    job_done[0] = True
                    try:
                        on_error(error_payload)
                    except Exception:
                        pass

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


def run_async_worker_with_drain(
    ctx: Any,
    worker_fn: Callable[[queue.Queue], None],
    apply_chunk_fn: Callable[[str, bool], None] | None,
    on_done_fn: Callable[..., None] | None,
    on_error_fn: Callable[[Any], None] | None,
    on_status_fn: Callable[[str], None] | None = None,
    stop_checker: Callable[[], bool] | None = None,
    on_stopped_fn: Callable[[], None] | None = None,
    name: str = "async-worker",
    q: queue.Queue[Any] | BatchingStreamQueue | None = None,
    on_approval_required: Callable[[Any], None] | None = None,
):
    """Run a background worker and drain its queue on the main thread.

    ``worker_fn`` is a callable that accepts the queue and produces
    :class:`StreamQueueKind` tuples. It does not need to post a terminal
    ``STREAM_DONE`` — the wrapper does so in ``finally`` so the drain loop
    always unblocks. Any exception raised by ``worker_fn`` is converted
    into an ``ERROR`` payload.

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

    def worker_wrapper():
        try:
            worker_fn(cast("queue.Queue[Any]", _batched.raw if _batched is not None else q))  # worker always sees a real Queue
        except BaseException as e:
            from plugin.framework.errors import format_error_payload

            payload = (StreamQueueKind.ERROR, format_error_payload(e))
            if _batched is not None:
                _batched.flush()
            _real_q.put(payload)
        finally:
            # Terminal sentinel — always flush any pending display text first
            # when using the batcher, then emit the sentinel on the real queue.
            if _batched is not None:
                _batched.flush()
            _real_q.put((StreamQueueKind.STREAM_DONE, None))

    from plugin.framework.uno_context import get_toolkit

    toolkit = get_toolkit(ctx)
    if toolkit is None:
        from plugin.framework.errors import UnoObjectError

        err = UnoObjectError(f"Failed to create toolkit for {name}")
        if on_error_fn:
            on_error_fn(err)
        return

    run_in_background(worker_wrapper, daemon=True, name=name, dedicated=True)

    def on_stream_done_wrapper(item):
        if on_done_fn:
            try:
                on_done_fn(item)
            except TypeError:
                # Fallback for callbacks that don't take any arguments.
                on_done_fn()
        # Return True so _handle_stream_done_like sets job_done[0] and the
        # drain loop exits. This is the sole exit path now that the worker
        # thread no longer sets job_done directly (see worker_wrapper comment).
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
        try:
            _done_fn(None)
        except TypeError:
            _done_fn()

    resolved_on_stopped = on_stopped_fn or (_call_done_on_stopped if on_done_fn else _noop_stopped)

    run_stream_drain_loop(
        _real_q,
        toolkit,
        job_done,
        resolved_apply_chunk,
        on_stream_done=on_stream_done_wrapper,
        on_stopped=resolved_on_stopped,
        on_error=resolved_on_error,
        on_status_fn=on_status_fn,
        ctx=ctx,
        on_approval_required=on_approval_required,
        stop_checker=stop_checker,
    )


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

    def worker(q: queue.Queue) -> None:
        kwargs: dict[str, Any] = {"append_callback": lambda t: q.put((StreamQueueKind.CHUNK, t)), "append_thinking_callback": lambda t: q.put((StreamQueueKind.THINKING, t)), "stop_checker": stop_checker}
        if include_status:
            kwargs["status_callback"] = lambda t: q.put((StreamQueueKind.STATUS, t))
        client_call(**kwargs)
        if stop_checker and stop_checker():
            put_stream_queue_stopped(q)

    run_async_worker_with_drain(ctx, worker, apply_chunk_fn=apply_chunk_fn, on_done_fn=on_done_fn, on_error_fn=on_error_fn, on_status_fn=on_status_fn, stop_checker=stop_checker, name=name)


def run_stream_completion_async(ctx, client, prompt, system_prompt, max_tokens, apply_chunk_fn, on_done_fn, on_error_fn, on_status_fn=None, stop_checker=None):
    """High-level helper for simple non-tool streams (always chat completions)."""
    # crosshair: off

    def client_call(**cb_kwargs):
        client.stream_completion(prompt, system_prompt, max_tokens, **cb_kwargs)

    _run_client_stream(ctx, client_call, apply_chunk_fn=apply_chunk_fn, on_done_fn=on_done_fn, on_error_fn=on_error_fn, on_status_fn=on_status_fn, stop_checker=stop_checker, name="stream-completion", include_status=True)


def run_stream_async(ctx, client, messages, tools=None, apply_chunk_fn=None, on_done_fn=None, on_error_fn=None, max_tokens=None, stop_checker=None):
    """Compatibility helper for legacy run_stream_async calls (using messages/tools)."""
    # crosshair: off

    effective_max = max_tokens or 512

    def client_call(**cb_kwargs):
        if tools:
            client.stream_request_with_tools(messages, effective_max, tools=tools, **cb_kwargs)
        else:
            client.stream_chat_response(messages, effective_max, **cb_kwargs)

    _run_client_stream(ctx, client_call, apply_chunk_fn=apply_chunk_fn, on_done_fn=on_done_fn, on_error_fn=on_error_fn, stop_checker=stop_checker, name="stream-async", include_status=False)


def run_blocking_in_thread(
    ctx,
    func,
    *args,
    pump_idle: bool = True,
    stop_checker: Callable[[], bool] | None = None,
    **kwargs,
):
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

    Never runs *func* on the caller thread: a missing Toolkit used to fall back
    to a synchronous call, which blocked recalc with no worker isolation.

    The internal queue uses :class:`BlockingPumpKind` as the first tuple
    element only (same contract as :class:`StreamQueueKind` for the stream drain).
    """
    # crosshair: off
    q: "queue.Queue[BlockingPumpQueueItem]" = queue.Queue()

    def worker():
        try:
            result = func(*args, **kwargs)
            q.put((BlockingPumpKind.DONE, result))
        except BaseException as e:
            q.put((BlockingPumpKind.ERROR, e))

    toolkit = None
    if pump_idle:
        try:
            toolkit = ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        except Exception as e:
            log.warning("run_blocking_with_pump: Failed to create toolkit, waiting without pump. %s", e)
            toolkit = None

    run_in_background(worker, daemon=True, name="blocking-thread", dedicated=True)

    # Do not take drain_owner_scope here: this helper may run under an active stream
    # drain. pump_ui_idle remains the owner-safe VCL pump path.
    poll = (pump_idle and toolkit is not None) or stop_checker is not None
    while True:
        try:
            item = q.get(timeout=0.1 if poll else None)
            kind, data = item
            if not isinstance(kind, BlockingPumpKind):
                ek = TypeError("blocking pump queue item kind must be BlockingPumpKind, got %s" % (type(kind).__name__,))
                log.error("Invalid blocking pump tag: %s", ek)
                raise ek
            if kind == BlockingPumpKind.DONE:
                return data
            if kind == BlockingPumpKind.ERROR:
                raise data
        except queue.Empty:
            if stop_checker is not None and stop_checker():
                raise BlockingWaitStopped("stopped")
            if pump_idle and toolkit is not None:
                pump_ui_idle(toolkit)


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
