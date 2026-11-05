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

`execute()` off the main thread with no AsyncCallback raises `RuntimeError`
and does not call `fn` on the caller. `post()` does not run the callback on
the background thread. Before AsyncCallback exists it keeps a pending list.
A list shorter than the cap stores the callable. A full list waits for a
flush, then raises `TimeoutError` without storing that callable. When
initialization has finished and AsyncCallback is `None` (eval harness), a
full list raises `TimeoutError` immediately and does not store the callable;
waiting cannot create the callback. A flush that fails partway logs, puts
the failed item and the tail back on the list, and does not raise; the
caller's own `execute` / `post` still runs.

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

import enum
import logging
import os
import queue
import sys
import threading
import time
from typing import Any, Callable, ClassVar, cast

from plugin.framework.send_cancellation import (
    SendCancelled,
    SendCancellation,
    _SCOPE_UNSET,
    _ScopeUnset,
    get_current_send_cancellation,
    is_agent_active,
    is_cancelled,
)

log = logging.getLogger("writeragent.framework.queue_executor")


class Policy(enum.Enum):
    production = "production"
    eval_harness = "eval_harness"
    test_inline = "test_inline"
    test_marshal = "test_marshal"


# Posts queued before AsyncCallback exists. Past this, wait for a flush
# instead of dropping the callable. Same pressure signal as the grammar gate:
# wait, then TimeoutError.
_PENDING_POST_CAP = 32
_PENDING_POST_WAIT_SEC = 30.0
# Slice for the untimed wait on a claimed marshal (log + shutdown check only).
_UNTIMED_WAIT_SLICE_SEC = 5.0


def install_from_environ(
    *,
    designated: threading.Thread | None = None,
    poke: Callable[[MainThread], None] | None = None,
) -> None:
    """Set process-wide policy from environment variables (eval harness or testing)."""
    if os.environ.get("WRITERAGENT_EVAL_HARNESS") == "1":
        policy = Policy.eval_harness
    elif os.environ.get("WRITERAGENT_TESTING") == "1":
        policy = Policy.test_inline
    else:
        policy = Policy.production
    MainThread._policy = policy
    if designated is not None:
        MainThread.designated = designated
    if poke is not None:
        MainThread.poke = poke


def _marshal_thread_tag(executor: "MainThread | None" = None) -> str:
    """One-line thread context for marshal/deadlock diagnosis (writeragent_debug.log)."""
    from plugin.framework.thread_guard import get_background_task_name

    cur = threading.current_thread()
    py_main = cur is threading.main_thread()
    ex = executor or main
    qdepth = ex.pending_work_count()
    return "thread=%r ident=%s py_main=%s logical_main=%s bg_task=%r agent_active=%s queue_depth=%s" % (
        cur.name,
        cur.ident,
        py_main,
        ex.is_current(),
        get_background_task_name(),
        is_agent_active(),
        qdepth,
    )


def _log_marshal(level: int, msg: str, *args: Any, executor: "MainThread | None" = None) -> None:
    """Log *msg* plus a marshal thread tag, only when *level* is enabled.

    ``log.debug(..., _marshal_thread_tag())`` still builds the tag when debug
    is off: the argument is evaluated before the logger checks the level.
    The tag takes ``is_agent_active``'s lock and reads queue depth, so the
    hot path (execute, post, process_queue) must not call it unless this
    record will be emitted. *msg* must end with a ``%s`` for the tag.
    """
    if not log.isEnabledFor(level):
        return
    tag = _marshal_thread_tag(executor)
    log.log(level, msg, *args, tag)


def _fn_label(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or repr(fn)


class _WorkItem:
    __slots__: ClassVar[tuple[str, ...]] = ("fn", "args", "kwargs", "event", "result", "exception", "cancelled", "_claimed", "scope")

    def __init__(self, fn: Any, args: Any, kwargs: Any, blocking: bool = True, scope: SendCancellation | None = None) -> None:
        self.fn: Any = fn
        self.args: Any = args
        self.kwargs: Any = kwargs
        self.event: threading.Event | None = threading.Event() if blocking else None
        self.result: Any = None
        self.exception: BaseException | None = None
        self.cancelled: bool = False
        self._claimed: bool = False
        self.scope: SendCancellation | None = scope


class MainThread:
    """Execute functions on main thread using queue system."""

    _policy: ClassVar[Policy] = Policy.production
    designated: ClassVar[threading.Thread | None] = None
    poke: ClassVar[Callable[[MainThread], None] | None] = None

    @property
    def policy(self) -> Policy:
        return MainThread._policy

    @classmethod
    def install(
        cls,
        policy: Policy,
        *,
        designated: threading.Thread | None = None,
        poke: Callable[[MainThread], None] | None = None,
    ) -> None:
        cls._policy = policy
        cls.designated = designated
        cls.poke = poke

    @staticmethod
    def on_python_main_thread() -> bool:
        """True for the Python main thread or the designated pump.

        A PyUNO ``_DummyThread`` is not this. Bootstrap must use this, not
        ``is_current()``: a status-listener upcall must not enter bootstrap.
        """
        current = threading.current_thread()
        designated = MainThread.designated
        if designated is not None:
            return current is designated
        return current is threading.main_thread()

    def is_current(self) -> bool:
        """True on the VCL thread, designated thread, or an untagged LibreOffice upcall holding SolarMutex."""
        if MainThread.on_python_main_thread():
            return True
        # A designated pump owns UNO alone; LO Dummy upcalls are not it (eval harness, test pump).
        if MainThread.designated is not None:
            return False
        from plugin.framework.thread_guard import UNTAGGED_DUMMY_IS_CURRENT, get_background_task_name

        if not UNTAGGED_DUMMY_IS_CURRENT:
            return False
        if get_background_task_name():
            return False
        return isinstance(threading.current_thread(), threading._DummyThread)

    def _is_marshal_thread(self) -> bool:
        from plugin.framework.thread_guard import get_background_task_name

        return (
            self.policy is not Policy.test_marshal
            and not get_background_task_name()
            and MainThread.on_python_main_thread()
        )

    def run(
        self,
        fn: Any,
        *args: Any,
        timeout: float = 30.0,
        bound_scope: SendCancellation | None | _ScopeUnset = _SCOPE_UNSET,
        **kwargs: Any,
    ) -> Any:
        """Call fn now if is_current() (including untagged Dummy-N), else marshal via call().

        is_current() is true for the Python main thread, designated test pump,
        and an untagged PyUNO Dummy thread. call() does not inline those Dummy
        threads, and it refuses inline work under test_marshal and background-task
        tags. This helper ensures untagged LO upcalls run inline rather than
        waiting 30 s on AsyncCallback.
        """
        if self.is_current():
            return fn(*args, **kwargs)
        call_kwargs: dict[str, Any] = dict(kwargs)
        call_kwargs["timeout"] = timeout
        if bound_scope is not _SCOPE_UNSET:
            call_kwargs["bound_scope"] = bound_scope
        return self.call(fn, *args, **call_kwargs)

    def post_or_run(
        self,
        fn: Any,
        *args: Any,
        bound_scope: SendCancellation | None | _ScopeUnset = _SCOPE_UNSET,
        **kwargs: Any,
    ) -> Any:
        """Run fn immediately if on main thread without a background task tag, else post.

        Matches the sidebar UI update idiom: paint inline on the VCL main thread,
        but enqueue via post when called from a background thread or a task-tagged
        worker (e.g. MCP, audio recording, grammar observer).
        """
        assert callable(fn), "fn must be callable"
        from plugin.framework.thread_guard import get_background_task_name

        if self.is_current() and not get_background_task_name():
            return fn(*args, **kwargs)
        self.post(fn, *args, bound_scope=bound_scope, **kwargs)
        return None

    def __init__(self, ctx: Any | None = None) -> None:
        from plugin.framework.thread_guard import unwrap_uno

        self._ctx: Any | None = unwrap_uno(ctx) if ctx is not None else None
        self._work_queue: queue.Queue[Any] = queue.Queue()
        self._async_callback_service: Any = None
        self._callback_instance: Any = None
        self._init_lock: threading.Lock = threading.Lock()
        self._claim_lock: threading.Lock = threading.Lock()
        self._initialized: bool = False
        self._logged_callback_problem: bool = False
        self._pending_posts: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any], SendCancellation | None]] = []
        self._pending_lock: threading.Condition = threading.Condition()
        self._order_lock: threading.RLock = threading.RLock()

    def set_context(self, ctx: Any) -> None:
        """Update or set the UNO component context (e.g. at bootstrap)."""
        from plugin.framework.thread_guard import unwrap_uno

        # Store the raw context: Layer A wraps get_ctx() results, and comparing
        # proxy vs target would reset initialization on every panel wire.
        raw = unwrap_uno(ctx) if ctx is not None else None
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
                self._logged_callback_problem = False
        # When posts are waiting, create AsyncCallback and flush them outside _init_lock.
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

    def is_marshal_ready(self) -> bool:
        """True when AsyncCallback initialization has completed successfully."""
        return bool(self._initialized and self._async_callback_service is not None)

    def _await_pending_slot_locked(self) -> bool:
        """True when the pending list can take one post. Caller holds the lock.

        Waiters sleep on ``_pending_lock`` until a flush or cancel frees a slot.
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

        ``_order_lock`` covers the swap and the puts to preserve order against concurrent enqueues.
        The poke happens after the lock is released.

        A partial failure puts the failed item and the tail back in front
        of posts appended after notify_all.
        """
        enqueued = 0
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
            except Exception:
                # Keep tail pending and continue caller work; later flush retries the tail.
                with self._pending_lock:
                    self._pending_posts = list(pending[enqueued:]) + self._pending_posts
                log.exception("MainThread: flush failed after %d pending post(s); %d put back", enqueued, len(pending) - enqueued)
        if enqueued:
            self._poke_main_thread()

    # Unwrap Layer A proxies before any UNO getattr. Creating AsyncCallback
    # from a worker is the marshal bootstrap: if the guard fires here it calls
    # main.call while ``_init_lock`` is held, and the UI thread
    # deadlocks in set_context(). Two defenses prevent this:
    #   1. unwrap_uno() strips the guard proxy so UNO calls below don't
    #      trigger assert_main_thread at all.
    #   2. _notify_thread_violation (thread_guard.py) bails early when
    #      ``not main.is_marshal_ready()``, which is exactly the state
    #      while this lock is held.
    # If you refactor here, preserve both or the bootstrap deadlocks.
    def _get_async_callback(self) -> Any:
        """Lazily create the AsyncCallback UNO service and XCallback instance."""
        if self._initialized:
            return self._async_callback_service
        with self._init_lock:
            if self._initialized:
                return self._async_callback_service
            if self.policy is Policy.eval_harness:
                # Headless eval: one designated _lo_thread owns the URP bridge.
                # A poke from a LoLane worker runs UNO on the VCL path while _lo_thread
                # is mid-call -> "Binary URP bridge already disposed". Keep UNO on designated thread.
                log.info("MainThread: EVAL_HARNESS set — AsyncCallback disabled (UNO stays on designated thread)")
                self._async_callback_service = None
                self._initialized = True
                return None
            try:
                # Use the extension's self.ctx (set_context at bootstrap).
                # uno.getComponentContext() can return a different context and
                # cause AsyncCallback to be created in the wrong context — silently
                # making execute() pokes no-ops. Missing ctx is logged, not probed.
                # self._ctx is already raw from __init__ and set_context; the service manager is unwrapped below.
                from plugin.framework.thread_guard import unwrap_uno

                ctx = self._ctx
                # get_ctx() is @main_thread_only. This runs on the first worker
                # post/execute; calling it here raises, the except swallows it,
                # and we would fall through to a wrong context. Bootstrap
                # set_context() is the path that works. Missing ctx logs below
                # and leaves AsyncCallback unset (tests without VCL still run).
                if ctx is None:
                    # Do not latch _initialized. A post before set_context used
                    # to fail the assert below and then refuse every later marshal
                    # until the context object identity changed.
                    if not self._logged_callback_problem:
                        log.warning("MainThread has no component context; call set_context() from bootstrap on the main thread")
                        self._logged_callback_problem = True
                    return None

                from plugin.framework.uno_context import get_service_manager

                smgr = unwrap_uno(get_service_manager(ctx))
                self._async_callback_service = cast("Any", smgr).createInstanceWithContext("com.sun.star.awt.AsyncCallback", ctx)
                if self._async_callback_service is None:
                    raise RuntimeError("createInstance com.sun.star.awt.AsyncCallback returned None")
                self._callback_instance = self._make_callback_instance()
                self._initialized = True
                log.info("MainThread initialized (AsyncCallback ready)")
            except Exception as exc:
                # Leave initialized flag clear on toolkit failure so subsequent calls retry.
                self._async_callback_service = None
                self._callback_instance = None
                if not self._logged_callback_problem:
                    log.warning("AsyncCallback unavailable (%s); next marshal will retry", exc)
                    self._logged_callback_problem = True
            return self._async_callback_service

    def has_async_callback(self) -> bool:
        """True when AsyncCallback exists or can be created.

        Uses the lazy creator so install sees the same readiness as
        ``_get_async_callback()`` without peeking ``_async_callback_service`` alone
        (that field stays None until creation, which would make install always skip).
        """
        return self._get_async_callback() is not None

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
        if item.event is not None and not item.event.is_set():
            item.exception = SendCancelled()
            item.event.set()

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
            if item.cancelled or is_cancelled(scope):
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
                if item.cancelled or is_cancelled(scope):
                    item._claimed = False
                    self._abandon_unstarted(item)
                    skipped = True

        if skipped:
            log.debug("MainThread: skipping cancelled item %s (%s)", id(item), _fn_label(item.fn))
        else:
            # The item is claimed: anything that raises from here must reach the
            # ``finally`` that sets the event.
            fn_label = "<fn>"
            try:
                fn_label = _fn_label(item.fn)
                _log_marshal(logging.DEBUG, "process_queue start fn=%s %s", fn_label, executor=self)
                item.result = item.fn(*item.args, **item.kwargs)
            except BaseException as exc:
                # Store KeyboardInterrupt/SystemExit too so the waiter re-raises
                # instead of seeing result=None while the exception hits VCL.
                item.exception = exc
                if item.event is None:
                    log.exception("MainThread: non-blocking main-thread work failed (%s)", fn_label)
            finally:
                if item.event is not None:
                    item.event.set()
                _log_marshal(logging.DEBUG, "process_queue done fn=%s %s", fn_label, executor=self)

        # Re-poke if more items waiting
        if not self._work_queue.empty():
            self._poke_main_thread()

    def _poke_main_thread(self) -> None:
        """Ask the VCL event loop to call our notify() callback."""
        if MainThread.poke is not None:
            MainThread.poke(self)
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
        that gap land ahead of older work from another send. Taking both locks
        prevents interleaving. The poke runs after both locks drop.

        A *scope* cancels only items enqueued under that send; other items go
        back in order. No scope drains the whole queue.
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
                    self._abandon_unstarted(item)
                for item in keep:
                    self._work_queue.put(item)
                kept_any = bool(keep)
        if kept_any:
            self._poke_main_thread()
        # Cancel matching pending posts before they reach the work queue.
        with self._pending_lock:
            if scope is None:
                self._pending_posts.clear()
            else:
                self._pending_posts = [row for row in self._pending_posts if row[3] is not scope]
            self._pending_lock.notify_all()

    def _enqueue_work(self, fn: Any, args: Any, kwargs: Any, blocking: bool = True, *, bound_scope: SendCancellation | None | _ScopeUnset = _SCOPE_UNSET, poke: bool = True) -> _WorkItem:
        """Add work item to queue. *poke* False lets a batch caller poke once."""
        # ``is`` does not narrow _ScopeUnset. None is an explicit scope.
        scope = get_current_send_cancellation() if bound_scope is _SCOPE_UNSET else cast("SendCancellation | None", bound_scope)
        if scope is not None:
            scope.bind_executor(self)
        item = _WorkItem(fn, args, kwargs, blocking, scope)
        # ``_order_lock`` (re-entrant: a flush holds it) orders this put behind
        # a flush in progress. ``_claim_lock`` is the same lock as
        # ``cancel_pending_work``'s drain. Neither is held across the poke:
        # ``process_queue`` may re-enter through the test poke handler, and
        # AsyncCallback is a UNO call.
        with self._order_lock:
            with self._claim_lock:
                self._work_queue.put(item)
        if poke:
            self._poke_main_thread()
        return item

    def _wait_for_result(self, item: _WorkItem, timeout: float) -> Any:
        """Wait for and return result from main thread."""
        assert item.event is not None
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
            with self._claim_lock:
                finished = item.event.is_set()
                if not finished and not item._claimed:
                    item.cancelled = True
            if not finished and item._claimed:
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
                            "MainThread: waiting unbound for in-flight fn=%s %s (%.0fs)",
                            _fn_label(item.fn),
                            _marshal_thread_tag(self),
                            waited,
                        )
            elif not finished:
                raise TimeoutError("Main-thread execution of %s timed out after %ss" % (_fn_label(item.fn), timeout))

        if item.exception is not None:
            raise item.exception

        return item.result

    def call(
        self,
        fn: Callable[..., Any],
        /,
        *args: Any,
        timeout: float = 30.0,
        bound_scope: SendCancellation | None | _ScopeUnset = _SCOPE_UNSET,
        **kwargs: Any,
    ) -> Any:
        """Execute function on main thread (blocking).

        If already on the marshal thread, calls directly (avoids deadlock).
        Otherwise blocks the calling thread up to *timeout* seconds.
        Raises TimeoutError if the main thread doesn't process the item in time.
        Re-raises any exception thrown by *fn*.
        """
        from plugin.framework.thread_guard import get_background_task_name, in_sync_host_dispatch

        fn_label = _fn_label(fn)
        bg_task = get_background_task_name()

        if self._is_marshal_thread():
            return fn(*args, **kwargs)

        if in_sync_host_dispatch():
            msg = "marshal refused: main.call called from synchronous host dispatch context (deadlock hazard #402, fn=%s)" % fn_label
            _log_marshal(logging.ERROR, "%s %s", msg, executor=self)
            raise RuntimeError(msg)

        # test_inline inlines call on Dummy-N because call() blocks, and a Dummy-N
        # UNO test waiting on AsyncCallback deadlocks. post is fire-and-forget so it can enqueue.
        if self.policy is Policy.test_inline and not bg_task:
            return fn(*args, **kwargs)

        if self.policy is not Policy.test_marshal and self._get_async_callback() is None:
            msg = "marshal refused: AsyncCallback unavailable from background thread (fn=%s)" % fn_label
            _log_marshal(logging.ERROR, "%s %s", msg, executor=self)
            raise RuntimeError(msg)

        self._flush_pending_posts()
        item = self._enqueue_work(fn, args, kwargs, blocking=True, bound_scope=bound_scope)
        return self._wait_for_result(item, timeout)

    def post(
        self,
        fn: Callable[..., Any],
        /,
        *args: Any,
        bound_scope: SendCancellation | None | _ScopeUnset = _SCOPE_UNSET,
        force_queue: bool = False,
        **kwargs: Any,
    ) -> None:
        """Post function to main thread without waiting for its result.

        Unlike call, this does not return a result. Used for UI updates
        from background threads. Before AsyncCallback exists, a full pending
        list waits for a flush and then raises TimeoutError. When
        AsyncCallback is already known missing, a full list raises that
        TimeoutError immediately and does not store the callable.
        """
        from plugin.framework.thread_guard import get_background_task_name

        fn_label = _fn_label(fn)
        bg_task = get_background_task_name()

        # Spill timer and untagged workers enqueue when AsyncCallback exists; inlining touches UNO off-main.
        if not force_queue and self.policy is Policy.test_inline and not bg_task and (self._is_marshal_thread() or self._get_async_callback() is None):
            fn(*args, **kwargs)
            return

        if not force_queue and self.policy is not Policy.test_marshal and self._get_async_callback() is None:
            if self._is_marshal_thread():
                fn(*args, **kwargs)
                return
            # Wait for a flush to free a slot; a dropped icon or status update would look like a normal return.
            with self._pending_lock:
                if not self._await_pending_slot_locked():
                    raise TimeoutError("marshal post timed out: AsyncCallback unavailable and pending list is full (fn=%s)" % fn_label)
                scope = get_current_send_cancellation() if bound_scope is _SCOPE_UNSET else cast("SendCancellation | None", bound_scope)
                self._pending_posts.append((fn, args, kwargs, scope))
                return

        self._flush_pending_posts()
        self._enqueue_work(fn, args, kwargs, blocking=False, bound_scope=bound_scope)


main = MainThread()


def async_callback_for_drain_rearm() -> Any | None:
    """Live ``AsyncCallback`` service for the stream drain, or None.

    None means the drain must keep its blocking loop (eval harness,
    force-marshal, test poke). Headless pytest forces the blocking path via
    :func:`~plugin.framework.async_stream.set_drain_scheduler_override` instead
    of sniffing ``unittest.mock`` here.
    """
    if main.policy in (Policy.eval_harness, Policy.test_marshal):
        return None
    service = main._async_callback_service
    if service is not None:
        return service
    ctx = main._ctx
    if ctx is None:
        return None
    return main._get_async_callback()


def pump_main_thread_work_queue(*, max_items: int = 1, executor: MainThread | None = None) -> None:
    """Process queued UNO work on the LO main thread (call from idle/drain loops)."""
    ex = executor or main
    if not ex.is_current():
        return

    for _unused in range(max_items):
        ex.process_queue()


def pump_ui_idle(toolkit: Any, *, max_queue_items: int = 1, executor: MainThread | None = None) -> None:
    """Idle tick for main-thread wait loops: drain MainThread, then maybe pump VCL.

    Depth 0 or 1 still calls ``processEventsToIdle``; depth > 1 skips VCL and
    still drains the marshal queue. ``owner_may_pump_vcl`` is only for this
    function; secondary progress uses ``process_events_to_idle``, which no-ops
    while any owner is set. See :func:`plugin.framework.async_drain_guard.drain_owner_scope`.
    """
    from plugin.framework.async_drain_guard import (
        get_drain_owner,
        note_suppressed_vcl_pump,
        owner_may_pump_vcl,
    )
    from plugin.framework.vcl_pumping import pump_vcl_events

    ex = executor or main
    if not ex.is_current():
        return

    pump_main_thread_work_queue(max_items=max_queue_items, executor=ex)
    # owner_may_pump_vcl (depth > 1) notes the suppression and skips VCL.
    # The marshal queue above still runs so main.call is not stuck behind a suppressed VCL pump.
    if not owner_may_pump_vcl():
        note_suppressed_vcl_pump(get_drain_owner())
        return
    pump_vcl_events(toolkit)
