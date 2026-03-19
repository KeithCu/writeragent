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
"""Dispatch UNO calls to the VCL main thread.

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

log = logging.getLogger("writeragent.framework.main_thread")


class _WorkItem:
    __slots__ = ("fn", "args", "kwargs", "event", "result", "exception")

    def __init__(self, fn, args, kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.event = threading.Event()
        self.result = None
        self.exception = None


_work_queue = queue.Queue()

_async_callback_service = None
_callback_instance = None
_init_lock = threading.Lock()
_initialized = False


def _get_async_callback():
    """Lazily create the AsyncCallback UNO service and XCallback instance."""
    global _async_callback_service, _callback_instance, _initialized
    if _initialized:
        return _async_callback_service
    with _init_lock:
        if _initialized:
            return _async_callback_service
        try:
            import uno
            ctx = uno.getComponentContext()
            smgr = ctx.ServiceManager
            _async_callback_service = smgr.createInstanceWithContext(
                "com.sun.star.awt.AsyncCallback", ctx)
            if _async_callback_service is None:
                raise RuntimeError("createInstance returned None")
            _callback_instance = _make_callback_instance()
            log.info("MainThreadExecutor initialized (AsyncCallback ready)")
        except Exception as exc:
            log.warning(
                "AsyncCallback unavailable (%s) — UNO calls will run "
                "in the HTTP thread (legacy behaviour)", exc)
            _async_callback_service = None
        _initialized = True
        return _async_callback_service


def _make_callback_instance():
    """Create a UNO XCallback that processes work items one at a time."""
    import unohelper
    from com.sun.star.awt import XCallback

    class _MainThreadCallback(unohelper.Base, XCallback):
        """XCallback that processes ONE item per call.

        Processing one item at a time lets the VCL event loop handle
        other events (redraws, user input) between tool executions.
        """

        def notify(self, _ignored):
            try:
                item = _work_queue.get_nowait()
            except queue.Empty:
                return
            try:
                item.result = item.fn(*item.args, **item.kwargs)
            except Exception as exc:
                item.exception = exc
            finally:
                item.event.set()
            # Re-poke if more items waiting
            if not _work_queue.empty():
                _poke_vcl()

    return _MainThreadCallback()


def _poke_vcl():
    """Ask the VCL event loop to call our notify() callback."""
    if _async_callback_service is None or _callback_instance is None:
        return
    try:
        import uno
        _async_callback_service.addCallback(
            _callback_instance, uno.Any("void", None))
    except Exception as e:
        log.debug("_poke_vcl with Any failed, retrying without: %s", e)
        try:
            _async_callback_service.addCallback(_callback_instance, None)
        except Exception as e2:
            log.warning("_poke_vcl failed: %s", e2)


def execute_on_main_thread(fn, *args, timeout=30.0, **kwargs):
    """Execute fn(*args, **kwargs) on the LibreOffice main (VCL) thread.

    If already on the main thread, calls directly (avoids deadlock).
    Otherwise blocks the calling thread up to *timeout* seconds.
    Raises TimeoutError if the main thread doesn't process the item in time.
    Re-raises any exception thrown by *fn*.
    """
    # Already on main thread — call directly to avoid deadlock
    if threading.current_thread() is threading.main_thread():
        return fn(*args, **kwargs)

    svc = _get_async_callback()

    if svc is None:
        # Fallback: call directly (not thread-safe).
        return fn(*args, **kwargs)

    item = _WorkItem(fn, args, kwargs)
    _work_queue.put(item)
    _poke_vcl()

    if not item.event.wait(timeout):
        raise TimeoutError(
            "Main-thread execution of %s timed out after %ss"
            % (getattr(fn, "__name__", str(fn)), timeout))

    if item.exception is not None:
        raise item.exception

    return item.result


def post_to_main_thread(fn):
    """Fire-and-forget: post fn() to the VCL main thread.

    Unlike execute_on_main_thread, does not block or return a result.
    Used for UI updates from background threads.
    """
    svc = _get_async_callback()
    if svc is None:
        fn()
        return

    item = _WorkItem(fn, (), {})
    _work_queue.put(item)
    _poke_vcl()
