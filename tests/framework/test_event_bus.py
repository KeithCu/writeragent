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

