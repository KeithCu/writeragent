# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unified main thread execution via queue system.

The MCP HTTP server runs in daemon threads. UNO is NOT thread-safe:
calling it from a background thread causes black menus, crashes on large
docs, and random corruption.

Solution: use com.sun.star.awt.AsyncCallback.addCallback() to post work
into the VCL event loop. The HTTP thread blocks on a threading.Event
until the main thread has executed the work item and stored the result.

Fallback: if AsyncCallback is unavailable (unit-test, headless without
a toolkit), the function is called directly with a warning.

Concurrency: ``_claim_lock`` decides whether a timed-out waiter or the
main thread “owns” a queued function so UNO does not run after the
caller has given up. ``llm_request_lane`` and the grammar in-flight
counter serialize **HTTP to a local LLM** (Ollama/llama.cpp often serve
one request). They are not UNO locks. Document and widget work from the
MCP HTTP thread still comes through this queue onto the UI thread.
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, cast, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

log = logging.getLogger("writeragent.framework.queue_executor")

# Layer B pytest: when True, execute/post always enqueue even under WRITERAGENT_TESTING=1.
_force_marshal_mode = False
# Optional poke handler (tests): runs process_queue on the designated main thread.
_test_poke_handler: Callable[["QueueExecutor"], None] | None = None

_AGENT_ACTIVE_LOCK = threading.Lock()
_AGENT_ACTIVE_COUNT = 0
_LLM_REQUEST_LOCK = threading.Lock()
_GRAMMAR_INFLIGHT_LOCK = threading.Lock()
_GRAMMAR_INFLIGHT_CV = threading.Condition(_GRAMMAR_INFLIGHT_LOCK)
_GRAMMAR_INFLIGHT_COUNT = 0
_current_send_cancellation: ContextVar["SendCancellation | None"] = ContextVar("current_send_cancellation", default=None)

# Drain ownership: re-export from async_drain_guard (single-owner VCL pump sentry).
from plugin.framework.async_drain_guard import (
    NestedDrainOwnerError as NestedDrainOwnerError,
    drain_owner_scope as drain_owner_scope,
    get_drain_owner as get_drain_owner,
    get_suppressed_vcl_pump_count as get_suppressed_vcl_pump_count,
    note_suppressed_vcl_pump as note_suppressed_vcl_pump,
    reset_suppressed_vcl_pump_count as reset_suppressed_vcl_pump_count,
)

_note_suppressed_vcl_pump = note_suppressed_vcl_pump



def set_force_marshal_mode(enabled: bool) -> None:
    """Test hook: force cross-thread marshal via the work queue (Layer B)."""
    global _force_marshal_mode
    _force_marshal_mode = enabled


def get_force_marshal_mode() -> bool:
    return _force_marshal_mode


def set_test_poke_handler(handler: Callable[["QueueExecutor"], None] | None) -> None:
    """Test hook: replace AsyncCallback poke with a synthetic main-thread pump."""
    global _test_poke_handler
    _test_poke_handler = handler


class SendCancelled(Exception):
    """Raised when main-thread work is skipped because the user stopped the send."""


class SendCancellation:
    """Per-send cancellation: flag, registered HTTP clients, and optional hooks."""

    __slots__ = ("_cancelled", "_lock", "_hooks", "_executors")

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._hooks: list[Callable[[], None]] = []
        self._executors: list[QueueExecutor] = []

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def bind_executor(self, executor: "QueueExecutor") -> None:
        """Track a :class:`QueueExecutor` whose pending work Stop must cancel."""
        with self._lock:
            if executor not in self._executors:
                self._executors.append(executor)

    def register_client(self, client: Any) -> None:
        # Resolve .stop() at registration time so cancel() only needs one list of
        # plain callables — no duck-type dispatch needed there.
        stop = getattr(client, "stop", None)
        if callable(stop):
            with self._lock:
                self._hooks.append(cast("Callable[[], None]", stop))

    def register_on_cancel(self, hook: Callable[[], None]) -> None:
        with self._lock:
            self._hooks.append(hook)

    def cancel(self) -> None:
        if self._cancelled.is_set():
            return
        self._cancelled.set()
        # Snapshot under the lock so concurrent register_* calls during
        # cancellation don't produce a torn iteration.
        with self._lock:
            hooks = list(self._hooks)
            executors = list(self._executors)
        for hook in hooks:
            try:
                hook()
            except Exception:
                log.exception("SendCancellation: error in cancel hook")
        if not executors:
            executors = [default_executor]
        for executor in executors:
            executor.cancel_pending_work()


def get_current_send_cancellation() -> SendCancellation | None:
    return _current_send_cancellation.get()


def bind_send_stop_checker(scope: SendCancellation | None, fallback: Callable[[], bool] | None = None) -> Callable[[], bool]:
    """Return a stop predicate tied to *scope*, not the panel field.

    Worker threads must use this (or ``scope.is_cancelled``) so Stop stays latched after
    the main thread clears ``panel._send_cancellation`` when the drain loop exits.

    When both *scope* and *fallback* are set, either latch is enough: Stop can
    fire after SEND_CLICKED but before the deferred drain enters ``agent_session``.
    """
    if scope is not None and fallback is not None:
        def _cancelled() -> bool:
            return scope.is_cancelled() or fallback()

        return _cancelled
    if scope is not None:
        return scope.is_cancelled
    if fallback is not None:
        return fallback
    return lambda: False


@contextmanager
def agent_session(scope: SendCancellation | None = None) -> Generator[SendCancellation, None, None]:
    """Mark a chat/agent session as active and expose a :class:`SendCancellation` scope.

    Pass an existing *scope* when Stop must be able to cancel before the drain
    body starts (Send ``actionPerformed`` returns, then AsyncCallback runs drain).
    """
    global _AGENT_ACTIVE_COUNT
    if scope is None:
        scope = SendCancellation()
        scope.bind_executor(default_executor)
    token = _current_send_cancellation.set(scope)
    with _AGENT_ACTIVE_LOCK:
        _AGENT_ACTIVE_COUNT += 1
    abort = True
    try:
        yield scope
        abort = False
    finally:
        # Cancel on abort (exception / GeneratorExit), not on success. Stop and
        # disposing() call cancel() while still inside the with-body; do not
        # cancel again when _do_send returns normally after Stop.
        if abort and not scope.is_cancelled():
            scope.cancel()
        _current_send_cancellation.reset(token)
        with _AGENT_ACTIVE_LOCK:
            _AGENT_ACTIVE_COUNT = max(0, _AGENT_ACTIVE_COUNT - 1)


def is_agent_active() -> bool:
    with _AGENT_ACTIVE_LOCK:
        return _AGENT_ACTIVE_COUNT > 0


def _marshal_thread_tag(executor: "QueueExecutor | None" = None) -> str:
    """One-line thread context for marshal/deadlock diagnosis (writeragent_debug.log)."""
    from plugin.framework.thread_guard import get_background_task_name, on_main_thread

    cur = threading.current_thread()
    main = threading.main_thread()
    cur_name = getattr(cur, "name", repr(cur))
    cur_ident = getattr(cur, "ident", "?")
    py_main = cur is main
    ex = executor or default_executor
    try:
        qdepth = ex._work_queue.qsize()
    except Exception:
        qdepth = -1
    return (
        "thread=%r ident=%s py_main=%s logical_main=%s bg_task=%r agent_active=%s queue_depth=%s"
        % (cur_name, cur_ident, py_main, on_main_thread(), get_background_task_name(), is_agent_active(), qdepth)
    )


def _fn_label(fn: Callable) -> str:
    return getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or repr(fn)


@contextmanager
def llm_request_lane(timeout: float = 60.0) -> Generator[None, None, None]:
    """Serialize LLM requests when callers choose to opt in."""
    acquired = _LLM_REQUEST_LOCK.acquire(timeout=timeout)
    if not acquired:
        log.warning("llm_request_lane timed out after %ss waiting for LLM lock", timeout)
        raise TimeoutError("Timed out waiting for LLM request lane lock after %ss" % timeout)
    try:
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
    with _GRAMMAR_INFLIGHT_CV:
        while _GRAMMAR_INFLIGHT_COUNT >= limit:
            if not _GRAMMAR_INFLIGHT_CV.wait(timeout=timeout):
                log.warning("grammar_llm_request_gate timed out after %ss waiting for slot", timeout)
                raise TimeoutError("Timed out waiting for grammar request gate slot after %ss" % timeout)
        _GRAMMAR_INFLIGHT_COUNT += 1
    try:
        yield
    finally:
        with _GRAMMAR_INFLIGHT_CV:
            _GRAMMAR_INFLIGHT_COUNT = max(0, _GRAMMAR_INFLIGHT_COUNT - 1)
            _GRAMMAR_INFLIGHT_CV.notify_all()


class _WorkItem:
    __slots__ = ("id", "fn", "args", "kwargs", "blocking", "event", "result", "exception", "cancelled", "_claimed")

    def __init__(self, item_id, fn, args, kwargs, blocking=True):
        self.id = item_id
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.blocking = blocking
        self.event = threading.Event() if blocking else None
        self.result: Any = None
        self.exception: BaseException | None = None
        self.cancelled = False
        self._claimed = False


class QueueExecutor:
    """Execute functions on main thread using queue system."""

    def __init__(self, ctx: Any | None = None) -> None:
        from plugin.framework.thread_guard import _unwrap_uno

        self._ctx = _unwrap_uno(ctx) if ctx is not None else None
        self._work_queue: queue.Queue[Any] = queue.Queue()
        self._async_callback_service = None
        self._callback_instance = None
        self._init_lock = threading.Lock()
        self._claim_lock = threading.Lock()
        self._initialized = False

    def set_context(self, ctx: Any) -> None:
        """Update or set the UNO component context (e.g. at bootstrap)."""
        from plugin.framework.thread_guard import _unwrap_uno

        # Store the raw context: Layer A wraps get_ctx() results, and comparing
        # proxy vs target would reset initialization on every panel wire.
        raw = _unwrap_uno(ctx) if ctx is not None else None
        with self._init_lock:
            if self._ctx is not raw:
                self._ctx = raw
                # Reset initialization so AsyncCallback is re-created with the updated context if needed
                self._initialized = False
                self._async_callback_service = None
                self._callback_instance = None

    def _get_async_callback(self):
        """Lazily create the AsyncCallback UNO service and XCallback instance."""
        if self._initialized:
            return self._async_callback_service
        with self._init_lock:
            if self._initialized:
                return self._async_callback_service
            try:
                # Use the extension's self.ctx (set_context at bootstrap).
                # uno.getComponentContext() can return a different context and
                # cause AsyncCallback to be created in the wrong context — silently
                # making execute() pokes no-ops. Missing ctx is logged, not probed.
                #
                # Unwrap Layer A proxies before any UNO getattr. Creating
                # AsyncCallback from a worker is the marshal bootstrap: if the
                # guard fires here it calls execute_on_main_thread while this
                # lock is held, and the UI thread deadlocks in set_context().
                # Two defenses prevent this:
                #   1. _unwrap_uno() strips the guard proxy so UNO calls below
                #      don't trigger assert_main_thread at all.
                #   2. _notify_thread_violation (thread_guard.py) bails early
                #      when ``not default_executor._initialized``, which is
                #      exactly the state while this lock is held.
                # If you refactor here, preserve both or the bootstrap deadlocks.
                from plugin.framework.thread_guard import _unwrap_uno

                ctx = _unwrap_uno(self._ctx)
                # get_ctx() is @main_thread_only. This runs on the first worker
                # post/execute; calling it here raises, the except swallows it,
                # and we would fall through to a wrong context. Bootstrap
                # set_context() is the path that works. Missing ctx logs below
                # and leaves AsyncCallback unset (tests without VCL still run).
                if ctx is None:
                    log.warning(
                        "QueueExecutor has no component context; "
                        "call set_context() from bootstrap on the main thread"
                    )

                assert ctx is not None, "UNO component context is required for AsyncCallback"
                ctx_any = cast("Any", ctx)
                from plugin.framework.uno_context import get_service_manager

                smgr = _unwrap_uno(get_service_manager(ctx_any))
                assert smgr is not None, "ServiceManager unavailable on UNO context"
                self._async_callback_service = cast("Any", smgr).createInstanceWithContext(
                    "com.sun.star.awt.AsyncCallback", ctx_any
                )
                if self._async_callback_service is None:
                    raise RuntimeError("createInstance com.sun.star.awt.AsyncCallback returned None")
                self._callback_instance = self._make_callback_instance()
                log.info("QueueExecutor initialized (AsyncCallback ready)")
            except Exception as exc:
                log.warning("AsyncCallback unavailable (%s) — UNO calls will run in the HTTP thread (legacy behaviour)", exc)
                self._async_callback_service = None
            self._initialized = True
            return self._async_callback_service

    def _make_callback_instance(self):
        """Create a UNO XCallback that processes work items one at a time."""
        import unohelper
        from com.sun.star.awt import XCallback

        # We must keep a reference to `self` accessible inside the inner class
        executor = self

        class _MainThreadCallback(unohelper.Base, XCallback):
            """XCallback that processes ONE item per call.

            Processing one item at a time lets the VCL event loop handle
            other events (redraws, user input) between tool executions.
            """

            def notify(self, aData):
                executor.process_queue()

        return _MainThreadCallback()

    def process_queue(self):
        """Process one item from queue (called from main thread via AsyncCallback)."""
        try:
            item = self._work_queue.get_nowait()
        except queue.Empty:
            return

        fn_label = _fn_label(item.fn)
        log.debug("process_queue start fn=%s %s", fn_label, _marshal_thread_tag(self))

        # Atomically claim or cancel: whoever holds self._claim_lock first wins.
        # This closes the race where _wait_for_result times out and sets
        # item.cancelled=True just after this thread has already read it as False.
        with self._claim_lock:
            if item.cancelled:
                log.debug("QueueExecutor: skipping cancelled item %s (%s)", item.id, getattr(item.fn, "__name__", "<fn>"))
                if item.blocking and item.event and not item.event.is_set():
                    item.exception = SendCancelled()
                    item.event.set()
                return
            item._claimed = True  # caller's timeout can no longer cancel this execution

        try:
            item.result = item.fn(*item.args, **item.kwargs)
        except BaseException as exc:
            # Store KeyboardInterrupt/SystemExit too so the waiter re-raises
            # instead of seeing result=None while the exception hits VCL.
            item.exception = exc
        finally:
            if item.blocking and item.event:
                item.event.set()
            log.debug("process_queue done fn=%s %s", fn_label, _marshal_thread_tag(self))

        # Re-poke if more items waiting
        if not self._work_queue.empty():
            self._poke_main_thread()

    def _poke_main_thread(self):
        """Ask the VCL event loop to call our notify() callback."""
        if _test_poke_handler is not None:
            _test_poke_handler(self)
            return
        if self._async_callback_service is None or self._callback_instance is None:
            log.debug("poke skipped (no AsyncCallback) %s", _marshal_thread_tag(self))
            return
        try:
            # PyUNO rejects uno.Any for addCallback userData on Linux; None is accepted on supported LO builds.
            self._async_callback_service.addCallback(self._callback_instance, None)
        except Exception as e:
            log.warning("_poke_main_thread addCallback failed: %s %s", e, _marshal_thread_tag(self))

    def cancel_pending_work(self) -> None:
        """Mark queued main-thread work as cancelled and wake blocking waiters."""
        pending: list[_WorkItem] = []
        while True:
            try:
                pending.append(self._work_queue.get_nowait())
            except queue.Empty:
                break
        with self._claim_lock:
            for item in pending:
                item.cancelled = True
                if item.blocking and item.event and not item.event.is_set():
                    item.exception = SendCancelled()
                    item.event.set()

    def _enqueue_work(self, fn, args, kwargs, blocking=True):
        """Add work item to queue."""
        scope = get_current_send_cancellation()
        if scope is not None:
            scope.bind_executor(self)
        item_id = str(uuid.uuid4())
        item = _WorkItem(item_id, fn, args, kwargs, blocking)
        self._work_queue.put(item)
        self._poke_main_thread()
        return item

    def _wait_for_result(self, item, timeout):
        """Wait for and return result from main thread."""
        if not item.event.wait(timeout):
            # Atomically cancel only if process_queue hasn't already claimed
            # this item for execution. Without _claim_lock there was a window
            # where the main thread could start executing fn() after this thread
            # gave up, causing UNO calls to run against an abandoned caller.
            with self._claim_lock:
                if not item._claimed:
                    item.cancelled = True
            raise TimeoutError("Main-thread execution of %s timed out after %ss" % (getattr(item.fn, "__name__", str(item.fn)), timeout))

        # The redundant `if item.cancelled and item.exception` branch has been
        # removed: the unconditional check below covers it entirely.
        if item.exception is not None:
            raise item.exception

        return item.result

    def _is_logical_main_thread(self) -> bool:
        """True when the caller may run UNO work inline (real or designated main thread)."""
        from plugin.framework.thread_guard import on_main_thread

        return on_main_thread()

    def _may_run_marshal_inline(self) -> bool:
        """True only when the caller is the thread that may run UNO work inline.

        Do not use on_main_thread() alone: designated-main test hooks and LO embed quirks
        can mark workers as logical main while the drain loop runs on MainThread.
        """
        from plugin.framework.thread_guard import get_background_task_name, get_designated_main_thread

        if _force_marshal_mode:
            return False
        if get_background_task_name():
            return False
        current = threading.current_thread()
        designated = get_designated_main_thread()
        if designated is not None:
            return current is designated
        return current is threading.main_thread()

    def _should_run_inline(self) -> bool:
        """Whether to skip the queue and call *fn* on the caller's thread."""
        if _force_marshal_mode:
            return False
        import os

        if os.environ.get("WRITERAGENT_TESTING") == "1":
            return True
        return False

    def execute(self, fn: Callable, *args, timeout: float = 30.0, **kwargs) -> Any:
        """Execute function on main thread (blocking).

        If already on the main thread, calls directly (avoids deadlock).
        Otherwise blocks the calling thread up to *timeout* seconds.
        Raises TimeoutError if the main thread doesn't process the item in time.
        Re-raises any exception thrown by *fn*.
        """
        from plugin.framework.thread_guard import get_background_task_name, in_sync_host_dispatch

        fn_label = _fn_label(fn)
        tag = _marshal_thread_tag(self)
        bg_task = get_background_task_name()

        if self._may_run_marshal_inline():
            log.debug("marshal route=inline_logical_main fn=%s %s", fn_label, tag)
            return fn(*args, **kwargs)

        if in_sync_host_dispatch():
            msg = (
                "marshal refused: execute_on_main_thread called from synchronous host dispatch "
                "context (deadlock hazard #402, fn=%s)" % fn_label
            )
            log.error("%s %s", msg, tag)
            raise RuntimeError(msg)

        if bg_task:
            log.debug(
                "marshal route=force_enqueue (background task %r) fn=%s %s",
                bg_task,
                fn_label,
                tag,
            )
        elif self._is_logical_main_thread():
            log.debug(
                "marshal route=force_enqueue (logical main but not Python MainThread) fn=%s %s",
                fn_label,
                tag,
            )

        if self._should_run_inline() and not bg_task:
            log.debug("marshal route=inline_testing fn=%s %s", fn_label, tag)
            return fn(*args, **kwargs)

        svc = None if _force_marshal_mode else self._get_async_callback()

        if svc is None and not _force_marshal_mode:
            if is_agent_active() or bg_task:
                msg = "marshal refused: AsyncCallback unavailable from background thread (fn=%s)" % fn_label
                try:
                    raise RuntimeError(msg)
                except RuntimeError:
                    log.exception("%s %s", msg, tag)
                    raise
            # Fallback: call directly (not thread-safe).
            log.warning(
                "marshal route=fallback_no_async (UNO on caller thread) fn=%s %s",
                fn_label,
                tag,
            )
            return fn(*args, **kwargs)

        log.debug("marshal route=enqueue fn=%s %s", fn_label, tag)
        item = self._enqueue_work(fn, args, kwargs, blocking=True)
        return self._wait_for_result(item, timeout)

    def post(self, fn: Callable, *args, **kwargs) -> None:
        """Post function to main thread (non-blocking).

        Unlike execute, does not block or return a result.
        Used for UI updates from background threads.
        """
        from plugin.framework.thread_guard import get_background_task_name

        fn_label = _fn_label(fn)
        tag = _marshal_thread_tag(self)
        bg_task = get_background_task_name()

        if self._should_run_inline():
            log.debug("marshal route=post_inline_testing fn=%s %s", fn_label, tag)
            fn(*args, **kwargs)
            return

        svc = None if _force_marshal_mode else self._get_async_callback()
        if svc is None and not _force_marshal_mode:
            if bg_task:
                log.warning(
                    "marshal route=post_dropped (AsyncCallback unavailable, background task %r) fn=%s %s",
                    bg_task,
                    fn_label,
                    tag,
                )
                return
            log.warning(
                "marshal route=post_fallback_no_async (UNO on caller thread) fn=%s %s",
                fn_label,
                tag,
            )
            fn(*args, **kwargs)
            return

        log.debug("marshal route=post_enqueue fn=%s %s", fn_label, tag)
        self._enqueue_work(fn, args, kwargs, blocking=False)


# We can keep a global default instance to mimic the old main_thread behavior
# until everything is fully DI injected.
default_executor = QueueExecutor()


def execute_on_main_thread(fn, *args, timeout=30.0, **kwargs):
    """Legacy helper: Use default_executor.execute instead."""
    return default_executor.execute(fn, *args, timeout=timeout, **kwargs)


def post_to_main_thread(fn, *args, **kwargs):
    """Legacy helper: Use default_executor.post instead."""
    return default_executor.post(fn, *args, **kwargs)


def pump_main_thread_work_queue(*, max_items: int = 1, executor: QueueExecutor | None = None) -> None:
    """Process queued UNO work on the LO main thread (call from idle/drain loops).

    Async tools enqueue via :func:`execute_on_main_thread` while the chat drain loop
    waits for them; this must run on the same thread as ``run_stream_drain_loop`` so
    workers are not blocked on AsyncCallback alone.
    """
    ex = executor or default_executor
    processed = 0
    for _unused in range(max_items):
        if ex._work_queue.empty():
            break
        ex.process_queue()
        processed += 1
    if processed:
        log.debug("pump_main_thread_work_queue processed=%d %s", processed, _marshal_thread_tag(ex))


def _pump_vcl_events(toolkit: Any) -> bool:
    """Call toolkit.processEventsToIdle(); only used from approved pump entry points."""
    if toolkit is not None and hasattr(toolkit, "processEventsToIdle"):
        try:
            toolkit.processEventsToIdle()
            return True
        except Exception:
            log.debug("processEventsToIdle failed", exc_info=True)
    return False


def pump_ui_idle(toolkit: Any, *, max_queue_items: int = 1, executor: QueueExecutor | None = None) -> None:
    """Idle tick for main-thread wait loops: drain QueueExecutor then pump VCL events.

    This is the drain-owner path: always pumps VCL when a toolkit is present so chat
    Send stays responsive. Secondary UI progress must use
    :func:`plugin.framework.uno_context.process_events_to_idle`, which no-ops while
    a :func:`drain_owner_scope` is active.
    """
    pump_main_thread_work_queue(max_items=max_queue_items, executor=executor)
    _pump_vcl_events(toolkit)
