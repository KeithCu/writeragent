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
one callback for the idle wait, and it does not call ``pump_ui_idle`` —
returning to VCL between slices is what keeps Stop and paints alive.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, cast

from plugin.framework.blocking_wait import automatic_gc_suspended
from plugin.framework.worker_pool import run_in_background
from plugin.framework.stream_batch import (
    BatchingStreamQueue,
    StreamQueueKind,
    split_stream_item,
    wait_for_timer_deadline,
)
from plugin.framework.deal_shim import DEAL_MAX_TOKEN, UNDER_CROSSHAIR, ascii_bounded, deal
from plugin.framework.errors import format_error_payload
from plugin.framework.async_drain_guard import (
    NestedDrainOwnerError,
    acquire_drain_owner,
    drain_owner_scope,
    get_drain_owner,
    release_drain_owner,
)
from plugin.framework.queue_executor import (
    _marshal_thread_tag,
    async_callback_for_drain_rearm,
    default_executor,
    pump_ui_idle,
)

log = logging.getLogger(__name__)


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
    thinking_open: bool = False
    # NEXT_TOOL is not terminal. A true on_stream_done return is applied
    # only after the rest of this already-pulled batch (see _process_batch).
    defer_next_tool_exit: bool = False
    # Set immediately before on_error for an ERROR item or an invalid tag.
    # If that callback raises, _process_batch must not call it again.
    error_callback_entered: bool = False

    def close_thinking(self) -> None:
        # crosshair: off
        if self.thinking_open:
            self.apply_chunk_fn(" /thinking\n", True)
            self.thinking_open = False

    def flush_buffers(self) -> None:
        # crosshair: off
        if self.current_thinking:
            if not self.thinking_open:
                self.apply_chunk_fn("[Thinking] ", True)
                self.thinking_open = True
            self.apply_chunk_fn("".join(self.current_thinking), True)
            self.current_thinking.clear()
        if self.current_content:
            self.close_thinking()
            self.apply_chunk_fn("".join(self.current_content), False)
            self.current_content.clear()

    def finish_display(self) -> None:
        """Flush display text and close an open thinking block.

        Terminal handlers use this. ``flush_buffers`` alone leaves thinking
        open so the next batch can continue the same block.
        """
        # crosshair: off
        self.flush_buffers()
        self.close_thinking()

    def notify_stopped(self) -> None:
        self.job_done[0] = True
        try:
            self.on_stopped()
        except Exception:
            log.exception("on_stopped failed")


def _drain_ready(q: queue.Queue[Any], max_items: int | None = 50) -> list[Any]:
    """Items already queued. Does not block.

    ``max_items`` caps one event-drain slice so a fast producer cannot hold
    the VCL callback. ``None`` drains every ready item (the blocking loop).
    A blocking ``get`` here would sit inside the VCL callback and hold
    SolarMutex for the whole timeout. See :func:`run_stream_drain_loop`.
    """
    # crosshair: off
    items: list[Any] = []
    try:
        while max_items is None or len(items) < max_items:
            items.append(q.get_nowait())
    except queue.Empty:
        pass
    return items


def _drain_batch(q: queue.Queue[Any], timeout: float) -> list[Any]:
    """Block up to *timeout* for one item, then drain any immediately available extras."""
    # crosshair: off
    try:
        first = q.get(timeout=timeout)
    except queue.Empty:
        return []
    return [first, *_drain_ready(q, max_items=None)]


def _append_stream_text(
    target_attr: str,
    other_attr: str,
    state: _DrainState,
    data: Any,
    _item: Any,
    *,
    is_tool: bool = False,
) -> None:
    # crosshair: off
    if is_tool and not state.show_search_thinking:
        return
    if getattr(state, other_attr):
        state.flush_buffers()
    # BatchingStreamQueue stores ``data or ""``. A raw queue can put None,
    # and ``"".join`` then raises TypeError.
    getattr(state, target_attr).append(data or "")


def _handle_status(state: _DrainState, data: Any, _item: Any) -> None:
    # crosshair: off
    if state.on_status_fn:
        state.on_status_fn(data)


def _handle_stream_done_like(state: _DrainState, _data: Any, item: Any) -> None:
    # crosshair: off
    state.finish_display()
    if state.on_stream_done(item):
        state.job_done[0] = True


def _handle_next_tool(state: _DrainState, _data: Any, item: Any) -> None:
    """Advance a tool round. Do not end the drain in the middle of this batch.

    Notify once and keep this batch going. NEXT_TOOL is not the terminal
    handler: a true ``on_stream_done`` return must not set ``job_done`` and
    break ``_process_batch``, or items already pulled (the next chunk,
    ``STREAM_DONE``) are discarded and the pump stops. The generic worker
    wrapper always returns true, so that would make any ``NEXT_TOOL`` look
    like a finished stream. A true return is applied only after this batch,
    so the tail runs in this drain instead of being dropped for a later one.
    A false return leaves ``job_done`` clear and the loop keeps pumping.
    """
    # crosshair: off
    state.finish_display()
    state.defer_next_tool_exit = bool(state.on_stream_done(item))


def _append_tool_line(prefix: str, state: _DrainState, data: Any, _item: Any) -> None:
    # crosshair: off
    state.finish_display()
    state.apply_chunk_fn(_format_agent_tool_stream_line(prefix, data), False)


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
    state.finish_display()
    if not state.on_approval_required:
        # There is no dialog to answer. Set the event so the worker sees
        # approved still false and denies the request. Returning without
        # the event leaves the worker in wait_for_approval; only the
        # handler used to set it.
        log.warning("APPROVAL_REQUIRED with no handler; unblocking worker")
        _set_approval_event(item)
        return
    try:
        state.on_approval_required(item)
    except Exception:
        # Re-raise so the batch on_error path ends the drain, and set the
        # event so the worker is not parked after the UI has already
        # unblocked. Logging and returning leaves job_done false, and the
        # worker stays in wait_for_approval because only the handler sets
        # that event.
        _set_approval_event(item)
        raise


def _handle_stopped(state: _DrainState, _data: Any, _item: Any) -> None:
    # crosshair: off
    state.finish_display()
    # Mark the drain finished first. A raise is logged and is not turned
    # into on_error. Running on_stopped before job_done lets that raise
    # fall into _process_batch's except, which calls on_error, so Stop
    # shows as a stream failure.
    state.notify_stopped()


def _handle_error(state: _DrainState, data: Any, _item: Any) -> None:
    # crosshair: off
    state.finish_display()
    # Mark the callback as entered first. A raising on_error falls into
    # _process_batch's except, which would call on_error again with that
    # new exception. A flush that raises before this line leaves the flag
    # clear, so that except still reports the flush once.
    state.error_callback_entered = True
    # This item already decided. A fatal error sets job_done below; a true
    # on_error keeps the drain. Leaving defer_next_tool_exit set from a
    # NEXT_TOOL in this batch lets the trailing check set job_done, so the
    # replacement worker never runs.
    state.defer_next_tool_exit = False
    recovered = state.on_error(data) is True
    if not recovered:
        state.job_done[0] = True


_DISPATCH: dict[StreamQueueKind, Callable[[_DrainState, Any, Any], None]] = {
    StreamQueueKind.CHUNK: functools.partial(_append_stream_text, "current_content", "current_thinking"),
    StreamQueueKind.THINKING: functools.partial(_append_stream_text, "current_thinking", "current_content"),
    StreamQueueKind.STATUS: _handle_status,
    StreamQueueKind.STREAM_DONE: _handle_stream_done_like,
    StreamQueueKind.TOOL_DONE: _handle_stream_done_like,
    StreamQueueKind.FINAL_DONE: _handle_stream_done_like,
    StreamQueueKind.NEXT_TOOL: _handle_next_tool,
    StreamQueueKind.TOOL_THINKING: functools.partial(_append_stream_text, "current_thinking", "current_content", is_tool=True),
    StreamQueueKind.TOOL_CALL: functools.partial(_append_tool_line, "[Tool call]"),
    StreamQueueKind.TOOL_RESULT: functools.partial(_append_tool_line, "[Tool result]"),
    StreamQueueKind.APPROVAL_REQUIRED: _handle_approval_required,
    StreamQueueKind.STOPPED: _handle_stopped,
    StreamQueueKind.ERROR: _handle_error,
}


def _apply_display_item(state: _DrainState, item: Any) -> None:
    """Apply one CHUNK or THINKING. Set an approval event. Leave other kinds.

    Only a real ``StreamQueueKind`` member is display text. The enum is a
    ``str`` enum, so ``==`` treats a bare ``"chunk"`` string as CHUNK, and
    ``_process_batch`` rejects that tag. Stop shows it. A non-member in the
    stop tail is dropped with the other control items. Calling ``on_error``
    here would turn Stop into a failure. An approval event in the tail is
    set so the worker is not left in ``wait_for_approval``.
    """
    # crosshair: off
    raw_kind, data = split_stream_item(item)
    if not isinstance(raw_kind, StreamQueueKind):
        return
    if raw_kind == StreamQueueKind.CHUNK:
        _append_stream_text("current_content", "current_thinking", state, data, item)
    elif raw_kind == StreamQueueKind.THINKING:
        _append_stream_text("current_thinking", "current_content", state, data, item)
    elif raw_kind == StreamQueueKind.APPROVAL_REQUIRED:
        # Set the event and do not open the dialog. Dropping this item on
        # Stop leaves the event unset and the worker in wait_for_approval
        # until its stop poll.
        _set_approval_event(item)


def _apply_queued_display(state: _DrainState) -> None:
    """Apply CHUNK/THINKING already on the queue. Drop other kinds.

    Stop used to break without reading them, so text flushed from the 250ms
    batcher never reached the sidebar. Control items from the stopped attempt
    (STREAM_DONE, ERROR) are discarded with the get. An approval event is set.
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

    Apply the unconsumed display tail, flush the producer batcher, apply
    what it just queued, then close thinking. The idle path used to call
    on_stopped without flushing buffers or closing thinking, and text still
    inside the 250ms batcher was dropped. A stop after ``_drain_batch`` only
    read ``state.q``. CHUNK and THINKING already pulled into the local batch
    were gone, including the whole batch when Stop tripped on the first
    item. Control items in the tail are not dispatched. An approval event
    in that tail is set.
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
    state.finish_display()
    # Mark the drain finished first. A raise is logged and is not on_error.
    # This helper sits outside _process_batch's try. Running on_stopped
    # before job_done turns that raise into _notify_drain_failure or
    # _report_slice_error, so a checker Stop shows as a stream error.
    state.notify_stopped()


def _process_batch(state: _DrainState, items: list[Any], stop_checker: Callable[[], bool] | None, flush_pending: Callable[[], None] | None = None) -> None:
    # crosshair: off
    # Trailing flush_buffers() must still raise on the success path (outer catch
    # / test_stream_drain_loop_processing_error). Skip it after stop and after
    # an inner handler failure: those paths already flushed, and a second raise
    # would call on_error again.
    state.defer_next_tool_exit = False
    state.error_callback_entered = False
    skip_trailing_flush = False
    for index, item in enumerate(items):
        if stop_checker and stop_checker():
            log.info("run_stream_drain_loop: Stop requested via checker.")
            # This tail is already off state.q, so the queue read inside
            # _finish_on_stop cannot see it.
            _finish_on_stop(state, flush_pending, items[index:])
            # That second flush is not the success-path flush. Leaving
            # skip_trailing_flush False runs it after _finish_on_stop has
            # already flushed.
            skip_trailing_flush = True
            break

        raw_kind, data = split_stream_item(item)

        try:
            if not isinstance(raw_kind, StreamQueueKind):
                ek = TypeError("stream queue item kind must be StreamQueueKind, got %s" % (type(raw_kind).__name__,))
                log.error("Invalid stream queue tag: %s", ek)
                state.finish_display()
                # Mark the callback entered first, same as _handle_error. A
                # raise otherwise falls into the except below, which calls
                # on_error again with a different payload. A flush that
                # raises before this line still reports once.
                state.error_callback_entered = True
                state.on_error(format_error_payload(ek))
                state.job_done[0] = True
                break

            _DISPATCH[raw_kind](state, data, item)
        except Exception as loop_e:
            if state.error_callback_entered:
                # The callback already ran. Log, end the drain, and skip the
                # trailing flush. Calling on_error again reports the
                # exception from that callback, so the UI sees a second,
                # different error.
                log.exception("on_error failed")
                state.job_done[0] = True
                skip_trailing_flush = True
                break
            # Dispatch handler (chunk/thinking UI) raised. Re-queuing ERROR used
            # to continue the batch: later CHUNKs still applied, STREAM_DONE ran
            # as success, and on_error never ran. Call on_error inline.
            # Recovery drops the rest of this batch and leaves job_done clear
            # for the replacement worker. A fatal error sets job_done. Do not
            # also call on_stream_done (Writer restore vs finish).
            error_payload = format_error_payload(loop_e)
            log.exception("Stream processing failed")
            try:
                state.finish_display()
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
                # on_error already ran; do not flush again. Leaving
                # skip_trailing_flush False lets the trailing flush_buffers()
                # raise, and run_stream_drain_loop then calls on_error a
                # second time for the same failure. The fatal path already
                # skips it.
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
            # Same as the handler-raise path. Do not flush again, and do not
            # honor a deferred NEXT_TOOL exit. Leaving skip_trailing_flush
            # False lets the trailing flush raise and call on_error again
            # after finish_display already flushed, and that deferred exit
            # still sets job_done after a recovered ERROR.
            skip_trailing_flush = True
            break

    if not skip_trailing_flush:
        state.flush_buffers()
    # Honor a true NEXT_TOOL return only after the pulled batch is done,
    # and not after stop or a recovered error (those already decided).
    # Breaking above on that return drops the tail.
    if state.defer_next_tool_exit and not state.job_done[0] and not skip_trailing_flush:
        state.job_done[0] = True


# Idle re-arm. Same cadence as the old ``Queue.get(0.1)`` wait, inside the
# 50–100 ms band. Immediate re-arm is used only when items are already queued,
# so an empty stream does not spin the main thread.
_DRAIN_IDLE_REARM_SEC = 0.1
_IDLE_REARM_RETRY_MAX_SEC = 1.0
_IDLE_REARM_MAX_FAILURES = 8

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

    A send callback calls this first, and ``run_stream_drain_loop`` calls
    it on entry, so a deferral can only attach to a drain started in the
    same synchronous call. An open drain keeps its own epilogue list.
    ``_event_drain`` stays set while a drain is open, across VCL callbacks.
    With two documents streaming, document B's send can return before
    starting a drain of its own (Stop before the drain, an error, a
    nested-owner refusal) and then defer its completion onto document A's
    drain, so B's buttons and status wait for A to finish.
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


class _IdleRearmThread:
    """One dedicated thread that pokes the main thread after a delay.

    A fresh thread per idle would churn for the whole stream. ``arm`` only
    moves the deadline. The thread never touches UNO objects it created;
    the fire callable (``addCallback``) is the same one workers already use.
    """

    _cv: threading.Condition
    _deadline: float | None
    _fire: Callable[[], None] | None
    _on_give_up: Callable[[], None] | None
    _generation: int
    _failures: int
    _started: bool
    _stopped: bool

    def __init__(self) -> None:
        # crosshair: off
        self._cv = threading.Condition()
        self._deadline = None
        self._fire = None
        self._on_give_up = None
        self._generation = 0
        self._failures = 0
        self._started = False
        self._stopped = False

    def arm(self, delay: float, fire: Callable[[], None], on_give_up: Callable[[], None] | None = None) -> None:
        # crosshair: off
        with self._cv:
            if self._stopped:
                return
            self._generation += 1
            self._failures = 0
            self._fire = fire
            self._on_give_up = on_give_up
            self._deadline = time.monotonic() + delay
            if not self._started:
                run_in_background(self._run, name="drain-rearm", dedicated=True)
                self._started = True
            self._cv.notify()

    def cancel(self) -> None:
        # crosshair: off
        with self._cv:
            self._generation += 1
            self._failures = 0
            self._deadline = None
            self._fire = None
            self._on_give_up = None
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
                deadline = wait_for_timer_deadline(self._cv, self)
                if deadline is None:
                    return
                generation = self._generation
                fire = self._fire
                if fire is None:
                    continue
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._cv.wait(timeout=remaining)
                    continue
                # This thread still holds the lock and did not wait, so a
                # newer arm or cancel cannot have changed the generation.
                self._deadline = None
                self._fire = None
            try:
                fire()
                with self._cv:
                    self._failures = 0
            except Exception:
                # A lost idle re-arm means the drain never runs another slice
                # (owner held, Send stuck on Stop, Stop cannot recover).
                # Re-queue the same fire with capped backoff unless the drain
                # stopped or re-armed meanwhile.
                give_up = False
                give_up_cb = None
                delay = None
                failures = 0
                with self._cv:
                    if not self._stopped and self._deadline is None and self._generation == generation:
                        self._failures += 1
                        failures = self._failures
                        if self._failures >= _IDLE_REARM_MAX_FAILURES:
                            # Bugfix: unbounded 1 s retries held the drain owner until process
                            # exit when addCallback stayed dead (dispose/shutdown).
                            self._fire = None
                            give_up = True
                            give_up_cb = self._on_give_up
                        else:
                            delay = min(_IDLE_REARM_RETRY_MAX_SEC, _DRAIN_IDLE_REARM_SEC * 2**min(failures - 1, 4))
                            self._deadline = time.monotonic() + delay
                            self._fire = fire
                            self._cv.notify()
                if give_up:
                    log.warning("drain idle re-arm failed %d times; giving up", failures)
                    if give_up_cb is not None:
                        try:
                            give_up_cb()
                        except Exception:
                            log.exception("drain idle give-up callback failed")
                elif delay is None:
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
    _pending: list[Callable[[], None]]
    _closed: bool
    _callback: Any
    _timer: _IdleRearmThread
    _on_idle_give_up: Callable[[], None] | None

    def __init__(self, service: Any) -> None:
        # crosshair: off
        self._service = service
        self._lock = threading.Lock()
        self._pending = []
        self._closed = False
        # Created on the main thread. The timer thread only calls addCallback.
        self._callback = _new_xcallback(self._notify)
        self._timer = _IdleRearmThread()
        self._on_idle_give_up = None

    def bind_idle_give_up(self, fn: Callable[[], None]) -> None:
        self._on_idle_give_up = fn

    def _enqueue(self, fn: Callable[[], None]) -> None:
        """Queue one slice. A second writer cannot replace it.

        Append this closure. ``notify`` runs one and pokes again if more
        are waiting, so a late idle callback cannot replace a slice already
        queued. ``post`` and the idle timer both used to write ``_target``,
        then both called ``addCallback``. The idle fire could already be
        past its generation check, so it overwrote the new slice. Both
        ``notify`` calls then ran that stale closure, which returned on the
        generation mismatch, and nothing re-armed. The drain kept the pump
        owner. One slot, two writers.
        """
        # crosshair: off
        with self._lock:
            if self._closed:
                return
            self._pending.append(fn)
        try:
            self._poke()
        except Exception:
            # Bugfix: what was wrong: the slice stayed in _pending when addCallback raised.
            # How: the idle timer's retry calls the same fire, which is lambda: self._enqueue(fn),
            # so each failure appended another copy of that closure.
            # Why this fixes it: that append is removed on exception, and the retry enqueues one copy.
            # We take _lock and del the last entry that is the callable this call appended
            # (not pop() the tail, because another writer can append during _poke).
            with self._lock:
                for idx in range(len(self._pending) - 1, -1, -1):
                    if self._pending[idx] is fn:
                        del self._pending[idx]
                        break
            raise

    def _notify(self) -> None:
        # crosshair: off
        with self._lock:
            if not self._pending:
                return
            fn = self._pending.pop(0)
            more = bool(self._pending)
        try:
            fn()
        finally:
            # A slice that closes the drain clears _service. _poke then no-ops.
            if more:
                self._poke()

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
        self._enqueue(fn)

    def post_after(self, delay: float, fn: Callable[[], None]) -> None:
        # crosshair: off
        self._timer.arm(delay, lambda: self._enqueue(fn), on_give_up=self._on_idle_give_up)

    def close(self) -> None:
        # crosshair: off
        self._timer.stop()
        with self._lock:
            self._closed = True
            self._pending.clear()
            self._callback = None
            self._service = None
            self._on_idle_give_up = None


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
    _on_started: Callable[[], None] | None
    epilogues: list[Callable[[], None]]
    closed: bool
    _held: bool
    _generation: int
    _immediate_streak: int

    def __init__(
        self,
        state: _DrainState,
        scheduler: Any,
        stop_checker: Callable[[], bool] | None,
        flush_pending: Callable[[], None] | None,
        on_started: Callable[[], None] | None = None,
    ) -> None:
        # crosshair: off
        self._state = state
        self._scheduler = scheduler
        self._stop_checker = stop_checker
        self._flush_pending = flush_pending
        self._on_started = on_started
        self.epilogues = []
        self.closed = False
        self._held = False
        self._generation = 0
        self._immediate_streak = 0

    def start(self) -> None:
        """Take the pump owner and arm the first slice. Does not process items."""
        # crosshair: off
        global _event_drain
        acquire_drain_owner("stream")
        self._held = True
        _event_drain = self
        bind = getattr(self._scheduler, "bind_idle_give_up", None)
        if callable(bind):
            bind(self._on_idle_rearm_exhausted)
        try:
            self._schedule_next()
        except Exception as exc:
            log.exception("event drain failed to arm")
            self._fail(exc)
            return
        if self._on_started is not None:
            # Bugfix: the worker used to start before the first slice was armed,
            # so a failed addCallback left an LLM/tool thread writing into an
            # unread queue with no drain to see Stop.
            try:
                self._on_started()
            except Exception as exc:
                log.exception("drain on_drain_started failed")
                self._fail(exc)

    def _on_idle_rearm_exhausted(self) -> None:
        default_executor.post(self._abort_idle_rearm_exhausted, bound_scope=None)

    def _abort_idle_rearm_exhausted(self) -> None:
        if self.closed:
            return
        self._fail(RuntimeError("event drain idle re-arm failed %d times" % _IDLE_REARM_MAX_FAILURES))

    def _fail(self, exc: BaseException) -> None:
        # crosshair: off
        self._report_slice_error(exc)
        self._state.job_done[0] = True
        self._finish()

    def _schedule_next(self, delay: float | None = None) -> None:
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

        if delay is not None:
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
            items = _drain_ready(state.q)
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
            pending = state.q.qsize()

            elapsed = time.monotonic() - t0
            if elapsed > 0.1:
                log.debug("event drain slice took %.3fs", elapsed)

            if pending == 0:
                self._immediate_streak = 0
                self._schedule_next(_DRAIN_IDLE_REARM_SEC)
            elif self._immediate_streak >= _DRAIN_MAX_IMMEDIATE_REARMS:
                self._immediate_streak = 0
                self._schedule_next(_DRAIN_YIELD_GAP_SEC)
            else:
                self._immediate_streak += 1
                self._schedule_next()
        except Exception as exc:
            log.exception("event drain slice failed")
            self._fail(exc)

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
            release_drain_owner()
        epilogues = self.epilogues
        self.epilogues = []
        for fn in epilogues:
            try:
                fn()
            except Exception:
                log.exception("drain epilogue failed")


def _notify_drain_failure(
    on_error: Callable[[Any], Any],
    job_done: list[bool],
    exc: BaseException,
    log_message: str,
    level: int = logging.ERROR,
) -> None:
    """Log *exc*, report it once, and end the drain.

    Set ``job_done`` first, notify once, and log if that notify raises.
    Each crash tail used to copy this sequence, and the queue-drain tail
    called ``on_error`` outside a try. A raising handler then hit the outer
    except and was reported again.
    """
    # crosshair: off
    if level == logging.WARNING:
        log.warning("%s: %s", log_message, exc)
    else:
        log.exception(log_message)
    job_done[0] = True
    try:
        on_error(format_error_payload(exc))
    except Exception:
        log.exception("Failed to notify error handler")


def run_stream_drain_loop(
    q: queue.Queue[Any],
    toolkit: Any,
    job_done: list[bool],
    apply_chunk_fn: Callable[[str, bool], None],
    on_stream_done: Callable[..., Any],
    on_stopped: Callable[[], None],
    on_error: Callable[[Any], Any],
    on_status_fn: Callable[[str], None] | None = None,
    show_search_thinking: bool = False,
    on_approval_required: Callable[..., None] | None = None,
    stop_checker: Callable[[], bool] | None = None,
    flush_pending: Callable[[], None] | None = None,
    *,
    rearm: Any = None,
    on_drain_started: Callable[[], None] | None = None,
) -> None:
    """
    Main-thread drain: batches items from the queue, manages thinking/chunk
    buffers, and dispatches to callbacks.

    When *rearm* is passed, or ``AsyncCallback`` can be armed, process the
    items already queued and return to the VCL loop. The next slice is an
    ``addCallback`` (queue non-empty) or a ~100 ms idle re-arm (queue empty).
    That event-driven path does **not** call ``pump_ui_idle``; each slice
    returns to VCL so paints and Stop stay responsive. Callers that used to
    run after this function returns must use :func:`defer_until_drain_done`
    so that work still waits for the terminal slice. Without a callback
    (unit tests via :func:`set_drain_scheduler_override`, eval harness,
    force-marshal) the blocking loop still runs ``q.get`` + ``pump_ui_idle``.

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
    - (APPROVAL_REQUIRED, ...): HITL; call on_approval_required(item). If no
    handler is registered, the approval event is set and no dialog is shown.
    - (STOPPED, ignored): Calls on_stopped() (second element unused).
    - (ERROR, payload): Calls on_error(payload). If on_error returns True, the
    drain keeps running (handler recovered, e.g. STT fallback spawned a new
    worker on this queue) but drops the rest of the batch already pulled.
    A true NEXT_TOOL return earlier in that batch does not end the recovery.
    Any other return value ends the loop. If on_error itself raises, it is
    not called again. A dispatch handler that raises is the same contract
    (inline on_error, no re-queue, no on_stream_done).
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
        _run_stream_drain_blocking(state, toolkit, stop_checker, flush_pending, on_drain_started=on_drain_started)
        return
    try:
        # Same-name "stream" re-entry is allowed. acquire_drain_owner raises
        # NestedDrainOwnerError for a different owner (for example MCP).
        _EventDrain(state, scheduler, stop_checker, flush_pending, on_started=on_drain_started).start()
    except Exception as exc:
        # Drop the callback the same way a finished drain does. acquire
        # raises before _held, so _finish never closes the re-arm and the
        # XCallback waits on cyclic GC. arm() has not run, so there is no
        # idle thread. A start() that returns already closed from _finish;
        # do not close again.
        closer = getattr(scheduler, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:
                log.exception("drain re-arm close failed")
        if isinstance(exc, NestedDrainOwnerError):
            _notify_drain_failure(on_error, job_done, exc, "Nested stream drain rejected", level=logging.WARNING)
        else:
            _notify_drain_failure(on_error, job_done, exc, "Stream drain loop crashed")


# Pytest (and similar) can force the blocking drain without sniffing MagicMock
# inside queue_executor. A factory that returns None selects the blocking path.
_drain_scheduler_override: Callable[[], Any | None] | None = None


def set_drain_scheduler_override(factory: Callable[[], Any | None] | None) -> Callable[[], Any | None] | None:
    """Install a factory used by :func:`_make_drain_rearm` instead of production.

    Returns the previous factory so callers can restore it. Pass ``None`` to
    clear. Headless pytest sets ``lambda: None`` so ``run_stream_drain_loop``
    keeps the blocking path (event-driven re-arm needs a live AsyncCallback).
    Explicit ``rearm=`` on :func:`run_stream_drain_loop` still bypasses this.
    """
    global _drain_scheduler_override
    previous = _drain_scheduler_override
    _drain_scheduler_override = factory
    return previous


def _make_drain_rearm() -> _AsyncCallbackRearm | None:
    """Production re-arm, or None when the blocking loop must be used."""
    # crosshair: off
    if _drain_scheduler_override is not None:
        return _drain_scheduler_override()
    service = async_callback_for_drain_rearm()
    if service is None:
        return None
    try:
        return _AsyncCallbackRearm(service)
    except Exception:
        log.exception("AsyncCallback re-arm unavailable; blocking drain")
        return None


def _run_stream_drain_blocking(
    state: _DrainState,
    toolkit: Any,
    stop_checker: Any,
    flush_pending: Any,
    on_drain_started: Callable[[], None] | None = None,
) -> None:
    """Blocking drain used when ``AsyncCallback`` cannot take the next slice.

    Unit tests and the eval harness have no VCL callback to return to.
    Do not use this loop when ``addCallback`` works. See :meth:`_EventDrain._slice`.
    """
    # crosshair: off
    q = state.q
    job_done = state.job_done
    on_error = state.on_error
    try:
        # drain_owner_scope("stream") allows that same name and raises for a
        # different one. The depth counter makes pump_ui_idle skip nested
        # VCL. Rejecting any existing owner, including "stream", makes
        # dual-deck peer sends raise NestedDrainOwnerError.
        # This loop holds SolarMutex between pumps; see automatic_gc_suspended.
        with drain_owner_scope("stream"), automatic_gc_suspended():
            if on_drain_started is not None:
                on_drain_started()
            while not job_done[0]:
                if stop_checker and stop_checker():
                    log.info("run_stream_drain_loop: Stop requested via checker.")
                    _finish_on_stop(state, flush_pending)
                    break

                items = _drain_batch(q, 0.1)

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
                    _notify_drain_failure(on_error, job_done, e, "run_stream_drain_loop batch processing failed")

                if toolkit:
                    pump_ui_idle(toolkit)

            if toolkit:
                pump_ui_idle(toolkit)

    except NestedDrainOwnerError as e:
        _notify_drain_failure(on_error, job_done, e, "Nested stream drain rejected", level=logging.WARNING)

    except Exception as e:
        _notify_drain_failure(on_error, job_done, e, "Stream drain loop crashed")


def _call_item_or_zero_arg(fn: Callable[..., None], item: Any) -> None:
    """Call ``fn(item)`` or ``fn()`` once, from the signature.

    Choose the call before invoking the callback. An exception from the
    body is not a retry. A ``TypeError`` whose text contains "positional
    argument" is not an arity mismatch: a ``TypeError`` raised inside the
    body can contain that text, and calling ``on_done`` again with no
    arguments runs it twice.
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


class StreamQueue(queue.Queue[Any]):
    """Thread-safe queue tracking terminal item occurrences for drain watchers.

    Replaces the runtime Queue.put monkeypatch. Individual drain callers
    subscribe with subscribe_terminal(), receiving a [False] cell that is set
    to True when any terminal StreamQueueKind item is put onto the queue.
    """

    _terminal_lock: threading.Lock
    _terminal_flags: list[list[bool]]

    def __init__(self, maxsize: int = 0) -> None:
        # crosshair: off
        super().__init__(maxsize)
        self._terminal_lock = threading.Lock()
        self._terminal_flags = []

    def subscribe_terminal(self) -> list[bool]:
        # crosshair: off
        flag = [False]
        with self._terminal_lock:
            self._terminal_flags.append(flag)
        return flag

    def unsubscribe_terminal(self, flag: list[bool]) -> None:
        # crosshair: off
        with self._terminal_lock:
            for index, existing in enumerate(self._terminal_flags):
                if existing is flag:
                    del self._terminal_flags[index]
                    break

    def put(self, item: Any, block: bool = True, timeout: float | None = None) -> None:
        # crosshair: off
        if self._terminal_flags:
            kind, _data = split_stream_item(item)
            if kind in _TERMINAL_WATCH_KINDS:
                with self._terminal_lock:
                    active = list(self._terminal_flags)
                for flag in active:
                    flag[0] = True
        super().put(item, block, timeout)


def _producer_batch(q: Any) -> tuple[queue.Queue[Any], Callable[[], None] | None]:
    """Return the drain queue and a flush when ``q`` is a producer batcher.

    ``run_async_worker_with_drain`` must not name ``BatchingStreamQueue``.
    A plain ``queue.Queue`` has neither ``raw`` nor ``flush``, so Stop and
    the worker finally leave it alone.
    """
    # crosshair: off
    raw = getattr(q, "raw", None)
    flush = getattr(q, "flush", None)
    if isinstance(raw, queue.Queue) and callable(flush):
        return raw, cast("Callable[[], None]", flush)
    return cast("queue.Queue[Any]", q), None


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
    q: Any = None,
    on_approval_required: Callable[[Any], None] | None = None,
) -> None:
    """Run a background worker and drain its queue on the main thread.

    The worker starts after the drain is armed (event) or after the blocking
    owner is taken; a drain that fails to start never starts the worker.
    Toolkit may be ``None``.

    ``q`` is a :class:`StreamQueue` or a producer batcher with ``raw`` and
    ``flush``. The drain reads ``raw`` and calls ``flush`` on Stop and
    before the terminal item.

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
        q = StreamQueue()
    job_done = [False]

    _real_q, _flush = _producer_batch(q)

    # Watch the real queue's terminal signals for this worker. Overlapping
    # drains on one queue each subscribe their own cell; see StreamQueue.
    # A watch that only sees puts through the queue passed to worker_fn misses
    # send_handlers, which closes over the real queue and puts
    # ERROR/STREAM_DONE there, so finally always posts a second STREAM_DONE.
    # That can end a recovered drain (on_error True) on a later iteration.
    # Tip: a plain queue.Queue cannot report terminal puts, so the wrapper
    # would always add a second STREAM_DONE. Fail loudly instead.
    if not isinstance(_real_q, StreamQueue):
        raise TypeError(f"run_async_worker_with_drain needs a StreamQueue (or a batcher over one), got {type(_real_q).__name__}")
    real_q: StreamQueue = _real_q

    def worker_wrapper() -> None:
        # Skip the STREAM_DONE sentinel when this wrapper or the worker
        # already queued a terminal item. Always queuing it after ERROR
        # makes a handler that returns True (keep draining, e.g. STT
        # fallback) end the job before the replacement worker's chunks.
        #
        # Log a flush error, still queue ERROR, and unsubscribe from finally.
        # Flushing the batcher before ERROR and again before unsubscribe, with
        # no try, skips that put when flush raises, leaves the failure flag
        # set so the STREAM_DONE fallback is suppressed.
        # Skip ERROR if saw_terminal[0] is set. A worker that already queued
        # a terminal item (such as ERROR) and then raised must not queue a
        # second ERROR, or a recovery on_error (returning True) runs a
        # second time against the replacement worker.
        error_item: tuple[Any, Any] | None = None
        saw_terminal = real_q.subscribe_terminal()
        try:
            try:
                # Pass the real queue (or batcher). Puts to StreamQueue notify subscribers.
                worker_fn(cast("queue.Queue[Any]", q))
            except BaseException as e:
                error_item = (StreamQueueKind.ERROR, format_error_payload(e))
                if saw_terminal[0]:
                    # Bugfix: A worker that already queued a terminal item (ERROR, STREAM_DONE,
                    # etc.) and then raised had its exception silently dropped without any log.
                    # This happened because saw_terminal[0] prevents a duplicate ERROR from being
                    # queued (which would trigger recovery twice). We now log.exception here so
                    # the traceback is preserved without queuing a duplicate ERROR.
                    log.exception("Worker raised after terminal item was already queued; exception not queued")
            try:
                if _flush is not None:
                    _flush()
            except Exception:
                log.exception("producer batch flush before terminal failed")
            if error_item is not None and not saw_terminal[0]:
                real_q.put(error_item)
            elif not saw_terminal[0]:
                real_q.put((StreamQueueKind.STREAM_DONE, None))
        finally:
            real_q.unsubscribe_terminal(saw_terminal)

    from plugin.framework.uno_context import get_toolkit

    toolkit = get_toolkit(ctx)

    # Refuse before spawn: this helper refuses every current owner, including
    # "stream", before spawning a worker thread. The underlying drain would
    # accept "stream" (nesting depth) and reject a different name. Refusing here
    # ensures Extend, image, web, and agent tasks do not nest a second consumer
    # over an active stream. Callers that must overlap two documents call
    # run_stream_drain_loop directly. A different owner ("mcp", "outer") must
    # not spawn either, as the drain would raise and leave the worker writing
    # to an unread queue.
    existing_owner = get_drain_owner()
    if existing_owner is not None:
        nested = NestedDrainOwnerError(f"Nested stream drain while {existing_owner!r} already owns the UI pump")
        if on_error_fn:
            try:
                on_error_fn(format_error_payload(nested))
            except Exception:
                log.exception("Failed to notify error handler for nested drain")
        return

    def on_stream_done_wrapper(item: Any) -> bool:
        if on_done_fn:
            _call_item_or_zero_arg(on_done_fn, item)
        # Return True so _handle_stream_done_like sets job_done[0] and the
        # drain loop exits. This is the sole exit path now that the worker
        # thread no longer sets job_done directly (see worker_wrapper comment).
        # NEXT_TOOL is not that exit. Returning true here used to stop the
        # pump before the next tool round. STREAM_DONE / FINAL_DONE /
        # TOOL_DONE still end the drain.
        kind, _payload = split_stream_item(item)
        if kind == StreamQueueKind.NEXT_TOOL:
            return False
        return True

    resolved_apply_chunk = apply_chunk_fn or (lambda _text, _is_thinking: None)
    resolved_on_error = on_error_fn or (lambda _payload: None)
    resolved_on_stopped = on_stopped_fn or (
        (lambda: _call_item_or_zero_arg(on_done_fn, None))
        if on_done_fn is not None
        else (lambda: None)
    )

    # Chat's tool loop passes flush_pending so Stop emits text still inside
    # the 250ms batcher. This helper accepted a batcher and only flushed it
    # in the worker finally, so Stop could return with up to one interval
    # of already-produced text still buffered.
    def _spawn_worker() -> None:
        run_in_background(worker_wrapper, daemon=True, name=name, dedicated=True)

    run_stream_drain_loop(
        _real_q,
        toolkit,
        job_done,
        resolved_apply_chunk,
        on_stream_done=on_stream_done_wrapper,
        on_stopped=resolved_on_stopped,
        on_error=resolved_on_error,
        on_status_fn=on_status_fn,
        on_approval_required=on_approval_required,
        stop_checker=stop_checker,
        flush_pending=_flush,
        on_drain_started=_spawn_worker,
    )


def run_stream_completion_async(
    ctx: Any,
    client: Any,
    prompt: Any,
    system_prompt: Any,
    max_tokens: Any,
    apply_chunk_fn: Callable[[str, bool], None] | None,
    on_done_fn: Callable[..., None] | None,
    on_error_fn: Callable[[Any], None] | None,
    on_status_fn: Callable[[str], None] | None = None,
    stop_checker: Callable[[], bool] | None = None,
) -> None:
    """High-level helper for simple non-tool streams (always chat completions)."""
    # crosshair: off
    # Batch CHUNK/THINKING so Extend/Edit selection does not wake the drain
    # per token. STATUS still flushes (BatchingStreamQueue boundary).
    batched = BatchingStreamQueue(StreamQueue(), batch_interval=0.25)

    def worker(q: queue.Queue[Any]) -> None:
        kwargs: dict[str, Any] = {
            "append_callback": lambda t: q.put((StreamQueueKind.CHUNK, t)),
            "append_thinking_callback": lambda t: q.put((StreamQueueKind.THINKING, t)),
            "status_callback": lambda t: q.put((StreamQueueKind.STATUS, t)),
            "stop_checker": stop_checker,
        }
        client.stream_completion(prompt, system_prompt, max_tokens, **kwargs)
        if stop_checker and stop_checker():
            q.put((StreamQueueKind.STOPPED, None))

    run_async_worker_with_drain(
        ctx,
        worker,
        apply_chunk_fn=apply_chunk_fn,
        on_done_fn=on_done_fn,
        on_error_fn=on_error_fn,
        on_status_fn=on_status_fn,
        stop_checker=stop_checker,
        name="stream-completion",
        q=batched,
    )
