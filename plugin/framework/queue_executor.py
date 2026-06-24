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

    __slots__ = ("_cancelled", "_clients_lock", "_clients", "_on_cancel_hooks")

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._clients_lock = threading.Lock()
        self._clients: list[Any] = []
        self._on_cancel_hooks: list[Callable[[], None]] = []

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def register_client(self, client: Any) -> None:
        with self._clients_lock:
            self._clients.append(client)

    def register_on_cancel(self, hook: Callable[[], None]) -> None:
        self._on_cancel_hooks.append(hook)

    def cancel(self) -> None:
        if self._cancelled.is_set():
            return
        self._cancelled.set()
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            stop = getattr(client, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    log.exception("SendCancellation: error stopping registered LlmClient")
        for hook in self._on_cancel_hooks:
            try:
                hook()
            except Exception:
                log.exception("SendCancellation: error in on_cancel hook")
        default_executor.cancel_pending_work()


def get_current_send_cancellation() -> SendCancellation | None:
    return _current_send_cancellation.get()


def bind_send_stop_checker(scope: SendCancellation | None, fallback: Callable[[], bool] | None = None) -> Callable[[], bool]:
    """Return a stop predicate tied to *scope*, not the panel field.

    Worker threads must use this (or ``scope.is_cancelled``) so Stop stays latched after
    the main thread clears ``panel._send_cancellation`` when the drain loop exits.
    """
    if scope is not None:
        return scope.is_cancelled
    if fallback is not None:
        return fallback
    return lambda: False


@contextmanager
def agent_session() -> Generator[SendCancellation, None, None]:
    """Mark a chat/agent session as active and expose a :class:`SendCancellation` scope."""
    global _AGENT_ACTIVE_COUNT
    scope = SendCancellation()
    token = _current_send_cancellation.set(scope)
    with _AGENT_ACTIVE_LOCK:
        _AGENT_ACTIVE_COUNT += 1
    try:
        yield scope
    finally:
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
def llm_request_lane() -> Generator[None, None, None]:
    """Serialize LLM requests when callers choose to opt in."""
    _LLM_REQUEST_LOCK.acquire()
    try:
        yield
    finally:
        _LLM_REQUEST_LOCK.release()


@contextmanager
def grammar_llm_request_gate(ctx: Any) -> Generator[None, None, None]:
    """Gate grammar proofreader HTTP: limit=1 uses global lane; limit>1 allows N parallel grammar calls."""
    from plugin.writer.locale.grammar_proofread_locale import grammar_max_in_flight

    limit = grammar_max_in_flight(ctx)
    if limit <= 1:
        with llm_request_lane():
            yield
        return
    global _GRAMMAR_INFLIGHT_COUNT
    with _GRAMMAR_INFLIGHT_CV:
        while _GRAMMAR_INFLIGHT_COUNT >= limit:
            _GRAMMAR_INFLIGHT_CV.wait()
        _GRAMMAR_INFLIGHT_COUNT += 1
    try:
        yield
    finally:
        with _GRAMMAR_INFLIGHT_CV:
            _GRAMMAR_INFLIGHT_COUNT = max(0, _GRAMMAR_INFLIGHT_COUNT - 1)
            _GRAMMAR_INFLIGHT_CV.notify_all()


class _WorkItem:
    __slots__ = ("id", "fn", "args", "kwargs", "blocking", "event", "result", "exception", "cancelled")

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


class QueueExecutor:
    """Execute functions on main thread using queue system."""

    def __init__(self):
        self._work_queue = queue.Queue()
        self._async_callback_service = None
        self._callback_instance = None
        self._init_lock = threading.Lock()
        self._initialized = False

    def _get_async_callback(self):
        """Lazily create the AsyncCallback UNO service and XCallback instance."""
        if self._initialized:
            return self._async_callback_service
        with self._init_lock:
            if self._initialized:
                return self._async_callback_service
            try:
                import uno

                ctx = uno.getComponentContext()
                assert ctx is not None
                ctx_any = cast("Any", ctx)
                smgr = getattr(ctx_any, "ServiceManager", getattr(ctx_any, "getServiceManager", lambda: None)())
                assert smgr is not None
                self._async_callback_service = cast("Any", smgr).createInstanceWithContext("com.sun.star.awt.AsyncCallback", ctx_any)
                if self._async_callback_service is None:
                    raise RuntimeError("createInstance returned None")
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

        if item.cancelled:
            log.debug("QueueExecutor: skipping cancelled item %s (%s)", item.id, getattr(item.fn, "__name__", "<fn>"))
            if item.blocking and item.event and not item.event.is_set():
                item.exception = SendCancelled()
                item.event.set()
        else:
            try:
                item.result = item.fn(*item.args, **item.kwargs)
            except Exception as exc:
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
        for item in pending:
            item.cancelled = True
            if item.blocking and item.event and not item.event.is_set():
                item.exception = SendCancelled()
                item.event.set()

    def _enqueue_work(self, fn, args, kwargs, blocking=True):
        """Add work item to queue."""
        item_id = str(uuid.uuid4())
        item = _WorkItem(item_id, fn, args, kwargs, blocking)
        self._work_queue.put(item)
        self._poke_main_thread()
        return item

    def _wait_for_result(self, item, timeout):
        """Wait for and return result from main thread."""
        if not item.event.wait(timeout):
            # Main thread hasn't picked this up in time. Mark it cancelled
            # so process_queue drops it instead of running the fn against an
            # abandoned caller.
            item.cancelled = True
            raise TimeoutError("Main-thread execution of %s timed out after %ss" % (getattr(item.fn, "__name__", str(item.fn)), timeout))

        if item.cancelled and item.exception is not None:
            raise item.exception

        if item.exception is not None:
            raise item.exception

        return item.result

    def _is_logical_main_thread(self) -> bool:
        """True when the caller may run UNO work inline (real or designated main thread)."""
        from plugin.framework.thread_guard import on_main_thread

        return on_main_thread()

    def _may_run_marshal_inline(self) -> bool:
        """True only on Python MainThread without a worker_pool background tag.

        Do not use on_main_thread() alone: designated-main test hooks and LO embed quirks
        can mark workers as logical main while the drain loop runs on MainThread.
        """
        from plugin.framework.thread_guard import get_background_task_name

        if get_background_task_name():
            return False
        return threading.current_thread() is threading.main_thread()

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
        from plugin.framework.thread_guard import get_background_task_name

        fn_label = _fn_label(fn)
        tag = _marshal_thread_tag(self)
        bg_task = get_background_task_name()

        if self._may_run_marshal_inline():
            log.debug("marshal route=inline_logical_main fn=%s %s", fn_label, tag)
            return fn(*args, **kwargs)

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
            if is_agent_active():
                msg = "marshal refused: AsyncCallback unavailable during agent session (fn=%s)" % fn_label
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
        if self._should_run_inline():
            fn(*args, **kwargs)
            return

        svc = None if _force_marshal_mode else self._get_async_callback()
        if svc is None and not _force_marshal_mode:
            fn(*args, **kwargs)
            return

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
    for _ in range(max_items):
        if ex._work_queue.empty():
            break
        ex.process_queue()
        processed += 1
    if processed:
        log.debug("pump_main_thread_work_queue processed=%d %s", processed, _marshal_thread_tag(ex))


def pump_ui_idle(toolkit: Any, *, max_queue_items: int = 1, executor: QueueExecutor | None = None) -> None:
    """Idle tick for main-thread wait loops: drain QueueExecutor then pump VCL events."""
    pump_main_thread_work_queue(max_items=max_queue_items, executor=executor)
    if toolkit is not None and hasattr(toolkit, "processEventsToIdle"):
        toolkit.processEventsToIdle()
