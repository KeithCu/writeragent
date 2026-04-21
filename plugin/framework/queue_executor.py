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

import logging
import queue
import threading
import uuid
from typing import Any, Callable, cast

log = logging.getLogger("writeragent.framework.queue_executor")

class _WorkItem:
    __slots__ = ("id", "fn", "args", "kwargs", "blocking", "event",
                 "result", "exception", "cancelled")

    def __init__(self, item_id, fn, args, kwargs, blocking=True):
        self.id = item_id
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.blocking = blocking
        self.event = threading.Event() if blocking else None
        self.result = None
        self.exception = None
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
                self._async_callback_service = cast("Any", smgr).createInstanceWithContext(
                    "com.sun.star.awt.AsyncCallback", ctx_any)
                if self._async_callback_service is None:
                    raise RuntimeError("createInstance returned None")
                self._callback_instance = self._make_callback_instance()
                log.info("QueueExecutor initialized (AsyncCallback ready)")
            except Exception as exc:
                log.warning(
                    "AsyncCallback unavailable (%s) — UNO calls will run "
                    "in the HTTP thread (legacy behaviour)", exc)
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

        if item.cancelled:
            log.debug("QueueExecutor: skipping cancelled item %s (%s)",
                      item.id, getattr(item.fn, "__name__", "<fn>"))
        else:
            try:
                item.result = item.fn(*item.args, **item.kwargs)
            except Exception as exc:
                item.exception = exc
            finally:
                if item.blocking and item.event:
                    item.event.set()

        # Re-poke if more items waiting
        if not self._work_queue.empty():
            self._poke_main_thread()

    def _poke_main_thread(self):
        """Ask the VCL event loop to call our notify() callback."""
        if self._async_callback_service is None or self._callback_instance is None:
            return
        try:
            import uno
            self._async_callback_service.addCallback(
                self._callback_instance, uno.Any("void", None))  # type: ignore
        except Exception as e:
            log.debug("_poke_main_thread with Any failed, retrying without: %s", e)
            try:
                self._async_callback_service.addCallback(self._callback_instance, None)
            except Exception as e2:
                log.warning("_poke_main_thread failed: %s", e2)

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
            raise TimeoutError(
                "Main-thread execution of %s timed out after %ss"
                % (getattr(item.fn, "__name__", str(item.fn)), timeout))

        if item.exception is not None:
            raise item.exception

        return item.result

    def execute(self, fn: Callable, *args, timeout: float = 30.0, **kwargs) -> Any:
        """Execute function on main thread (blocking).

        If already on the main thread, calls directly (avoids deadlock).
        Otherwise blocks the calling thread up to *timeout* seconds.
        Raises TimeoutError if the main thread doesn't process the item in time.
        Re-raises any exception thrown by *fn*.
        """
        # Already on main thread — call directly to avoid deadlock
        if threading.current_thread() is threading.main_thread():
            return fn(*args, **kwargs)

        svc = self._get_async_callback()

        if svc is None:
            # Fallback: call directly (not thread-safe).
            return fn(*args, **kwargs)

        item = self._enqueue_work(fn, args, kwargs, blocking=True)
        return self._wait_for_result(item, timeout)

    def post(self, fn: Callable, *args, **kwargs) -> None:
        """Post function to main thread (non-blocking).

        Unlike execute, does not block or return a result.
        Used for UI updates from background threads.
        """
        svc = self._get_async_callback()
        if svc is None:
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
