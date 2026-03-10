"""Tests for plugin.framework.event_bus."""

import gc
import pytest

from plugin.framework.event_bus import EventBus


class TestSubscribeEmit:
    def test_basic_emit(self):
        bus = EventBus()
        received = []
        bus.subscribe("test", lambda **kw: received.append(kw))
        bus.emit("test", key="a", value=1)
        assert received == [{"key": "a", "value": 1}]

    def test_multiple_subscribers(self):
        bus = EventBus()
        a, b = [], []
        bus.subscribe("evt", lambda **kw: a.append(kw))
        bus.subscribe("evt", lambda **kw: b.append(kw))
        bus.emit("evt", x=42)
        assert a == [{"x": 42}]
        assert b == [{"x": 42}]

    def test_emit_unknown_event_does_nothing(self):
        bus = EventBus()
        bus.emit("nonexistent", data="whatever")  # no error

    def test_events_are_isolated(self):
        bus = EventBus()
        received = []
        bus.subscribe("a", lambda **kw: received.append("a"))
        bus.subscribe("b", lambda **kw: received.append("b"))
        bus.emit("a")
        assert received == ["a"]


class TestUnsubscribe:
    def test_unsubscribe_removes_callback(self):
        bus = EventBus()
        received = []
        cb = lambda **kw: received.append(1)
        bus.subscribe("evt", cb)
        bus.unsubscribe("evt", cb)
        bus.emit("evt")
        assert received == []

    def test_unsubscribe_unknown_event_safe(self):
        bus = EventBus()
        bus.unsubscribe("nope", lambda **kw: None)  # no error

    def test_unsubscribe_unknown_callback_safe(self):
        bus = EventBus()
        bus.subscribe("evt", lambda **kw: None)
        bus.unsubscribe("evt", lambda **kw: None)  # different lambda, no error


class TestExceptionHandling:
    def test_exception_in_subscriber_does_not_propagate(self):
        bus = EventBus()
        received = []

        def bad(**kw):
            raise RuntimeError("boom")

        def good(**kw):
            received.append("ok")

        bus.subscribe("evt", bad)
        bus.subscribe("evt", good)
        bus.emit("evt")  # should not raise
        assert received == ["ok"]


class TestWeakRefs:
    def test_weak_ref_auto_cleanup(self):
        bus = EventBus()
        received = []

        class Listener:
            def on_event(self, **kw):
                received.append("called")

        obj = Listener()
        bus.subscribe("evt", obj.on_event, weak=True)
        bus.emit("evt")
        assert received == ["called"]

        del obj
        gc.collect()
        # This will trigger `dead.append(i)` block in `emit`
        bus.emit("evt")
        assert received == ["called"]  # not called again

    def test_weak_ref_cleanup_method(self):
        bus = EventBus()

        class Listener:
            def on_event(self, **kw):
                pass

        obj = Listener()
        bus.subscribe("evt", obj.on_event, weak=True)

        # Simulate weakref cleanup callback
        ref_tuple = bus._subscribers["evt"][0]
        bus._cleanup("evt", ref_tuple[0])

        # The listener should be removed from subscribers
        assert len(bus._subscribers["evt"]) == 0

        # test cleanup missing evt
        bus._cleanup("missing_evt", None)

    def test_weak_ref_dead_emit(self):
        bus = EventBus()
        class Listener:
            def on_event(self, **kw):
                pass
        obj = Listener()
        bus.subscribe("evt", obj.on_event, weak=True)

        # Manually make the weakref dead by removing its target.
        # we can simulate the "None" resolved by overwriting the tuple with a dead weakref.
        import weakref

        class DeadTarget:
            def method(self): pass

        dt = DeadTarget()
        dead_ref = weakref.WeakMethod(dt.method)
        del dt # now dead_ref() is None

        bus._subscribers["evt"] = [(dead_ref, True)]

        bus.emit("evt")
        assert len(bus._subscribers["evt"]) == 0

    def test_weak_ref_unsubscribe(self):
        bus = EventBus()

        class Listener:
            def on_event(self, **kw):
                pass

        obj = Listener()
        bus.subscribe("evt", obj.on_event, weak=True)

        # also add a strong ref to test that it is not removed
        def regular(**kw): pass
        bus.subscribe("evt", regular)

        # also add a dead weak ref to trigger None block in unsubscribe
        class Listener2:
            def on_event(self, **kw):
                pass
        obj2 = Listener2()
        bus.subscribe("evt", obj2.on_event, weak=True)
        del obj2
        gc.collect()

        bus.unsubscribe("evt", obj.on_event)

        # The first listener should be removed, the other two kept
        assert len(bus._subscribers["evt"]) == 2

    def test_weak_ref_plain_function_stored_strong(self):
        """Plain functions (no __self__) are stored as strong refs."""
        bus = EventBus()
        received = []

        def handler(**kw):
            received.append(1)

        bus.subscribe("evt", handler, weak=True)
        bus.emit("evt")
        assert received == [1]

class TestGlobalEventBus:
    def test_get_event_bus(self):
        from plugin.framework.event_bus import get_event_bus
        import sys

        bus1 = get_event_bus()
        bus2 = get_event_bus()
        assert bus1 is bus2
        assert bus1 is sys._localwriter_event_bus

        # reset sys for test consistency
        del sys._localwriter_event_bus
        bus3 = get_event_bus()
        assert bus3 is not bus1
