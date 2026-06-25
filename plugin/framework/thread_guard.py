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

See docs/uno-thread-safety-enforcement.md (Layer A).
"""

import os
import threading
import logging
from typing import Any

log = logging.getLogger("writeragent.threadguard")

# Non-release bundles ship this full module with guard on by default.
# Release bundles replace this file with a stub (GUARD_ON = False).
# Opt out in dev: WRITERAGENT_UNO_THREAD_GUARD=0
GUARD_ON = os.environ.get("WRITERAGENT_UNO_THREAD_GUARD", "1") == "1"

# Thread-local storage for background task identity (set at birth in run_in_background).
_bg = threading.local()

# Layer B pytest: when set, on_main_thread() treats this thread as the UNO main thread
# (typically the synthetic pump thread started by tests/framework/thread_safety.py).
_designated_main_thread: threading.Thread | None = None


def set_designated_main_thread(thread: threading.Thread | None) -> None:
    """Test hook: designate which thread may touch UNO (see docs/uno-thread-safety-enforcement.md Layer B)."""
    global _designated_main_thread
    _designated_main_thread = thread


def get_designated_main_thread() -> threading.Thread | None:
    return _designated_main_thread


def on_main_thread() -> bool:
    current = threading.current_thread()
    if _designated_main_thread is not None:
        return current is _designated_main_thread
    return current is threading.main_thread()


def set_background_task(name: str) -> None:
    """Tag the current thread as a background worker task (for better diagnostics)."""
    try:
        _bg.task_name = name
    except Exception:
        pass


def get_background_task_name() -> str | None:
    return getattr(_bg, "task_name", None)


# At most one modal alert per background thread (proxy can fire on every UNO access).
_violation_ui_threads: set[int] = set()
_violation_ui_lock = threading.Lock()


def _notify_thread_violation(msg: str) -> None:
    """Log and post a main-thread message box for guard-on violations (dev only)."""
    log.error(msg, stack_info=True)
    if os.environ.get("WRITERAGENT_TESTING") == "1":
        return
    tid = threading.get_ident()
    with _violation_ui_lock:
        if tid in _violation_ui_threads:
            return
        _violation_ui_threads.add(tid)

    def _show_popup() -> None:
        try:
            from plugin.framework.uno_context import get_ctx
            from plugin.chatbot.dialogs import msgbox
            from plugin.framework.i18n import _

            msgbox(get_ctx(), _("UNO Thread Violation"), msg, box_type=3)
        except Exception:
            log.exception("Failed to show thread violation message box")

    try:
        from plugin.framework.queue_executor import execute_on_main_thread

        # Blocking marshal: post_to_main_thread can inline on the worker when
        # AsyncCallback is missing, which re-triggers the guard inside msgbox.
        execute_on_main_thread(_show_popup, timeout=5.0)
    except Exception:
        log.exception("Failed to show thread violation message box on main thread")


def assert_main_thread(what: str) -> None:
    """Raise (if guard on) or log warning+stack (if guard off) when off the main thread."""
    if on_main_thread():
        return
    task = get_background_task_name() or threading.current_thread().name
    msg = "UNO thread violation: %r touched UNO from background task %r; marshal via execute_on_main_thread()." % (what, task)
    if GUARD_ON:
        _notify_thread_violation(msg)
        raise RuntimeError(msg)
    log.warning(msg, stack_info=True)


def main_thread_only(fn):
    """Decorator: assert main thread on entry. Use on UNO source functions."""
    def wrapper(*a, **k):
        assert_main_thread(getattr(fn, "__qualname__", str(fn)))
        return fn(*a, **k)
    return wrapper


def _is_pyuno(obj: Any) -> bool:
    """Heuristic: is this a real PyUNO object we should guard?"""
    if obj is None:
        return False
    # Existing test-mock fast-paths used elsewhere in the tree
    t = type(obj)
    tname = t.__name__
    if tname in ("Mock", "MagicMock") or hasattr(obj, "_mock_return_value"):
        return False
    mod = getattr(t, "__module__", "") or ""
    if "pyuno" in mod:
        return True
    if hasattr(obj, "__pyunostruct__"):
        return True
    # Many UNO objects support XInterface.queryInterface
    if hasattr(obj, "queryInterface"):
        return True
    return False


class _UnoThreadGuardProxy:
    """Transparent debug wrapper around a PyUNO object.

    Every attribute access and call runs assert_main_thread, then recursively
    wraps any PyUNO result so the guard follows the object graph.

    Plain Python values pass through. Only installed when GUARD_ON.
    """

    def __init__(self, target: Any) -> None:
        # Use object.__setattr__ to avoid triggering our own __setattr__ during init
        object.__setattr__(self, "_target", target)

    # --- Attribute access (methods and properties) ---
    def __getattr__(self, name: str) -> Any:
        assert_main_thread(f"UNO.{name}")
        val = getattr(self._target, name)
        return _wrap_uno(val)

    # --- Assignment to properties (e.g. para.NumberingRules = rules) ---
    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
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
    def __repr__(self) -> str:  # type: ignore[override]
        try:
            return f"<UNOProxy for {self._target!r}>"
        except Exception:
            return "<UNOProxy>"

    def __str__(self) -> str:  # type: ignore[override]
        try:
            return str(self._target)
        except Exception:
            return "<UNOProxy>"

    # --- Common protocols used by enumeration walks etc. (explicit methods are covered by __getattr__) ---
    def __iter__(self):  # type: ignore[override]
        assert_main_thread("UNO iter")
        it = iter(self._target)
        # Yield wrapped items lazily
        return (_wrap_uno(x) for x in it)

    def __bool__(self) -> bool:
        assert_main_thread("UNO bool")
        return bool(self._target)

    def __len__(self) -> int:  # type: ignore[override]
        assert_main_thread("UNO len")
        return len(self._target)  # type: ignore[arg-type]

    def __getitem__(self, key: Any) -> Any:
        assert_main_thread("UNO getitem")
        return _wrap_uno(self._target[key])

    # Expose the real target for the (rare) cases that need the concrete UNO object under the guard
    @property
    def __uno_target__(self) -> Any:
        return self._target


def _wrap_uno(obj: Any) -> Any:
    """Wrap a PyUNO object with the guard proxy (only when GUARD_ON)."""
    if not GUARD_ON:
        return obj
    if not _is_pyuno(obj):
        return obj
    if isinstance(obj, _UnoThreadGuardProxy):
        return obj
    return _UnoThreadGuardProxy(obj)


def _unwrap_uno(obj: Any) -> Any:
    """Return the underlying UNO target if obj is one of our proxies."""
    if isinstance(obj, _UnoThreadGuardProxy):
        return obj._target
    return obj


__all__ = [
    "assert_main_thread",
    "main_thread_only",
    "set_background_task",
    "get_background_task_name",
    "set_designated_main_thread",
    "get_designated_main_thread",
    "on_main_thread",
    "_wrap_uno",
    "_unwrap_uno",
    "GUARD_ON",
]
