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
"""Lightweight synchronous event bus for inter-module communication.

Concurrency: ``emit`` copies the listener list under a lock, then calls
those callables **on the emitting thread** (often a worker). The lock is
not held while handlers run — that would deadlock the UI against workers
and would block two legitimate same-named events from different threads.
``subscribe`` during an emit is not in that fan-out. Snapshotting is not
UNO safety: a handler that touches Writer still belongs on the main
thread. Full rules on the ``EventBus`` class and
docs/framework/threading.md.
"""

from __future__ import annotations

import contextlib
import sys
import logging
import threading
import weakref
from typing import Any, Iterator, cast

log = logging.getLogger("writeragent.events")


from plugin.framework.deal_shim import deal


def _reentry_token(event: str, data: dict[str, Any]) -> str:
    """Identity used to drop a nested emit. config:changed includes the key."""
    if event == "config:changed":
        return "config:changed:" + str(data.get("key"))
    return event


class EventBus:
    """Publish/subscribe event bus.

    All callbacks run synchronously on the calling thread. Exceptions in
    subscribers are logged but never propagated to the emitter.

    ``emit`` iterates a snapshot of the subscriber list so a concurrent
    ``subscribe`` append cannot skip/shift listeners mid-loop. There is no
    mutex across callbacks: a lock held while handlers run would deadlock
    UI vs workers and would serialize same-name emits that other threads
    are allowed to run. Handlers that touch UNO still belong on the main
    thread (see docs/framework/threading.md); a snapshot is not UNO safety.

    Re-entrant ``emit`` of the *same* event on the same thread is dropped
    (warning logged). That stops the sidebar config-refresh loop where
    ``config:changed`` → control ``setText`` → listener → ``set_config`` →
    ``config:changed`` for the same key. A nested change of a different key
    still runs. Nested *different* events still run. Concurrent
    emits of the same name on *other* threads are not dropped (thread-local
    dispatch set, not a process-wide lock).

    Usage::

        bus = EventBus()
        bus.subscribe("config:changed", my_callback)
        bus.emit("config:changed", key="mcp.port", value=9000)

    Weak references are supported to avoid preventing garbage collection
    of listener objects::

        bus.subscribe("document:closed", obj.on_close, weak=True)
    """

    _dispatching: threading.local

    def __init__(self) -> None:
        # crosshair: off  # threading.local() is engine-hostile (cover-all 33093268817: exit 1, 0 contract errors)
        self._subscribers: dict[str, list[tuple[Any, bool]]] = {}  # event -> list of (callback, is_weakref)
        # RLock: a weakref callback can run on this thread while the lock is
        # already held (GC during a list copy). A plain Lock deadlocks there.
        # _guard_depth is how many of our own sections are on the stack.
        # Cleanup is queued and applied only at the outermost exit so a
        # callback cannot replace the list a caller is still copying.
        self._lock: threading.RLock = threading.RLock()
        self._guard_depth: int = 0
        self._applying_cleanups: bool = False
        self._pending_cleanups: list[tuple[str, Any]] = []
        # Per-thread names currently in emit(); instance-wide would drop
        # legitimate parallel emits of the same event from two threads.
        self._dispatching = threading.local()

    @contextlib.contextmanager
    def _guard(self) -> Iterator[None]:
        """Hold ``_lock`` and apply queued weakref drops after the outermost exit."""
        # crosshair: off
        self._lock.acquire()
        self._guard_depth += 1
        try:
            yield
        finally:
            self._guard_depth -= 1
            try:
                if self._guard_depth == 0:
                    self._apply_pending_cleanups()
            finally:
                self._lock.release()

    def subscribe(self, event: str, callback: Any, weak: bool = False) -> None:
        """Register *callback* for *event*.

        Args:
            event:    Event name (e.g. "config:changed").
            callback: Callable to invoke when the event is emitted.
            weak:     If True, store a weakref to the callback's bound
                      object. The subscription auto-removes when the
                      object is garbage-collected. If ``WeakMethod``
                      rejects the callable, it is stored strongly.
                      ``weakref.ref`` would point at a temporary bound
                      method and the subscription would die on the next GC.
        """
        # crosshair: off  # threading.local() is engine-hostile (cover-all 33093268817: exit 1, 0 contract errors)
        entry: tuple[Any, bool]
        if weak and hasattr(callback, "__self__"):
            try:
                entry = (weakref.WeakMethod(callback, lambda r: self._cleanup(event, r)), True)
            except TypeError:
                # WeakMethod rejects builtins and methods of instances with no
                # ``__weakref__`` (``__slots__``). Keep the callback strongly.
                # ``weakref.ref(callback)`` tracks the bound-method object, not
                # the instance. Callers pass the method inline (``items.append``)
                # and do not keep it, so the next GC would drop the subscription
                # and emit would never run. A strong ref still fires. Python
                # methods on normal instances stay on the WeakMethod path above.
                entry = (callback, False)
        elif weak:
            entry = self._weakref_or_strong(event, callback)
        else:
            entry = (callback, False)
        with self._guard():
            self._subscribers.setdefault(event, []).append(entry)

    def _weakref_or_strong(self, event: str, callback: Any) -> tuple[Any, bool]:
        """Weak-ref a callable the caller holds, or keep it if it cannot be weak.

        Bound methods do not belong here. ``weakref.ref`` would track the
        temporary method object, which dies when the caller did not stash it.
        """
        # crosshair: off  # threading.local() is engine-hostile (cover-all 33093268817: exit 1, 0 contract errors)
        try:
            return (weakref.ref(callback, lambda r: self._cleanup(event, r)), True)
        except TypeError:
            return (callback, False)

    def unsubscribe(self, event: str, callback: Any) -> None:
        """Remove *callback* from *event*."""
        # crosshair: off  # threading.local() is engine-hostile (cover-all 33093268817: exit 1, 0 contract errors)
        with self._guard():
            subs = self._subscribers.get(event)
            if not subs:
                return
            # Replace the list; an in-flight emit already holds a snapshot.
            self._subscribers[event] = [(cb, is_weak) for cb, is_weak in subs if not self._same_callback(self._resolve(cb, is_weak), callback)]

    @staticmethod
    def _same_callback(stored: Any, callback: Any) -> bool:
        """True if *stored* is the same callable the caller passed.

        Bound methods are new objects on every attribute access
        (``obj.m is obj.m`` is False), so identity alone never matches
        ``unsubscribe("e", obj.handler)``. Compare ``__self__``/``__func__``.
        Callables with ``__self__`` but no ``__func__`` (builtins,
        method-wrappers) compare with ``==`` so a sibling method on the
        same object does not match.
        """
        if stored is None:
            return False
        if stored is callback:
            return True
        stored_self = getattr(stored, "__self__", None)
        other_self = getattr(callback, "__self__", None)
        if stored_self is None or other_self is None:
            return False
        stored_func = getattr(stored, "__func__", None)
        other_func = getattr(callback, "__func__", None)
        if stored_func is None and other_func is None:
            # Builtins and method-wrappers have ``__self__`` and no ``__func__``.
            # ``None is None`` would treat every pair as the same method, so
            # ``items.append`` would unsubscribe ``items.clear``. ``==`` is
            # true for the same builtin method and false for a sibling.
            return bool(stored_self is other_self and stored == callback)
        return stored_self is other_self and stored_func is other_func

    def _active_events(self) -> set[str]:
        # crosshair: off  # threading.local() is engine-hostile (cover-all 33093268817: exit 1, 0 contract errors)
        active = getattr(self._dispatching, "events", None)
        if active is None:
            active = set()
            self._dispatching.events = active
        return active

    @deal.post(lambda result: result is None)
    def emit(self, event: str, **data: Any) -> None:
        """Emit *event*, calling all subscribers with **data as kwargs.

        Exceptions in subscribers are logged and swallowed.
        Re-entrant emit of the same event on this thread is dropped.
        ``config:changed`` is dropped only when the key matches, so a handler
        that writes a second key still notifies listeners.
        """
        # crosshair: off
        with self._guard():
            live = self._subscribers.get(event)
            if not live:
                return
            # Frozen listeners at emit time. Dead weakrefs stay until _cleanup
            # or unsubscribe replace the stored list; do not pop in place.
            subs = list(live)

        active = self._active_events()
        token = _reentry_token(event, data)
        if token in active:
            log.warning("Suppressed re-entrant event_bus.emit for %r on the same thread", token)
            return

        active.add(token)
        try:
            for cb, is_weak in subs:
                resolved = self._resolve(cb, is_weak)
                if resolved is None:
                    continue
                try:
                    resolved(**data)
                except Exception as e:
                    # One bad listener must not stop the rest of the fan-out.
                    log.exception("Unhandled error in event handler %s for %s: %s", resolved, event, e)
        finally:
            active.discard(token)

    def _resolve(self, cb: Any, is_weak: bool) -> Any:
        # crosshair: off  # threading.local() is engine-hostile (cover-all 33093268817: exit 1, 0 contract errors)
        if is_weak:
            return cb()  # weakref -> call to dereference
        return cb

    def _cleanup(self, event: str, ref: Any) -> None:
        """Called when a weakref target is garbage-collected.

        ``_lock`` is an ``RLock``. Emit, subscribe, and unsubscribe allocate
        while holding it, and GC of a cyclic weak subscriber runs this
        callback on that same thread. A non-reentrant lock taken again here
        never returns. Queue the dead ref and apply it only when this thread
        is not already inside ``_guard`` (outermost exit, or a callback that
        found the lock free). The list a caller is copying stays stable until
        that copy finishes, and the callback cannot deadlock against the bus.
        """
        # crosshair: off  # threading.local() is engine-hostile (cover-all 33093268817: exit 1, 0 contract errors)
        self._lock.acquire()
        try:
            self._pending_cleanups.append((event, ref))
            # _apply drops the depth to 0 before it runs, and the list copy
            # can collect another subscriber. Don't re-enter apply from that
            # callback; the loop already in progress picks the new ref up.
            if self._guard_depth == 0 and not self._applying_cleanups:
                self._apply_pending_cleanups()
        finally:
            self._lock.release()

    def _apply_pending_cleanups(self) -> None:
        """Drop queued dead refs. Caller holds ``_lock``."""
        # crosshair: off
        self._applying_cleanups = True
        try:
            while self._pending_cleanups:
                pending = self._pending_cleanups
                self._pending_cleanups = []
                for event, ref in pending:
                    subs = self._subscribers.get(event)
                    if not subs:
                        continue
                    # Replace, do not mutate in place (emit may still hold a snapshot).
                    self._subscribers[event] = [(cb, w) for cb, w in subs if cb is not ref]
        finally:
            self._applying_cleanups = False


def get_event_bus() -> EventBus:
    """Return the true singleton EventBus across all LO import contexts."""
    if not hasattr(sys, "_writeragent_event_bus"):
        setattr(sys, "_writeragent_event_bus", EventBus())
    return cast("EventBus", getattr(sys, "_writeragent_event_bus"))


global_event_bus = get_event_bus()
