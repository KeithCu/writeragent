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
"""Lightweight synchronous event bus for inter-module communication."""

import sys
import logging
import weakref

from plugin.framework.service_base import ServiceBase

log = logging.getLogger("writeragent.events")


class EventBus:
    """Publish/subscribe event bus.

    All callbacks run synchronously on the calling thread. Exceptions in
    subscribers are logged but never propagated to the emitter.

    Usage::

        bus = EventBus()
        bus.subscribe("config:changed", my_callback)
        bus.emit("config:changed", key="mcp.port", value=9000)

    Weak references are supported to avoid preventing garbage collection
    of listener objects::

        bus.subscribe("document:closed", obj.on_close, weak=True)
    """

    def __init__(self):
        self._subscribers = {}  # event -> list of (callback, is_weakref)

    def subscribe(self, event, callback, weak=False):
        """Register *callback* for *event*.

        Args:
            event:    Event name (e.g. "config:changed").
            callback: Callable to invoke when the event is emitted.
            weak:     If True, store a weakref to the callback's bound
                      object. The subscription auto-removes when the
                      object is garbage-collected.
        """
        if event not in self._subscribers:
            self._subscribers[event] = []

        if weak and hasattr(callback, "__self__"):
            ref = weakref.WeakMethod(callback, lambda r: self._cleanup(event, r))
            self._subscribers[event].append((ref, True))
        else:
            self._subscribers[event].append((callback, False))

    def unsubscribe(self, event, callback):
        """Remove *callback* from *event*."""
        subs = self._subscribers.get(event)
        if not subs:
            return

        self._subscribers[event] = [(cb, is_weak) for cb, is_weak in subs if self._resolve(cb, is_weak) is not callback]

    def emit(self, event, **data):
        """Emit *event*, calling all subscribers with **data as kwargs.

        Exceptions in subscribers are logged and swallowed.
        """
        subs = self._subscribers.get(event)
        if not subs:
            return

        dead = []
        for i, (cb, is_weak) in enumerate(subs):
            resolved = self._resolve(cb, is_weak)
            if resolved is None:
                dead.append(i)
                continue
            try:
                resolved(**data)
            except TypeError as e:
                log.error("TypeError in event handler %s for %s: %s", resolved, event, e)
            except ValueError as e:
                log.error("ValueError in event handler %s for %s: %s", resolved, event, e)
            except Exception as e:
                # Still catch Exception to avoid one bad listener breaking the whole bus,
                # but log it clearly as an unhandled application error
                log.exception("Unhandled error in event handler %s for %s: %s", resolved, event, e)

        # Clean up dead weakrefs
        if dead:
            for i in reversed(dead):
                subs.pop(i)

    def _resolve(self, cb, is_weak):
        if is_weak:
            return cb()  # weakref -> call to dereference
        return cb

    def _cleanup(self, event, ref):
        """Called when a weakref target is garbage-collected."""
        subs = self._subscribers.get(event)
        if subs:
            self._subscribers[event] = [(cb, w) for cb, w in subs if cb is not ref]


def get_event_bus():
    """Return the true singleton EventBus across all LO import contexts."""
    if not hasattr(sys, "_localwriter_event_bus"):
        setattr(sys, "_localwriter_event_bus", EventBus())
    return getattr(sys, "_localwriter_event_bus")


global_event_bus = get_event_bus()


class EventBusService(ServiceBase, EventBus):
    """Singleton event bus exposed as a service.

    Inherits from both ServiceBase (for registry) and EventBus (for
    pub/sub). Modules access it as ``services.events``.
    """

    name = "events"

    def __init__(self):
        ServiceBase.__init__(self)
        self._subscribers = global_event_bus._subscribers
