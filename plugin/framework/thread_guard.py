# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
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
"""Runtime guard for UNO main-thread affinity (Layer A).

On by default in non-release bundles (dev-deploy, ``make build``). Release OXT
bundles replace this module with a no-op stub. Opt out in dev:
``WRITERAGENT_UNO_THREAD_GUARD=0``.

When active:
- Calls to guarded UNO sources from a background thread raise immediately
  with a stack trace naming the offending call and (if tagged) the worker task.
- Returned PyUNO objects are wrapped in a transparent proxy that asserts on
  every subsequent access/call, so arbitrary object-graph walks are covered
  with zero per-site annotations.

When inactive, violations log a warning with full stack (no crash).

See docs/framework/uno-thread-safety.md (Layer A).

Concurrency: each background job is tagged with a name in
**thread-local** storage when ``run_in_background`` starts, so a
violation message can say ``run_search`` instead of a reused pool thread
name. ``_violation_ui_lock`` guards ``_violation_dialog_open`` so only
one process-wide violation dialog can be open at a time; further
violations are logged until the dialog returns. This module
**detects** wrong-thread UNO access; it does not move work to the UI
thread — use ``QueueExecutor`` / ``execute_on_main_thread`` for that.
Untagged ``_DummyThread`` is LO dispatch; a background-task tag overrides it.
"""

from __future__ import annotations

import functools
import os
import threading
import logging
from contextlib import contextmanager
from typing import Any, Generator, Iterator

log = logging.getLogger("writeragent.threadguard")

# Non-release bundles ship this full module with guard on by default.
# Release bundles replace this file with a stub (GUARD_ON = False).
# Opt out in dev: WRITERAGENT_UNO_THREAD_GUARD=0
GUARD_ON = os.environ.get("WRITERAGENT_UNO_THREAD_GUARD", "1") == "1"
THREAD_VIOLATION_MARK = "UNO thread violation"

# Thread-local storage for background task identity (set at birth in run_in_background).
_bg = threading.local()

# Thread-local storage for synchronous host/bridge dispatch context (Yellow context).
# When active on a non-main thread, execute_on_main_thread must refuse immediately
# to prevent lock inversion deadlocks against the waiting main thread (#402).
_sync_host = threading.local()


@contextmanager
def sync_host_dispatch() -> Generator[None, None, None]:
    """Mark the calling thread as executing within a synchronous host/bridge dispatch (Yellow context).

    While in this context off the main thread, blocking operations that wait on the
    LibreOffice main thread (such as execute_on_main_thread) will immediately raise
    RuntimeError rather than deadlocking for 30s.
    """
    prev = getattr(_sync_host, "active", False)
    _sync_host.active = True
    try:
        yield
    finally:
        _sync_host.active = prev


def in_sync_host_dispatch() -> bool:
    """Return True if the current thread is executing within a synchronous host dispatch."""
    return getattr(_sync_host, "active", False)


# Layer B pytest: when set, on_main_thread() treats this thread as the UNO main thread
# (typically the synthetic pump thread started by tests/framework/thread_safety.py).
_designated_main_thread: threading.Thread | None = None


def set_designated_main_thread(thread: threading.Thread | None) -> None:
    """Test hook: designate which thread may touch UNO (see docs/framework/uno-thread-safety.md Layer B)."""
    global _designated_main_thread
    _designated_main_thread = thread


def get_designated_main_thread() -> threading.Thread | None:
    return _designated_main_thread


def on_python_main_thread() -> bool:
    """True only for the Python main thread or the designated test pump.

    A PyUNO ``_DummyThread`` is not this. Bootstrap must use this, not
    ``on_main_thread()``: a status-listener upcall must not enter bootstrap.
    """
    current = threading.current_thread()
    if _designated_main_thread is not None:
        return current is _designated_main_thread
    return current is threading.main_thread()


def on_main_thread() -> bool:
    """True on the VCL thread, or on an untagged LibreOffice upcall.

    What was wrong: only ``threading.main_thread()`` passed, so every PyUNO
    upcall (a new ``threading._DummyThread`` per call, named ``Dummy-N``)
    was a violation. LibreOffice invokes listeners while holding the
    SolarMutex, so that access is legitimate. With ``GUARD_ON``, the raise
    aborted ``review_toolbar`` modify-listener registration.
    How it happened: ``assert_main_thread`` / ``@main_thread_only``
    (``get_runtime_uid``, ``uno_same``) and proxy ``__getattr__``
    (``UNO.getServiceManager``) all go through this predicate.
    Why this change: an untagged ``_DummyThread`` is LO dispatch. A Dummy
    thread tagged with ``set_background_task`` is our work and stays a
    violation. ``threading.Thread`` workers stay a violation. The
    designated-main hook still wins over both.
    """
    if on_python_main_thread():
        return True
    if _designated_main_thread is not None:
        return False
    if get_background_task_name():
        return False
    return isinstance(threading.current_thread(), threading._DummyThread)


# In-flight violation dialog flag. Only one process-wide violation dialog may be
# open at a time; further violations are logged until that dialog returns.
_violation_dialog_open: bool = False
_violation_ui_lock = threading.Lock()


def set_background_task(name: str | None) -> None:
    """Tag the current thread as a background worker task (for better diagnostics).

    Pass None to clear the task name when a pooled worker finishes so the next
    job is not blamed under a stale task id. It does not arm another dialog.
    """
    _bg.task_name = name


def get_background_task_name() -> str | None:
    return getattr(_bg, "task_name", None)


@contextmanager
def background_if_dummy_thread(name: str) -> Generator[None, None, None]:
    """Tag a PyUNO Dummy carrier so ``on_main_thread()`` stays false.

    A ``threading.Thread``, the Python main thread, and the designated
    pump thread are unchanged. Call this at LO entry points whose body
    must keep marshaling (proofreading PE2I, ``=PY()`` / ``=PROMPT()``).
    """
    current = threading.current_thread()
    if _designated_main_thread is not None and current is _designated_main_thread:
        yield
        return
    if not isinstance(current, threading._DummyThread):
        yield
        return
    prev = get_background_task_name()
    set_background_task(name)
    try:
        yield
    finally:
        set_background_task(prev)


def _notify_thread_violation(msg: str) -> None:
    """Log always; post one dialog except under WRITERAGENT_TESTING; the post is unscoped so Stop cannot drop it."""
    log.error(msg, stack_info=True)
    if os.environ.get("WRITERAGENT_TESTING") == "1":
        return
    # Capture full stack trace for display
    import traceback

    stack_trace = "".join(traceback.format_stack())
    full_msg = f"{msg}\n\nStack trace:\n{stack_trace}"
    # Marshal bootstrap (_get_async_callback) holds QueueExecutor._init_lock.
    # A blocking execute() from here deadlocks if the UI thread is waiting or if
    # the calling thread was dispatched synchronously from an external UNO call (#402).
    # Only post asynchronously when AsyncCallback is available; never block the worker.
    try:
        from plugin.framework.queue_executor import default_executor

        if not default_executor.is_marshal_ready():
            return
    except Exception:
        return

    # Bugfix: Per-ident dedup posted one modal per violating thread, and each
    # modal runs a nested main-thread loop. set_background_task(None) also
    # cleared the ident while that dialog was still up, so the next job on
    # the same pool thread posted a second modal. One flag, cleared only when
    # the dialog callback finishes or the post itself fails, allows a single dialog.
    global _violation_dialog_open
    with _violation_ui_lock:
        if _violation_dialog_open:
            return
        _violation_dialog_open = True

    def _show_popup() -> None:
        try:
            from plugin.framework.uno_context import get_ctx
            from plugin.chatbot.dialogs import msgbox_with_report
            from plugin.framework.i18n import _

            # nosemgrep: uno-off-main-thread
            msgbox_with_report(get_ctx(), _("UNO Thread Violation"), full_msg, box_type=3, reportable=True, report_title="UNO thread violation", report_extra=full_msg)
        except Exception:
            log.exception("Failed to show thread violation message box")
        finally:
            global _violation_dialog_open
            with _violation_ui_lock:
                _violation_dialog_open = False

    try:
        from plugin.framework.queue_executor import post_to_main_thread

        # Bugfix: run_in_background copies the send ContextVar, so an omitted
        # scope binds the active SendCancellation. cancel() -> cancel_pending_work
        # and process_queue's scope.is_cancelled() skip then drop _show_popup
        # after _violation_dialog_open was set, leaving the flag stuck. An
        # unscoped post (bound_scope=None) ensures the popup survives cancellation.
        post_to_main_thread(_show_popup, bound_scope=None)
    except Exception:
        log.exception("Failed to post thread violation message box on main thread")
        with _violation_ui_lock:
            _violation_dialog_open = False


def assert_main_thread(what: str) -> None:
    """Raise (if guard on) or log warning+stack (if guard off) when off the main thread."""
    if on_main_thread():
        return
    task = get_background_task_name() or threading.current_thread().name
    msg = "%s: %r touched UNO from background task %r; marshal via execute_on_main_thread()." % (THREAD_VIOLATION_MARK, what, task)
    if GUARD_ON:
        _notify_thread_violation(msg)
        raise RuntimeError(msg)
    log.warning(msg, stack_info=True)


def main_thread_only(fn: Any) -> Any:
    """Decorator: assert main thread on entry. Use on UNO source functions."""

    @functools.wraps(fn)
    def wrapper(*a: Any, **k: Any) -> Any:
        assert_main_thread(getattr(fn, "__qualname__", str(fn)))
        return fn(*a, **k)

    return wrapper


def background(fn: Any) -> Any:
    """Decorator: mark a function as background-only (blue for Layer C Semgrep).

    Warns if invoked on the main thread; documents intent for static analysis.
    See docs/framework/uno-thread-safety.md (Layer C).
    """

    @functools.wraps(fn)
    def wrapper(*a: Any, **k: Any) -> Any:
        if on_main_thread():
            log.warning("@background fn %r ran on the main thread", getattr(fn, "__qualname__", str(fn)))
        return fn(*a, **k)

    return wrapper


def _is_pyuno(obj: Any) -> bool:
    """Heuristic: is this a real PyUNO object we should guard?"""
    if obj is None:
        return False
    # Structs (PropertyValue, etc.) are pass-by-value; wrapping them breaks
    # TypeDetection / loadComponentFromURL which require real structs in tuples.
    if hasattr(obj, "__pyunostruct__"):
        return False
    t = type(obj)
    # Bugfix: do not treat PyUNO_callable as a guardable UNO object.
    # What was wrong: returning True for tp_name "PyUNO_callable"
    # (pyuno/source/module/pyuno_callable.cxx, module builtins, no
    # queryInterface) made __getattr__ wrap the method and __call__
    # wrap its return.
    # How it happened: pyuno.invoke keeps uno.Any only when the target
    # is a real PyUNO instance (pyuno.cxx PyUNO_invoke). A proxy takes
    # the else branch, which drops Any before the call, and
    # replaceByIndex / setPropertyValue of a PropertyValue or TabStop
    # sequence raises IllegalArgumentException. pyObject2Any extracts
    # the real XInterface only for a PyUNO instance
    # (pyuno_runtime.cxx). Any other object whose getTypes() returns
    # types — this proxy forwards getTypes — becomes an invocation
    # adapter with a different identity, so XDrawPage.group and graphic
    # export miss the real shape.
    # Why this change: leave the name to fall through. __getattr__
    # still asserts, then _wrap_uno returns the raw callable. Its
    # return value stays a real PyUNO object. Deepening _unwrap_uno
    # cannot fix this: invoke and pyObject2Any never call it.
    mod = getattr(t, "__module__", "") or ""
    if "pyuno" in mod:
        return True
    # Many UNO objects support XInterface.queryInterface
    for cls in t.__mro__:
        if "queryInterface" in cls.__dict__:
            return True
    return False


class _UnoThreadGuardProxy:
    """Transparent debug wrapper around a PyUNO object.

    Every attribute access and call runs assert_main_thread, then recursively
    wraps any PyUNO result so the guard follows the object graph. A method
    lookup returns the raw PyUNO_callable. Calling that callable does not
    enter this proxy, and the value it returns is not wrapped here.

    Plain Python values pass through. Only installed when GUARD_ON.
    """

    def __init__(self, target: Any) -> None:
        # Use object.__setattr__ to avoid triggering our own __setattr__ during init
        object.__setattr__(self, "_target", target)

    # --- Attribute access (methods and properties) ---
    def __getattr__(self, name: str) -> Any:
        # Bugfix: copy.copy and pickle.loads call hasattr on an instance built
        # with object.__new__ before __dict__ is restored. Reading self._target
        # from __getattr__ when _target is absent called __getattr__ again and
        # raised RecursionError. Raising AttributeError immediately lets hasattr
        # return False so copy/unpickle machinery can apply the saved state.
        if name == "_target":
            raise AttributeError(name)
        # hasattr() uses this path. Returning False off-thread would hide UNO
        # probes from the guard (including pickle/IPython dunder lookups).
        assert_main_thread(f"UNO.{name}")
        val = getattr(self._target, name)
        return _wrap_uno(val)

    def __dir__(self) -> list[str]:
        assert_main_thread("UNO dir")
        return dir(self._target)

    # --- Assignment to properties (e.g. para.NumberingRules = rules) ---
    def __setattr__(self, name: str, value: Any) -> None:
        # Bugfix: startswith("_") wrote the proxy __dict__ and skipped
        # assert_main_thread, so a dev write never reached the UNO target.
        # Only _target is stored locally on the proxy.
        if name == "_target":
            object.__setattr__(self, name, value)
            return
        assert_main_thread(f"UNO set .{name}")
        # Unwrap if caller is handing a proxy back into UNO
        setattr(self._target, name, _unwrap_uno(value))

    # --- Invocation ---
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        assert_main_thread("UNO() call")
        # Unwrap any proxy args (defensive)
        clean_args = tuple(_unwrap_uno(a) for a in args)
        clean_kwargs = {k: _unwrap_uno(v) for k, v in kwargs.items()}
        res = self._target(*clean_args, **clean_kwargs)
        return _wrap_uno(res)

    # --- UNO-specific entry points that return interfaces we must also guard ---
    def queryInterface(self, *args: Any, **kwargs: Any) -> Any:
        assert_main_thread("UNO.queryInterface")
        res = self._target.queryInterface(*args, **kwargs)
        return _wrap_uno(res)

    def getTypes(self, *args: Any, **kwargs: Any) -> Any:
        assert_main_thread("UNO.getTypes")
        return self._target.getTypes(*args, **kwargs)

    # --- Diagnostics / transparency ---
    # Assert before touching _target, and do not catch that RuntimeError.
    # These dunders do not go through __getattr__. Swallowing Exception
    # would let a log on a worker enter PyUNO and hide the violation behind
    # a fallback string. The guard's RuntimeError is an Exception.
    def __repr__(self) -> str:  # type: ignore[override]
        assert_main_thread("UNO repr")
        try:
            return f"<UNOProxy for {self._target!r}>"
        except RuntimeError:
            raise
        except Exception:
            return "<UNOProxy>"

    def __str__(self) -> str:  # type: ignore[override]
        assert_main_thread("UNO str")
        try:
            return str(self._target)
        except RuntimeError:
            raise
        except Exception:
            return "<UNOProxy>"

    # --- Protocols. Implicit dunders skip __getattr__, so they are declared here. ---
    def __iter__(self) -> Iterator[Any]:  # type: ignore[override]
        # Assert on every pull, before the raw iterator advances. One assert
        # here then lets next() walk UNO with no check. A later edit that
        # asserts inside ``for item in it`` is too late: that loop calls
        # next() before the body.
        assert_main_thread("UNO iter")
        it = iter(self._target)

        def _guarded() -> Iterator[Any]:
            while True:
                assert_main_thread("UNO iter")
                try:
                    item = next(it)
                except StopIteration:
                    return
                yield _wrap_uno(item)

        return _guarded()

    def __bool__(self) -> bool:
        assert_main_thread("UNO bool")
        return bool(self._target)

    def __len__(self) -> int:  # type: ignore[override]
        assert_main_thread("UNO len")
        return len(self._target)  # type: ignore[arg-type]

    def __getitem__(self, key: Any) -> Any:
        assert_main_thread("UNO getitem")
        return _wrap_uno(self._target[key])

    def __next__(self) -> Any:
        # Implicit special methods do not use __getattr__. __iter__ guards
        # the for-loop path; it is not __next__. Release _wrap_uno returns
        # the raw object, so next() on an iterator succeeds, but next(proxy)
        # raises TypeError in dev without this. Assert, then delegate, and
        # wrap a PyUNO item the same way __getitem__ does.
        assert_main_thread("UNO next")
        return _wrap_uno(next(self._target))

    def __setitem__(self, key: Any, value: Any) -> None:
        assert_main_thread("UNO setitem")
        self._target[_unwrap_uno(key)] = _unwrap_uno(value)

    def __delitem__(self, key: Any) -> None:
        assert_main_thread("UNO delitem")
        del self._target[_unwrap_uno(key)]

    def __contains__(self, item: object) -> bool:
        assert_main_thread("UNO contains")
        return _unwrap_uno(item) in self._target

    def __enter__(self) -> Any:
        assert_main_thread("UNO enter")
        entered = self._target.__enter__()
        # ``with raw as x`` is ``x is raw`` when __enter__ returns self.
        # A fresh proxy here would make dev identity disagree with release.
        if entered is self._target:
            return self
        return _wrap_uno(entered)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        assert_main_thread("UNO exit")
        return self._target.__exit__(exc_type, exc, tb)

    def __eq__(self, other: object) -> bool:
        # Dev-only proxy must not break ``doc is active``-style equality:
        # without this, ``proxy == unwrapped`` is False for the same UNO object.
        assert_main_thread("UNO eq")
        return self._target == _unwrap_uno(other)

    def __ne__(self, other: object) -> bool:
        assert_main_thread("UNO ne")
        return self._target != _unwrap_uno(other)

    def __lt__(self, other: object) -> bool:
        assert_main_thread("UNO lt")
        return self._target < _unwrap_uno(other)

    def __le__(self, other: object) -> bool:
        assert_main_thread("UNO le")
        return self._target <= _unwrap_uno(other)

    def __gt__(self, other: object) -> bool:
        assert_main_thread("UNO gt")
        return self._target > _unwrap_uno(other)

    def __ge__(self, other: object) -> bool:
        assert_main_thread("UNO ge")
        return self._target >= _unwrap_uno(other)

    def __hash__(self) -> int:
        # __eq__ asserts because it compares UNO objects. Hash must use the
        # same check: an off-thread set lookup calls hash, then __eq__, and
        # used to raise from __eq__ after hash succeeded.
        assert_main_thread("UNO hash")
        return hash(self._target)

    # Intentionally not overridden: __int__, __index__, __float__, __format__,
    # __reduce__, __copy__, __deepcopy__, __getstate__, __setstate__.
    # Implicit special methods do not use __getattr__, so these do not reach
    # _target (TypeError or object's default). copy and pickle restore
    # __dict__["_target"] and are covered by test_proxy_copy / test_proxy_pickle.



def guard_uno(obj: Any) -> Any:
    """Attach viral UNO thread guard at a document/service boundary (alias of ``_wrap_uno``)."""
    return _wrap_uno(obj)


def _wrap_uno(obj: Any) -> Any:
    """Wrap a PyUNO object with the guard proxy (only when GUARD_ON).

    PyUNO usually returns UNO containers (``__iter__`` / ``__getitem__`` wrap).
    A Python ``list``/``tuple`` of interfaces (e.g. ``getControls()``) would
    otherwise escape. One level only; dict **values**, not keys.
    """
    if not GUARD_ON:
        return obj
    if isinstance(obj, _UnoThreadGuardProxy):
        return obj
    if isinstance(obj, list):
        return [_wrap_uno(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_wrap_uno(x) for x in obj)
    if isinstance(obj, dict):
        return {k: _wrap_uno(v) for k, v in obj.items()}
    if not _is_pyuno(obj):
        return obj
    return _UnoThreadGuardProxy(obj)


def _unwrap_uno(obj: Any) -> Any:
    """Return the underlying UNO target if obj is one of our proxies.

    Tuples/lists of proxies (PropertyValue media descriptors) must unwrap
    too: C++ load/detect rejects a tuple of Python proxy objects.
    """
    if isinstance(obj, _UnoThreadGuardProxy):
        return obj._target
    if isinstance(obj, list):
        return [_unwrap_uno(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_unwrap_uno(x) for x in obj)
    if isinstance(obj, dict):
        return {k: _unwrap_uno(v) for k, v in obj.items()}
    return obj


__all__ = [
    "guard_uno",
    "assert_main_thread",
    "main_thread_only",
    "background",
    "background_if_dummy_thread",
    "sync_host_dispatch",
    "in_sync_host_dispatch",
    "set_background_task",
    "get_background_task_name",
    "set_designated_main_thread",
    "get_designated_main_thread",
    "on_main_thread",
    "on_python_main_thread",
    "_wrap_uno",
    "_unwrap_uno",
    "GUARD_ON",
]
