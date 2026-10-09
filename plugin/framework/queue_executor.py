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

If AsyncCallback is unavailable off the main thread, execute() raises
RuntimeError instead of calling the function on the caller. post() keeps a
short pending list and flushes it once AsyncCallback exists; it does not
run the callback on the background thread. A full list waits for a flush,
then raises TimeoutError. It does not drop the callable.

Concurrency: ``_claim_lock`` decides whether a timed-out waiter or the
main thread owns a queued function. An item that has not started is
cancelled so UNO does not run after the caller has given up. An item
already running on the UI thread is waited out so the caller sees that
result instead of retrying it. ``llm_request_lane`` and the grammar in-flight
counter serialize **HTTP to a local LLM** (Ollama/llama.cpp often serve
one request). They are not UNO locks. Document and widget work from the
MCP HTTP thread still comes through this queue onto the UI thread.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Callable, ClassVar, cast, TYPE_CHECKING
from plugin.framework.i18n import _

if TYPE_CHECKING:
    from collections.abc import Generator

log = logging.getLogger("writeragent.framework.queue_executor")

# Layer B pytest: when True, execute/post always enqueue even under WRITERAGENT_TESTING=1.
_force_marshal_mode = False
# Optional poke handler (tests): runs process_queue on the designated main thread.
_test_poke_handler: Callable[["QueueExecutor"], None] | None = None

_AGENT_ACTIVE_LOCK = threading.Lock()
_AGENT_ACTIVE_COUNT = 0
# Scopes whose agent_session has not exited yet (identity, under the lock).
_AGENT_OPEN_SCOPES: list["SendCancellation"] = []
_LLM_REQUEST_LOCK = threading.Lock()
_GRAMMAR_INFLIGHT_LOCK = threading.Lock()
_GRAMMAR_INFLIGHT_CV = threading.Condition(_GRAMMAR_INFLIGHT_LOCK)
_GRAMMAR_INFLIGHT_COUNT = 0
_current_send_cancellation: ContextVar["SendCancellation | None"] = ContextVar("current_send_cancellation", default=None)

# Posts queued before AsyncCallback exists. Past this, wait for a flush
# instead of dropping the callable. Same pressure signal as the grammar gate:
# wait, then TimeoutError.
_PENDING_POST_CAP = 32
_PENDING_POST_WAIT_SEC = 30.0
# Slice for the untimed wait on a claimed marshal (log + shutdown check only).
_UNTIMED_WAIT_SLICE_SEC = 5.0

# Drain ownership: re-export from async_drain_guard (single-owner VCL pump sentry).
from plugin.framework.async_drain_guard import (
    NestedDrainOwnerError as NestedDrainOwnerError,
    drain_owner_scope as drain_owner_scope,
    get_drain_depth as get_drain_depth,
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


class _ScopeUnset:
    """Marker: enqueue reads the current send instead of a caller-supplied scope."""


# ``_enqueue_work`` reads the current send unless the caller passes the scope
# that was current when a pending post was stored.
_SCOPE_UNSET = _ScopeUnset()


class SendCancelled(Exception):
    """Raised when main-thread work is skipped because the user stopped the send."""


def wait_for_approval(event: threading.Event, stop_checker: Callable[[], bool] | None, timeout: float = 0.2) -> bool:
    """Block until *event* is set, or the send is stopped.

    Approval used to call ``event.wait()`` with no stop check. Closing the
    sidebar latches stop and never signals the event, so the worker stayed
    parked after the drain ended. A set event wins a race with stop.
    """
    while not event.is_set():
        if stop_checker is not None and stop_checker():
            return False
        event.wait(timeout)
    return True


class SendCancellation:
    """Per-send cancellation: flag, registered HTTP clients, and optional hooks."""

    __slots__: ClassVar[tuple[str, ...]] = ("_cancelled", "_lock", "_hooks", "_executors")
    _cancelled: threading.Event
    _lock: threading.Lock

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
        # B13: Stop can fire before the drain creates/registers the client. If the
        # scope is already cancelled, call stop() immediately so the worker cannot
        # open a socket under llm_request_lane.
        stop = getattr(client, "stop", None)
        if not callable(stop):
            return
        with self._lock:
            self._hooks.append(cast("Callable[[], None]", stop))
            already = self._cancelled.is_set()
        if already:
            try:
                stop()
            except Exception:
                log.exception("SendCancellation: error stopping late-registered client")

    def register_on_cancel(self, hook: Callable[[], None]) -> None:
        with self._lock:
            self._hooks.append(hook)
            already = self._cancelled.is_set()
        # Stop can win the race: a hook registered after cancel() must still run.
        if already:
            try:
                hook()
            except Exception:
                log.exception("SendCancellation: error in late cancel hook")

    def cancel(self) -> None:
        # Set the flag under the lock so two callers cannot both run the hooks.
        with self._lock:
            if self._cancelled.is_set():
                return
            self._cancelled.set()
            hooks = list(self._hooks)
            executors = list(self._executors)
        for hook in hooks:
            try:
                hook()
            except Exception:
                log.exception("SendCancellation: error in cancel hook")
        # Unbound Stop only latches the flag. Falling back to default_executor
        # cancelled MCP, grammar, and peer items posted before the drain bound
        # this scope. The sidebar drain checks is_cancelled() and returns.
        if not executors:
            return
        for executor in executors:
            executor.cancel_pending_work(self)


def get_current_send_cancellation() -> SendCancellation | None:
    return _current_send_cancellation.get()


def _resolve_bound_scope(bound_scope: SendCancellation | None | _ScopeUnset) -> SendCancellation | None:
    """Current send when the caller did not pass a scope.

    ``is _SCOPE_UNSET`` is the only unset value. Checkers do not narrow ``is``
    against a class instance, so the else branch is cast.
    """
    if bound_scope is _SCOPE_UNSET:
        return get_current_send_cancellation()
    return cast("SendCancellation | None", bound_scope)


def _fail_closed(predicate: Callable[[], bool]) -> Callable[[], bool]:
    """Stop checker that treats a raising predicate as stopped.

    A checker that raises used to look like "not stopped" when the caller
    ignored the exception. Fail closed so a broken latch still aborts the send.
    """

    def _cancelled() -> bool:
        try:
            return predicate()
        except Exception:
            log.exception("stop_checker raised exception; failing closed (treating as stopped)")
            return True

    return _cancelled


def bind_send_stop_checker(scope: SendCancellation | None, fallback: Callable[[], bool] | None = None) -> Callable[[], bool]:
    """Return a stop predicate tied to *scope*, not the panel field.

    Worker threads must use this (or ``scope.is_cancelled``) so Stop stays latched after
    the main thread clears ``panel._send_cancellation`` when the drain loop exits.

    When both *scope* and *fallback* are set, either latch is enough: Stop can
    fire after SEND_CLICKED but before the deferred drain enters ``agent_session``.
    """
    if scope is not None and fallback is not None:
        return _fail_closed(lambda: scope.is_cancelled() or fallback())
    if scope is not None:
        return _fail_closed(scope.is_cancelled)
    if fallback is not None:
        return _fail_closed(fallback)
    return lambda: False


def capture_send_stop(host: Any) -> tuple[Any, Callable[[], bool]]:
    """Scope and stop checker frozen on the send thread at worker spawn.

    Call this before ``run_in_background``. The checker is whatever
    ``resolve_stop_checker`` returns now, which on the panel is
    ``bind_send_stop_checker`` closed over this scope object. The worker
    closes over both results and must not read the panel field again.
    Sub-agent and tool workers that call ``resolve_stop_checker()`` and
    read ``host._send_cancellation`` inside the thread body miss Stop:
    Stop's drain clears that field, and the next send stores a new scope
    there. A worker spawned but not yet in its body then binds the next
    send, so the old Stop is missed and the old worker can cancel the new
    one. ``run_in_background`` already copies contextvars at submit;
    ``get_current_send_cancellation`` stays that submit-time scope.
    """
    scope = getattr(host, "_send_cancellation", None)
    checker = host.resolve_stop_checker()
    return scope, checker


@contextmanager
def agent_session(scope: SendCancellation | None = None) -> Generator[SendCancellation, None, None]:
    """Mark a chat/agent session as active and expose a :class:`SendCancellation` scope.

    Pass an existing *scope* when Stop must be able to cancel before the drain
    body starts (Send ``actionPerformed`` returns, then AsyncCallback runs drain).
    """
    global _AGENT_ACTIVE_COUNT
    if scope is None:
        scope = SendCancellation()
    # Bind here, not when StartSendEffect creates the scope: Stop before the
    # deferred drain must not cancel_pending_work that posted closer.
    scope.bind_executor(default_executor)
    token = _current_send_cancellation.set(scope)
    with _AGENT_ACTIVE_LOCK:
        _AGENT_ACTIVE_COUNT += 1
        _AGENT_OPEN_SCOPES.append(scope)
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
        with _AGENT_ACTIVE_LOCK:
            _AGENT_ACTIVE_COUNT = max(0, _AGENT_ACTIVE_COUNT - 1)
            for i, open_scope in enumerate(_AGENT_OPEN_SCOPES):
                if open_scope is scope:
                    del _AGENT_OPEN_SCOPES[i]
                    break
            previous = token.old_value
            previous_open = any(open_scope is previous for open_scope in _AGENT_OPEN_SCOPES)
        _restore_send_cancellation(scope, token, previous_open)


def _restore_send_cancellation(scope: SendCancellation, token: Any, previous_open: bool) -> None:
    """Undo agent_session's ``set`` without resurrecting a finished scope.

    The event-driven drain keeps a session open across VCL callbacks, so
    two documents' sessions can exit in either order on the main thread.
    ``ContextVar.reset`` restores the value from when *this* session
    started. Exiting A first would reset B's live scope to None; exiting B
    would then restore A's finished (maybe cancelled) scope, and later
    main-thread work outside any session would be bound to it (posts
    dropped by its Stop, LLM lane and indexer waits seeing "cancelled").
    Leave the variable alone if another session is now current; when this
    scope is current, restore the earlier value only while that session is
    still open, otherwise None. Strict nesting behaves as before.
    """
    if _current_send_cancellation.get() is not scope:
        return
    try:
        if previous_open or token.old_value is None or token.old_value is Token.MISSING:
            _current_send_cancellation.reset(token)
        else:
            _current_send_cancellation.set(None)
    except ValueError:
        # Token from another Context (exit on a different thread/context).
        _current_send_cancellation.set(None)


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
        qdepth = ex.pending_work_count()
    except Exception:
        qdepth = -1
    return "thread=%r ident=%s py_main=%s logical_main=%s bg_task=%r agent_active=%s queue_depth=%s" % (cur_name, cur_ident, py_main, on_main_thread(), get_background_task_name(), is_agent_active(), qdepth)


def _log_marshal(level: int, msg: str, *args: Any, executor: "QueueExecutor | None" = None) -> None:
    """Log *msg* plus a marshal thread tag, only when *level* is enabled.

    ``log.debug(..., _marshal_thread_tag())`` still builds the tag when debug
    is off: the argument is evaluated before the logger checks the level.
    The tag takes ``is_agent_active``'s lock and reads queue depth, so the
    hot path (execute, post, process_queue) must not call it unless this
    record will be emitted. *msg* must end with a ``%s`` for the tag.
    """
    if not log.isEnabledFor(level):
        return
    # Named methods, not Logger.log: tests patch log.warning / log.debug.
    tag = _marshal_thread_tag(executor)
    if level == logging.DEBUG:
        log.debug(msg, *args, tag)
    elif level == logging.WARNING:
        log.warning(msg, *args, tag)
    elif level == logging.ERROR:
        log.error(msg, *args, tag)
    else:
        log.log(level, msg, *args, tag)


def _fn_label(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or repr(fn)


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

    while time.monotonic() < deadline:
        cancellation = get_current_send_cancellation()
        if cancellation and cancellation.is_cancelled():
            from plugin.framework.async_stream import BlockingWaitStopped
            raise BlockingWaitStopped("stopped")

        if _LLM_REQUEST_LOCK.acquire(timeout=0.25):
            acquired = True
            break

        if not notified_status and status_callback is not None:
            status_callback(_("Waiting for another document's reply..."))
            notified_status = True

    if not acquired:
        log.warning("llm_request_lane timed out after %ss waiting for LLM lock", timeout)
        raise TimeoutError("Timed out waiting for LLM request lane lock after %ss" % timeout)

    if notified_status and status_callback is not None:
        status_callback(resume_status or "")

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
            # One release frees one slot, so wake one waiter. notify_all() woke
            # every waiter; the extras rechecked the count and slept again.
            # CPython's condition deque is FIFO, so the oldest waiter runs.
            _GRAMMAR_INFLIGHT_CV.notify()


class _WorkItem:
    __slots__: ClassVar[tuple[str, ...]] = ("id", "fn", "args", "kwargs", "blocking", "event", "result", "exception", "cancelled", "_claimed", "scope")
    id: str
    fn: Any
    args: Any
    kwargs: Any
    blocking: bool
    event: threading.Event | None
    cancelled: bool
    _claimed: bool
    scope: SendCancellation | None

    def __init__(self, item_id: str, fn: Any, args: Any, kwargs: Any, blocking: bool = True, scope: SendCancellation | None = None) -> None:
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
        self.scope = scope


class QueueExecutor:
    """Execute functions on main thread using queue system."""

    _ctx: Any | None
    _async_callback_service: Any
    _callback_instance: Any
    _init_lock: threading.Lock
    _claim_lock: threading.Lock
    _pending_lock: threading.Condition
    _initialized: bool
    _logged_missing_ctx: bool
    _logged_async_callback_failure: bool
    _order_lock: threading.RLock

    def __init__(self, ctx: Any | None = None) -> None:
        from plugin.framework.thread_guard import _unwrap_uno

        self._ctx = _unwrap_uno(ctx) if ctx is not None else None
        self._work_queue: queue.Queue[Any] = queue.Queue()
        self._async_callback_service = None
        self._callback_instance = None
        self._init_lock = threading.Lock()
        self._claim_lock = threading.Lock()
        self._initialized = False
        self._logged_missing_ctx = False
        self._logged_async_callback_failure = False
        self._pending_posts: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any], SendCancellation | None]] = []
        self._pending_lock = threading.Condition()
        self._order_lock = threading.RLock()

    def set_context(self, ctx: Any) -> None:
        """Update or set the UNO component context (e.g. at bootstrap)."""
        from plugin.framework.thread_guard import _unwrap_uno

        # Store the raw context: Layer A wraps get_ctx() results, and comparing
        # proxy vs target would reset initialization on every panel wire.
        raw = _unwrap_uno(ctx) if ctx is not None else None
        with self._init_lock:
            if self._ctx is not raw:
                self._ctx = raw
                # Reset initialization so AsyncCallback is re-created with the updated context if needed.
                # One warning per context object; the same object keeps the
                # latch. A failed marshal latches the two log flags. Clearing
                # the service without the flags leaves a later context's
                # missing-ctx or toolkit failure silent.
                self._initialized = False
                self._async_callback_service = None
                self._callback_instance = None
                self._logged_missing_ctx = False
                self._logged_async_callback_failure = False
        # Posts queued before AsyncCallback existed used to sit until the next
        # marshal created it. When any are waiting, create the callback now and
        # flush them. _get_async_callback takes _init_lock; call it only after
        # that lock is released, or bootstrap deadlocks.
        with self._pending_lock:
            has_pending = bool(self._pending_posts)
        if has_pending:
            self._get_async_callback()
        self._flush_pending_posts()

    def pending_work_count(self) -> int:
        """How many marshal items are queued and not yet taken by ``process_queue``.

        Callers outside this class use this instead of ``_work_queue``. The
        count matches ``Queue.qsize`` (approximate if a producer is mid-put).
        """
        return self._work_queue.qsize()

    def callable_is_scheduled(self, fn: object) -> bool:
        """True when *fn* itself is queued or waiting for AsyncCallback.

        Queue depth is the wrong signal: this executor is process-wide, so an
        unrelated item (or a unit test that left one behind) is not this
        callback. A full pending list waits, then ``post`` raises; the
        callable is not stored, so the caller can retry.
        """
        with self._claim_lock:
            # ``Queue.queue`` is the deque. The mutex is the one ``put`` /
            # ``get`` hold; ``_claim_lock`` is already held around those calls.
            with self._work_queue.mutex:
                queued = any(getattr(item, "fn", None) is fn and not getattr(item, "cancelled", False) for item in self._work_queue.queue)
        if queued:
            return True
        with self._pending_lock:
            return any(row[0] is fn for row in self._pending_posts)

    def _await_pending_slot_locked(self) -> bool:
        """True when the pending list can take one post. Caller holds the lock.

        A full list used to return from ``post`` after a warning. Waiters
        sleep on ``_pending_lock`` until a flush or cancel frees a slot.
        """
        deadline = time.monotonic() + _PENDING_POST_WAIT_SEC
        while len(self._pending_posts) >= _PENDING_POST_CAP:
            # Eval harness sets initialized with no service. Flush never runs.
            if self._initialized and self._async_callback_service is None:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._pending_lock.wait(timeout=remaining)
        return True

    def _flush_pending_posts(self) -> None:
        """Enqueue posts that arrived before AsyncCallback existed.

        ``_order_lock`` covers the swap and the puts. A direct ``_enqueue_work``
        from another thread used to land between them, ahead of older posts.
        The poke happens after the lock is released.

        The failed item and the tail go back in front of anything appended
        after the notify, which keeps FIFO order. Swapping the list to
        ``[]`` before the loop drops posts if ``_enqueue_work`` raises
        partway: they are neither queued nor pending. The exception still
        propagates so the caller does not treat the flush as done. Items
        that did enqueue are poked after the lock drops.
        """
        enqueued = 0
        flush_error: Exception | None = None
        with self._order_lock:
            if not self._initialized or self._async_callback_service is None:
                return
            with self._pending_lock:
                pending = self._pending_posts
                self._pending_posts = []
                # Slots just opened. Waiters in post() are on this condition.
                self._pending_lock.notify_all()
            try:
                for fn, args, kwargs, scope in pending:
                    self._enqueue_work(fn, args, kwargs, blocking=False, bound_scope=scope, poke=False)
                    enqueued += 1
            except Exception as exc:
                with self._pending_lock:
                    self._pending_posts = list(pending[enqueued:]) + self._pending_posts
                log.exception("QueueExecutor: flush failed after %d pending post(s); %d put back", enqueued, len(pending) - enqueued)
                flush_error = exc
        if enqueued:
            self._poke_main_thread()
        if flush_error is not None:
            raise flush_error

    # Unwrap Layer A proxies before any UNO getattr. Creating AsyncCallback
    # from a worker is the marshal bootstrap: if the guard fires here it calls
    # execute_on_main_thread while ``_init_lock`` is held, and the UI thread
    # deadlocks in set_context(). Two defenses prevent this:
    #   1. _unwrap_uno() strips the guard proxy so UNO calls below don't
    #      trigger assert_main_thread at all.
    #   2. _notify_thread_violation (thread_guard.py) bails early when
    #      ``not default_executor._initialized``, which is exactly the state
    #      while this lock is held.
    # If you refactor here, preserve both or the bootstrap deadlocks.
    def _get_async_callback(self) -> Any:
        """Lazily create the AsyncCallback UNO service and XCallback instance."""
        if self._initialized:
            return self._async_callback_service
        with self._init_lock:
            if self._initialized:
                return self._async_callback_service
            # Headless eval: one designated ``_lo_thread`` owns the URP bridge.
            # DISPLAY=:9 (or any toolkit) can still create AsyncCallback; a poke
            # from a LoLane worker would run UNO on the VCL/bridge callback path
            # while ``_lo_thread`` is mid-call → "Binary URP bridge already disposed".
            import os

            if os.environ.get("WRITERAGENT_EVAL_HARNESS") == "1":
                log.info("QueueExecutor: EVAL_HARNESS set — AsyncCallback disabled (UNO stays on designated thread)")
                self._async_callback_service = None
                self._initialized = True
                return None
            try:
                # Use the extension's self.ctx (set_context at bootstrap).
                # uno.getComponentContext() can return a different context and
                # cause AsyncCallback to be created in the wrong context — silently
                # making execute() pokes no-ops. Missing ctx is logged, not probed.
                # Unwrap before any UNO getattr (see the note above this method).
                from plugin.framework.thread_guard import _unwrap_uno

                ctx = _unwrap_uno(self._ctx)
                # get_ctx() is @main_thread_only. This runs on the first worker
                # post/execute; calling it here raises, the except swallows it,
                # and we would fall through to a wrong context. Bootstrap
                # set_context() is the path that works. Missing ctx logs below
                # and leaves AsyncCallback unset (tests without VCL still run).
                if ctx is None:
                    # Do not latch _initialized. A post before set_context used
                    # to fail the assert below and then refuse every later marshal
                    # until the context object identity changed.
                    if not self._logged_missing_ctx:
                        log.warning("QueueExecutor has no component context; call set_context() from bootstrap on the main thread")
                        self._logged_missing_ctx = True
                    return None

                ctx_any = cast("Any", ctx)
                from plugin.framework.uno_context import get_service_manager

                smgr = _unwrap_uno(get_service_manager(ctx_any))
                assert smgr is not None, "ServiceManager unavailable on UNO context"
                self._async_callback_service = cast("Any", smgr).createInstanceWithContext("com.sun.star.awt.AsyncCallback", ctx_any)
                if self._async_callback_service is None:
                    raise RuntimeError("createInstance com.sun.star.awt.AsyncCallback returned None")
                self._callback_instance = self._make_callback_instance()
                self._initialized = True
                log.info("QueueExecutor initialized (AsyncCallback ready)")
            except Exception as exc:
                # A one-shot toolkit failure used to set _initialized, after which
                # execute() raised and post() dropped work until set_context saw
                # a different context object. Leave the flag clear and retry.
                self._async_callback_service = None
                self._callback_instance = None
                if not self._logged_async_callback_failure:
                    log.warning("AsyncCallback unavailable (%s); next marshal will retry", exc)
                    self._logged_async_callback_failure = True
            return self._async_callback_service

    def _make_callback_instance(self) -> Any:
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

            def notify(self, aData: Any) -> None:
                executor.process_queue()

        return _MainThreadCallback()

    def _abandon_unstarted(self, item: _WorkItem) -> None:
        """Mark an item that has not entered ``fn`` and wake a blocking waiter."""
        item.cancelled = True
        if item.blocking and item.event and not item.event.is_set():
            item.exception = SendCancelled()
            item.event.set()

    def _put_work_items(self, items: list[_WorkItem]) -> None:
        """Put *items* under ``_claim_lock`` without poking."""
        with self._claim_lock:
            for item in items:
                self._work_queue.put(item)

    def process_queue(self) -> None:
        """Process one item from queue (called from main thread via AsyncCallback)."""
        # Dequeue and the cancel check share ``_claim_lock``. ``scope.cancel()``
        # sets the flag before Stop's drain; checking ``is_cancelled()`` here
        # covers an item the drain never marked. ``get_nowait`` first lets
        # Stop's drain see only items still queued, and the one already
        # removed still runs. The callable stays outside the lock
        # (``threading.Lock`` is not reentrant, and a test poke can re-enter).
        with self._claim_lock:
            try:
                item = self._work_queue.get_nowait()
            except queue.Empty:
                return
            scope = item.scope
            if item.cancelled or (scope is not None and scope.is_cancelled()):
                self._abandon_unstarted(item)
                skipped = True
            else:
                item._claimed = True  # caller's timeout can no longer cancel this execution
                skipped = False

        if not skipped:
            # ``scope.cancel()`` sets an Event and does not take ``_claim_lock``.
            # The claim above can miss a cancel that lands after it. Recheck
            # here so that cancel is dropped before ``fn()``. This does not
            # close the window: a cancel after this lock is released and
            # before ``fn()`` still runs the item. ``cancel_pending_work``
            # cannot see it (already off the queue). Stop tolerates that.
            # A function that has already started is waited out by the caller.
            with self._claim_lock:
                if item.cancelled or (scope is not None and scope.is_cancelled()):
                    item._claimed = False
                    self._abandon_unstarted(item)
                    skipped = True

        if skipped:
            log.debug("QueueExecutor: skipping cancelled item %s (%s)", item.id, getattr(item.fn, "__name__", "<fn>"))
            # A timed-out head used to return here and leave the next item
            # queued until some later poke. Wake the main thread now.
            if not self._work_queue.empty():
                self._poke_main_thread()
            return

        # The item is claimed: anything that raises from here must reach the
        # ``finally`` that sets the event. ``_fn_label`` and the start log used
        # to run before ``try``, so a raise there left the waiter parked.
        fn_label = "<fn>"
        try:
            fn_label = _fn_label(item.fn)
            _log_marshal(logging.DEBUG, "process_queue start fn=%s %s", fn_label, executor=self)
            item.result = item.fn(*item.args, **item.kwargs)
        except BaseException as exc:
            # Store KeyboardInterrupt/SystemExit too so the waiter re-raises
            # instead of seeing result=None while the exception hits VCL.
            # Non-blocking posts have no waiter, so the exception used to vanish.
            item.exception = exc
            if not item.blocking:
                log.exception("QueueExecutor: non-blocking main-thread work failed (%s)", fn_label)
        finally:
            if item.blocking and item.event:
                item.event.set()
            _log_marshal(logging.DEBUG, "process_queue done fn=%s %s", fn_label, executor=self)

        # Re-poke if more items waiting
        if not self._work_queue.empty():
            self._poke_main_thread()

    def _poke_main_thread(self) -> None:
        """Ask the VCL event loop to call our notify() callback."""
        if _test_poke_handler is not None:
            _test_poke_handler(self)
            return
        if self._async_callback_service is None or self._callback_instance is None:
            _log_marshal(logging.DEBUG, "poke skipped (no AsyncCallback) %s", executor=self)
            return
        try:
            # PyUNO rejects uno.Any for addCallback userData on Linux; None is accepted on supported LO builds.
            self._async_callback_service.addCallback(self._callback_instance, None)
        except Exception as e:
            _log_marshal(logging.WARNING, "_poke_main_thread addCallback failed: %s %s", e, executor=self)

    def cancel_pending_work(self, scope: SendCancellation | None = None) -> None:
        """Mark queued main-thread work as cancelled and wake blocking waiters.

        Drain, mark, and put survivors back under ``_order_lock`` then
        ``_claim_lock`` — the same order as ``_enqueue_work``. Releasing
        ``_claim_lock`` and re-queuing survivors afterwards lets a put in
        that gap land ahead of older work from another send. A put that
        only took ``_claim_lock`` after the drain had already seen an empty
        queue used to stay runnable too. The poke runs after both locks
        drop: a test handler re-enters ``process_queue``, and holding a
        lock across that poke deadlocks. ``_put_work_items`` is not used
        here because it acquires ``_claim_lock`` again (``Lock`` is not
        re-entrant).

        A *scope* cancels only items enqueued under that send. Other items go
        back in order. Stop used to wipe MCP, grammar, and peer marshals that
        share ``default_executor``. No scope still drains the whole queue.
        """
        kept_any = False
        with self._order_lock:
            with self._claim_lock:
                pending: list[_WorkItem] = []
                while True:
                    try:
                        pending.append(self._work_queue.get_nowait())
                    except queue.Empty:
                        break
                keep: list[_WorkItem] = []
                for item in pending:
                    if scope is not None and item.scope is not scope:
                        keep.append(item)
                        continue
                    item.cancelled = True
                    if item.blocking and item.event and not item.event.is_set():
                        item.exception = SendCancelled()
                        item.event.set()
                for item in keep:
                    self._work_queue.put(item)
                kept_any = bool(keep)
        if kept_any:
            self._poke_main_thread()
        # Pending posts are not on the work queue yet. A later flush used to
        # enqueue them under whatever send was current then, so Stop did not
        # drop the posts from the cancelled scope.
        with self._pending_lock:
            if scope is None:
                self._pending_posts.clear()
            else:
                self._pending_posts = [row for row in self._pending_posts if row[3] is not scope]
            self._pending_lock.notify_all()

    def _enqueue_work(self, fn: Any, args: Any, kwargs: Any, blocking: bool = True, *, bound_scope: SendCancellation | None | _ScopeUnset = _SCOPE_UNSET, poke: bool = True) -> _WorkItem:
        """Add work item to queue. *poke* False lets a batch caller poke once."""
        scope = _resolve_bound_scope(bound_scope)
        if scope is not None:
            scope.bind_executor(self)
        item_id = str(uuid.uuid4())
        item = _WorkItem(item_id, fn, args, kwargs, blocking, scope)
        # ``_order_lock`` (re-entrant: a flush holds it) orders this put behind
        # a flush in progress. ``_claim_lock`` is the same lock as
        # ``cancel_pending_work``'s drain. Neither is held across the poke:
        # ``process_queue`` may re-enter through the test poke handler, and
        # AsyncCallback is a UNO call.
        with self._order_lock:
            self._put_work_items([item])
        if poke:
            self._poke_main_thread()
        return item

    def _wait_for_result(self, item: Any, timeout: float) -> Any:
        """Wait for and return result from main thread."""
        if not item.event.wait(timeout):
            # Atomically cancel only if process_queue hasn't already claimed
            # this item for execution. Without _claim_lock there was a window
            # where the main thread could start executing fn() after this thread
            # gave up, causing UNO calls to run against an abandoned caller.
            # wait() can also return false in the same window the result is
            # stored and the event is set. Raising TimeoutError then drops a
            # finished result.
            #
            # TimeoutError only when the item had not started and is now
            # cancelled. An in-flight call is waited out with no second
            # timeout so the caller sees the real result or exception. A
            # claimed item that still raises TimeoutError is already inside
            # fn() on the UI thread; the caller treats that as "did not
            # happen", and a retry applies the document change twice.
            keep_waiting = False
            finished = False
            with self._claim_lock:
                finished = item.event is not None and item.event.is_set()
                if not finished and not item._claimed:
                    item.cancelled = True
                elif not finished:
                    keep_waiting = True
            if keep_waiting and item.event is not None:
                # Claimed marshal has no second timeout (retry would double-apply).
                # The main-thread call may still be mutating the document, so the
                # caller waits it out instead of abandoning it with an unknown
                # result. The wait is sliced only to log a hung fn (once past 30s,
                # then every 60s) and to bail out while the interpreter shuts down,
                # when the main thread will not run the item to completion.
                waited = 0.0
                next_log = 30.0
                while not item.event.wait(_UNTIMED_WAIT_SLICE_SEC):
                    waited += _UNTIMED_WAIT_SLICE_SEC
                    if sys.is_finalizing():
                        raise RuntimeError("outcome unknown: interpreter shutting down while waiting for main-thread fn=%s" % _fn_label(item.fn))
                    if waited >= next_log:
                        next_log += 60.0
                        log.warning(
                            "QueueExecutor: waiting unbound for in-flight fn=%s %s (%.0fs)",
                            _fn_label(item.fn),
                            _marshal_thread_tag(self),
                            waited,
                        )
            elif not finished:
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

    def execute(self, fn: Callable[..., Any], *args: Any, timeout: float = 30.0, bound_scope: SendCancellation | None | _ScopeUnset = _SCOPE_UNSET, **kwargs: Any) -> Any:
        """Execute function on main thread (blocking).

        If already on the main thread, calls directly (avoids deadlock).
        Otherwise blocks the calling thread up to *timeout* seconds.
        Raises TimeoutError if the main thread doesn't process the item in time.
        Re-raises any exception thrown by *fn*.
        """
        from plugin.framework.thread_guard import get_background_task_name, in_sync_host_dispatch

        fn_label = _fn_label(fn)
        bg_task = get_background_task_name()

        if self._may_run_marshal_inline():
            _log_marshal(logging.DEBUG, "marshal route=inline_logical_main fn=%s %s", fn_label, executor=self)
            return fn(*args, **kwargs)

        if in_sync_host_dispatch():
            msg = "marshal refused: execute_on_main_thread called from synchronous host dispatch context (deadlock hazard #402, fn=%s)" % fn_label
            _log_marshal(logging.ERROR, "%s %s", msg, executor=self)
            raise RuntimeError(msg)

        if bg_task:
            _log_marshal(logging.DEBUG, "marshal route=force_enqueue (background task %r) fn=%s %s", bg_task, fn_label, executor=self)
        elif self._is_logical_main_thread():
            _log_marshal(logging.DEBUG, "marshal route=force_enqueue (logical main but not Python MainThread) fn=%s %s", fn_label, executor=self)

        # WRITERAGENT_TESTING inlines execute on any non-background thread,
        # including Dummy-N. post() enqueues when AsyncCallback exists and the
        # caller is not the marshal thread. Why they differ: execute blocks, and
        # a Dummy-N UNO test waiting on VCL deadlocks (testing_runner sets the
        # flag so that hop stays inline). post is fire-and-forget, so it can enqueue.
        if self._should_run_inline() and not bg_task:
            _log_marshal(logging.DEBUG, "marshal route=inline_testing fn=%s %s", fn_label, executor=self)
            return fn(*args, **kwargs)

        svc = None if _force_marshal_mode else self._get_async_callback()

        if svc is None and not _force_marshal_mode:
            # Off the main thread with no AsyncCallback. Running fn here touches
            # UNO on the caller. The logical-main path already returned above.
            # Log the refusal and raise it directly. Raising RuntimeError
            # inside try/except only to log.exception and re-raise shows a
            # traceback for an exception this function just constructed.
            msg = "marshal refused: AsyncCallback unavailable from background thread (fn=%s)" % fn_label
            _log_marshal(logging.ERROR, "%s %s", msg, executor=self)
            raise RuntimeError(msg)

        self._flush_pending_posts()
        _log_marshal(logging.DEBUG, "marshal route=enqueue fn=%s %s", fn_label, executor=self)
        item = self._enqueue_work(fn, args, kwargs, blocking=True, bound_scope=bound_scope)
        return self._wait_for_result(item, timeout)

    def post(self, fn: Callable[..., Any], *args: Any, bound_scope: SendCancellation | None | _ScopeUnset = _SCOPE_UNSET, **kwargs: Any) -> None:
        """Post function to main thread without waiting for its result.

        Unlike execute, this does not return a result. Used for UI updates
        from background threads. Before AsyncCallback exists, a full pending
        list waits for a flush and then raises TimeoutError.
        """
        from plugin.framework.thread_guard import get_background_task_name

        fn_label = _fn_label(fn)
        bg_task = get_background_task_name()

        # Inline only when _should_run_inline() and the caller is not a
        # background task. execute() already required ``not bg_task`` for
        # that path; post() inlining whenever WRITERAGENT_TESTING=1 lets a
        # tagged worker touch UNO on itself during native tests. A tagged
        # worker falls through to enqueue, or to the pending list when
        # AsyncCallback is missing. Untagged threads still inline.
        # An untagged worker (e.g. a spill timer) also queues when
        # AsyncCallback exists; inlining there touches UNO off the main
        # thread. With no AsyncCallback it still inlines.
        # _should_run_inline() is already False under force-marshal, so
        # that mode needs no extra check here.
        if self._should_run_inline() and not bg_task and (self._may_run_marshal_inline() or self._get_async_callback() is None):
            _log_marshal(logging.DEBUG, "marshal route=post_inline_testing fn=%s %s", fn_label, executor=self)
            fn(*args, **kwargs)
            return

        svc = None if _force_marshal_mode else self._get_async_callback()
        if svc is None and not _force_marshal_mode:
            if self._may_run_marshal_inline():
                _log_marshal(logging.DEBUG, "marshal route=post_inline_logical_main fn=%s %s", fn_label, executor=self)
                fn(*args, **kwargs)
                return
            # Wait for a flush to free a slot (grammar gate / llm lane wait,
            # then TimeoutError). Returning after a warning drops icon and
            # status updates from before set_context. At _PENDING_POST_CAP
            # a logged-and-dropped callable looks like a normal return to
            # the caller. If AsyncCallback is already known missing, waiting
            # cannot help — fail immediately.
            with self._pending_lock:
                if not self._await_pending_slot_locked():
                    _log_marshal(logging.WARNING, "marshal route=post_timeout (AsyncCallback unavailable, pending full, background task %r) fn=%s %s", bg_task, fn_label, executor=self)
                    raise TimeoutError("marshal post timed out: AsyncCallback unavailable and pending list is full (fn=%s)" % fn_label)
                scope = _resolve_bound_scope(bound_scope)
                self._pending_posts.append((fn, args, kwargs, scope))
                _log_marshal(logging.DEBUG, "marshal route=post_pending fn=%s %s", fn_label, executor=self)
                return

        self._flush_pending_posts()
        _log_marshal(logging.DEBUG, "marshal route=post_enqueue fn=%s %s", fn_label, executor=self)
        self._enqueue_work(fn, args, kwargs, blocking=False, bound_scope=bound_scope)


# We can keep a global default instance to mimic the old main_thread behavior
# until everything is fully DI injected.
default_executor = QueueExecutor()


def async_callback_for_drain_rearm() -> Any | None:
    """Live ``AsyncCallback`` service for the stream drain, or None.

    None means the drain must keep its blocking loop (eval harness,
    force-marshal, test poke). Headless pytest forces the blocking path via
    :func:`~plugin.framework.async_stream.set_drain_scheduler_override` instead
    of sniffing ``unittest.mock`` here. ``WRITERAGENT_TESTING`` is not a reason
    to return None: that flag makes :meth:`QueueExecutor.post` run inline, and
    an inline re-arm would sit on this stack again. The drain calls
    ``addCallback`` directly so the mock-sidebar soffice (which sets the flag)
    still returns to the VCL loop between batches.
    """
    import os

    if os.environ.get("WRITERAGENT_EVAL_HARNESS") == "1":
        return None
    if _force_marshal_mode or _test_poke_handler is not None:
        return None
    service = default_executor._async_callback_service
    if service is not None:
        return service
    ctx = default_executor._ctx
    if ctx is None:
        return None
    return default_executor._get_async_callback()


def execute_on_main_thread(fn: Any, *args: Any, timeout: float = 30.0, bound_scope: SendCancellation | None | _ScopeUnset = _SCOPE_UNSET, **kwargs: Any) -> Any:
    """Legacy helper: Use default_executor.execute instead."""
    return default_executor.execute(fn, *args, timeout=timeout, bound_scope=bound_scope, **kwargs)


def post_to_main_thread(fn: Any, *args: Any, bound_scope: SendCancellation | None | _ScopeUnset = _SCOPE_UNSET, **kwargs: Any) -> None:
    """Legacy helper: Use default_executor.post instead."""
    return default_executor.post(fn, *args, bound_scope=bound_scope, **kwargs)


def pump_main_thread_work_queue(*, max_items: int = 1, executor: QueueExecutor | None = None) -> None:
    """Process queued UNO work on the LO main thread (call from idle/drain loops).

    Async tools enqueue via :func:`execute_on_main_thread` while the chat drain loop
    waits for them; this must run on the same thread as ``run_stream_drain_loop`` so
    workers are not blocked on AsyncCallback alone.
    """
    ex = executor or default_executor
    processed = 0
    for _unused in range(max_items):
        if ex.pending_work_count() == 0:
            break
        ex.process_queue()
        processed += 1
    if processed:
        _log_marshal(logging.DEBUG, "pump_main_thread_work_queue processed=%d %s", processed, executor=ex)


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
    """Idle tick for main-thread wait loops: drain QueueExecutor, then maybe pump VCL.

    Depth 0 or 1 still calls ``processEventsToIdle`` so chat Send stays responsive.
    Same-owner re-entry (depth > 1) skips VCL — the outer owner is already pumping —
    but still drains the marshal queue. Secondary UI progress must use
    :func:`plugin.framework.uno_context.process_events_to_idle`, which no-ops while
    a :func:`drain_owner_scope` is active.
    """
    pump_main_thread_work_queue(max_items=max_queue_items, executor=executor)
    # Depth > 1 notes the suppression and skips VCL. The marshal queue
    # above still runs so execute_on_main_thread is not stuck behind it.
    # Pumping VCL on every idle tick, including when a nested stream drain
    # (depth > 1) is already inside the outer pump, re-enters the owner.
    if get_drain_depth() > 1:
        note_suppressed_vcl_pump(get_drain_owner())
        return
    _pump_vcl_events(toolkit)
