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

import logging
import sys
import threading
import weakref
from typing import Any, cast

log = logging.getLogger("writeragent.events")


def _reentry_token(event: str, data: dict[str, Any]) -> str:
    """Identity used to drop a nested emit. config:changed includes the key set."""
    if event == "config:changed":
        # Bugfix: Batch saves pass key="" plus the full key set (config._emit_pending).
        # Keying on key alone made every nested batch collide on "config:changed:",
        # dropping nested batch emits for different key sets. Including the keys
        # tuple distinguishes distinct batches while still dropping re-entrant dupes.
        return f"config:changed:{data.get('key')!r}:{tuple(data.get('keys') or ())!r}"
    return event


class _Dispatching(threading.local):
    def __init__(self) -> None:
        # crosshair: off
        # threading.local() is engine-hostile.
        self.events: set[str] = set()


class EventBus:
    """Publish/subscribe event bus.

    All callbacks run synchronously on the calling thread. Exceptions in
    subscribers are logged but never propagated to the emitter.

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

    _dispatching: _Dispatching

    def __init__(self) -> None:
        # crosshair: off
        # threading.local() is engine-hostile.
        self._subscribers: dict[str, list[Any]] = {}  # event -> list of callback or weakref
        # Dead weak refs are dropped on the next locked subscribe, unsubscribe, or
        # emit. No weakref callback: that callback re-entered this lock from GC.
        self._lock: threading.RLock = threading.RLock()
        self._dispatching = _Dispatching()

    def _deref(self, entry: Any) -> Any:
        # crosshair: off
        if isinstance(entry, weakref.ref):
            return entry()
        return entry

    def _live_entries(self, event: str) -> tuple[list[Any], list[Any]]:
        """Caller holds ``_lock``. Return (live entries, strong callbacks) from one pass."""
        kept: list[Any] = []
        resolved: list[Any] = []
        for entry in self._subscribers.get(event, []):
            cb = self._deref(entry)
            if cb is None:
                continue
            kept.append(entry)
            resolved.append(cb)
        return kept, resolved

    def subscribe(self, event: str, callback: Any, weak: bool = False) -> None:
        """Register *callback* for *event*.

        Args:
            event:    Event name (e.g. "config:changed").
            callback: Callable to invoke when the event is emitted.
            weak:     If True, store a weakref to the callback's bound
                      object. The listener object can be collected; the
                      dead entry is skipped and removed on the next
                      ``subscribe``, ``unsubscribe``, or ``emit`` for that
                      event. If ``WeakMethod`` rejects the callable, it is
                      stored strongly. ``weakref.ref`` would point at a
                      temporary bound method and the subscription would
                      die on the next GC.
        """
        # crosshair: off
        # threading.local() is engine-hostile.
        entry: Any
        if weak:
            try:
                # Bound methods use WeakMethod. weakref.ref would track the
                # temporary method, and an inline subscribe (items.append)
                # would die on the next GC. TypeError (builtin, slots,
                # method-wrapper) keeps a strong ref so emit still runs.
                entry = (
                    weakref.WeakMethod(callback)
                    if hasattr(callback, "__self__")
                    else weakref.ref(callback)
                )
            except TypeError:
                entry = callback
        else:
            entry = callback
        with self._lock:
            kept, _callbacks = self._live_entries(event)
            kept.append(entry)
            self._subscribers[event] = kept

    def unsubscribe(self, event: str, callback: Any) -> None:
        """Remove *callback* from *event*."""
        # crosshair: off
        # threading.local() is engine-hostile.
        with self._lock:
            if not self._subscribers.get(event):
                return
            kept, resolved = self._live_entries(event)
            self._subscribers[event] = [
                entry for entry, live in zip(kept, resolved) if live != callback
            ]

    # Bugfix: A payload key 'event' or 'self' previously bound the parameter
    # and raised TypeError ("got multiple values for argument 'event'" or
    # "... unexpected keyword argument 'self'") before any subscriber ran.
    # Making self and event positional-only ensures that payload keys named
    # 'event' or 'self' land safely in **data.
    def emit(self, event: str, /, **data: Any) -> None:
        """Emit *event*, calling all subscribers with **data as kwargs.

        The event name is positional-only so a payload may contain keys named
        'event' or 'self'. Exceptions in subscribers are logged and swallowed.
        """
        # crosshair: off
        with self._lock:
            live = self._subscribers.get(event)
            if not live:
                return
            kept, resolved = self._live_entries(event)
            if len(kept) != len(live):
                self._subscribers[event] = kept

        active = self._dispatching.events
        token = _reentry_token(event, data)
        if token in active:
            log.warning("Suppressed re-entrant event_bus.emit for %r on the same thread", token)
            return

        active.add(token)
        try:
            for cb in resolved:
                try:
                    cb(**data)
                except Exception:
                    # One bad listener must not stop the rest of the fan-out.
                    log.exception("Unhandled error in event handler %s for %s", cb, event)
        finally:
            active.discard(token)


def get_event_bus() -> EventBus:
    """Return the true singleton EventBus across all LO import contexts.

    All callers call this at the point of use; there is deliberately no module-level alias.
    """
    if not hasattr(sys, "_writeragent_event_bus"):
        setattr(sys, "_writeragent_event_bus", EventBus())
    return cast("EventBus", getattr(sys, "_writeragent_event_bus"))
