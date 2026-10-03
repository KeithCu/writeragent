import gc
from plugin.framework.event_bus import EventBus, get_event_bus

def test_subscribe_emit():
    bus = EventBus()
    received = []

    def handler(event_data):
        received.append(event_data)

    bus.subscribe("test:event", handler)
    bus.emit("test:event", event_data="hello")

    assert received == ["hello"]

def test_unsubscribe():
    bus = EventBus()
    received = []

    def handler(event_data=None):
        received.append(event_data)

    bus.subscribe("test:event", handler)
    bus.unsubscribe("test:event", handler)
    bus.emit("test:event", event_data="hello")

    assert received == []

def test_weakref_subscribe():
    bus = EventBus()
    received = []

    class Target:
        def handler(self, event_data):
            received.append(event_data)

    target = Target()
    bus.subscribe("test:event", target.handler, weak=True)

    bus.emit("test:event", event_data="first")
    assert received == ["first"]

    target = None
    gc.collect()

    bus.emit("test:event", event_data="second")
    assert received == ["first"] # unchanged

def test_get_event_bus_singleton():
    bus1 = get_event_bus()
    bus2 = get_event_bus()
    assert bus1 is bus2

def test_emit_swallows_exception():
    bus = EventBus()
    received = []

    def bad_handler():
        raise RuntimeError("failed")

    def good_handler():
        received.append("good")

    bus.subscribe("test:event", bad_handler)
    bus.subscribe("test:event", good_handler)

    bus.emit("test:event")

    # Exception was swallowed, good handler still ran
    assert received == ["good"]

def test_emit_no_subscribers():
    bus = EventBus()
    # Should simply return without error
    bus.emit("nonexistent:event", data=123)

def test_unsubscribe_nonexistent_event():
    bus = EventBus()
    def handler():
        pass

    # Should simply return without error
    bus.unsubscribe("nonexistent:event", handler)

def test_unsubscribe_nonexistent_handler():
    bus = EventBus()
    def handler1():
        pass
    def handler2():
        pass

    bus.subscribe("test:event", handler1)
    # Should simply return without error, not touching handler1
    bus.unsubscribe("test:event", handler2)

    # Verify handler1 is still subscribed
    assert len(bus._subscribers.get("test:event", [])) == 1

def test_multiple_subscribers():
    bus = EventBus()
    received1 = []
    received2 = []

    def handler1(event_data):
        received1.append(event_data)

    def handler2(event_data):
        received2.append(event_data)

    bus.subscribe("test:event", handler1)
    bus.subscribe("test:event", handler2)

    bus.emit("test:event", event_data="hello")

    assert received1 == ["hello"]
    assert received2 == ["hello"]


def test_unsubscribe_bound_method_without_stashing():
    """Bound methods are new objects each access; unsubscribe must still match."""
    bus = EventBus()
    received = []

    class Target:
        def handler(self, event_data=None):
            received.append(event_data)

    target = Target()
    bus.subscribe("test:event", target.handler)
    bus.unsubscribe("test:event", target.handler)
    bus.emit("test:event", event_data="hello")
    assert received == []


def test_unsubscribe_weak_bound_method_without_stashing():
    bus = EventBus()
    received = []

    class Target:
        def handler(self, event_data=None):
            received.append(event_data)

    target = Target()
    bus.subscribe("test:event", target.handler, weak=True)
    bus.unsubscribe("test:event", target.handler)
    bus.emit("test:event", event_data="hello")
    assert received == []


def test_emit_drops_reentrant_same_event():
    """Same-thread nested emit of the same name must not re-enter subscribers.

    That is the sidebar hang: config:changed -> setText -> set_config ->
    config:changed. Raising from emit would be swallowed by the outer emit's
    except Exception, so we drop and warn instead.
    """
    bus = EventBus()
    calls = []

    def handler():
        calls.append("enter")
        bus.emit("test:event")
        calls.append("after-nested")

    bus.subscribe("test:event", handler)
    bus.emit("test:event")
    assert calls == ["enter", "after-nested"]


def test_config_changed_reentry_allows_a_different_key():
    bus = EventBus()
    seen = []

    def handler(key, value=None, old_value=None, ctx=None):
        seen.append(key)
        if key == "a":
            bus.emit("config:changed", key="b", value=1)
            bus.emit("config:changed", key="a", value=2)

    bus.subscribe("config:changed", handler)
    bus.emit("config:changed", key="a", value=0)
    assert seen == ["a", "b"]


def test_emit_allows_nested_different_event():
    bus = EventBus()
    order = []

    def outer():
        order.append("outer")
        bus.emit("inner:event")

    def inner():
        order.append("inner")

    bus.subscribe("outer:event", outer)
    bus.subscribe("inner:event", inner)
    bus.emit("outer:event")
    assert order == ["outer", "inner"]


def test_subscribe_during_emit_is_not_in_current_fanout():
    """Snapshot: a handler subscribed mid-emit must wait for the next emit."""
    bus = EventBus()
    order = []

    def late():
        order.append("late")

    def first():
        order.append("first")
        bus.subscribe("test:event", late)

    bus.subscribe("test:event", first)
    bus.emit("test:event")
    assert order == ["first"]
    bus.emit("test:event")
    assert order == ["first", "first", "late"]


def test_unsubscribe_during_emit_still_runs_current_snapshot():
    """Snapshot: unsubscribing the current handler does not abort this emit."""
    bus = EventBus()
    calls = []

    def handler():
        calls.append("run")
        bus.unsubscribe("test:event", handler)

    bus.subscribe("test:event", handler)
    bus.emit("test:event")
    assert calls == ["run"]
    bus.emit("test:event")
    assert calls == ["run"]


def test_emit_allows_sequential_same_event():
    bus = EventBus()
    calls = []

    def handler():
        calls.append(1)

    bus.subscribe("test:event", handler)
    bus.emit("test:event")
    bus.emit("test:event")
    assert calls == [1, 1]


def test_weakref_subscribe_callable():
    bus = EventBus()
    received = []

    def make_handler():
        def _handler(event_data):
            received.append(event_data)
        return _handler

    handler = make_handler()
    bus.subscribe("test:event", handler, weak=True)
    bus.emit("test:event", event_data="first")
    assert received == ["first"]

    del handler
    gc.collect()

    bus.emit("test:event", event_data="second")
    assert received == ["first"]


def test_get_event_bus_reuses_bus_with_same_qualified_name():
    """Two EventBus classes with the same qualified name must share one bus.

    A second import defines a new class object. isinstance against the class
    from this import used to miss the stored bus and overwrite it.
    """
    import sys

    class FirstBus:
        pass

    class SecondBus:
        pass

    # Same qualified name as EventBus, different class objects. Assigning
    # these is what a second import of this module produces.
    FirstBus.__module__ = EventBus.__module__
    FirstBus.__qualname__ = EventBus.__qualname__
    SecondBus.__module__ = EventBus.__module__
    SecondBus.__qualname__ = EventBus.__qualname__
    saved = getattr(sys, "_writeragent_event_bus", None)
    first_cls = FirstBus
    second_cls = SecondBus
    assert first_cls is not second_cls
    assert (first_cls.__module__, first_cls.__qualname__) == (EventBus.__module__, EventBus.__qualname__)
    assert (second_cls.__module__, second_cls.__qualname__) == (first_cls.__module__, first_cls.__qualname__)
    stored = first_cls()
    assert not isinstance(stored, EventBus)
    try:
        setattr(sys, "_writeragent_event_bus", stored)
        assert get_event_bus() is stored
        assert get_event_bus() is stored
    finally:
        if saved is None:
            delattr(sys, "_writeragent_event_bus")
        else:
            setattr(sys, "_writeragent_event_bus", saved)


def test_get_event_bus_replaces_object_with_other_qualified_name():
    import sys

    saved = getattr(sys, "_writeragent_event_bus", None)

    class NotTheBus:
        pass

    foreign = NotTheBus()
    try:
        setattr(sys, "_writeragent_event_bus", foreign)
        bus = get_event_bus()
        assert isinstance(bus, EventBus)
        assert bus is not foreign
        assert get_event_bus() is bus
    finally:
        if saved is None:
            delattr(sys, "_writeragent_event_bus")
        else:
            setattr(sys, "_writeragent_event_bus", saved)


def test_subscribe_weak_builtin_inline_survives_gc():
    """Inline ``items.append`` with ``weak=True`` must still run after GC.

    What was wrong: WeakMethod rejects the builtin, and ``weakref.ref``
    kept the temporary bound method. The test that stashed
    ``method = items.append`` kept that object alive, so the drop never
    showed up. Callers subscribe inline and do not keep the method.
    """
    bus = EventBus()
    items: list[int] = []
    bus.subscribe("test:event", items.append, weak=True)
    gc.collect()
    stored, is_weak = bus._subscribers["test:event"][0]
    assert is_weak is False
    # emit() passes keywords; append is positional-only, so call the
    # resolved subscriber the same way emit resolves it.
    resolved = bus._resolve(stored, is_weak)
    assert resolved == items.append
    resolved(1)
    assert items == [1]


def test_subscribe_weak_slots_method_inline_survives_gc():
    """A method on an instance with no ``__weakref__`` must still run after GC.

    WeakMethod cannot reference the instance. ``weakref.ref`` of the
    temporary bound method dies even while the caller still holds the instance.
    """
    bus = EventBus()
    received = []

    class Slotted:
        __slots__ = ()

        def handler(self, event_data=None):
            received.append(event_data)

    target = Slotted()
    bus.subscribe("test:event", target.handler, weak=True)
    gc.collect()
    bus.emit("test:event", event_data="ok")
    assert received == ["ok"]


def test_unsubscribe_builtin_does_not_drop_sibling():
    """``append`` and ``clear`` on one list are different callbacks.

    Both have ``__self__`` and neither has ``__func__``. Treating the
    missing ``__func__`` as equal used to remove both.
    """
    bus = EventBus()
    items: list[int] = []
    bus.subscribe("test:event", items.append, weak=True)
    bus.subscribe("test:event", items.clear, weak=True)
    bus.unsubscribe("test:event", items.append)
    remaining = [bus._resolve(cb, is_weak) for cb, is_weak in bus._subscribers["test:event"]]
    assert remaining == [items.clear]


def test_subscribe_weak_method_wrapper_falls_back_to_strong_ref():
    """method-wrapper has __self__, and neither WeakMethod nor weakref.ref accepts it."""
    bus = EventBus()
    number = 5
    method = number.__add__
    bus.subscribe("test:event", method, weak=True)
    stored, is_weak = bus._subscribers["test:event"][0]
    assert is_weak is False
    assert stored is method


def test_weakref_cleanup_while_lock_held_does_not_deadlock():
    """GC of a cyclic weak subscriber must not hang on the bus lock.

    What was wrong: emit/subscribe allocate while holding a non-reentrant
    Lock. Collecting the subscriber ran ``_cleanup``, which took that lock
    again on the same thread and never returned.
    """
    import threading

    bus = EventBus()

    class Cyclic:
        def handler(self, **_kwargs):
            pass

    node = Cyclic()
    node.cycle = node
    bus.subscribe("gone", node.handler, weak=True)
    del node

    done = threading.Event()

    def _collect_while_held():
        with bus._lock:
            gc.collect()
        done.set()

    worker = threading.Thread(target=_collect_while_held, daemon=True)
    worker.start()
    assert done.wait(2.0), "weakref cleanup deadlocked on the bus lock"
    worker.join(timeout=1.0)
    assert not worker.is_alive()
    # Cleanup ran on the thread that held the lock. A skipped callback would
    # leave the dead weakref in the list.
    assert bus._subscribers.get("gone") == []
    bus.emit("gone")


def test_subscribe_weak_python_method_stays_weakmethod():
    import weakref

    bus = EventBus()

    class Target:
        def handler(self, event_data=None):
            return event_data

    target = Target()
    bus.subscribe("test:event", target.handler, weak=True)
    stored, is_weak = bus._subscribers["test:event"][0]
    assert is_weak is True
    assert isinstance(stored, weakref.WeakMethod)

