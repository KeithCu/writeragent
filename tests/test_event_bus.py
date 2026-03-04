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
        bus.emit("evt")
        assert received == ["called"]  # not called again

    def test_weak_ref_plain_function_stored_strong(self):
        """Plain functions (no __self__) are stored as strong refs."""
        bus = EventBus()
        received = []

        def handler(**kw):
            received.append(1)

        bus.subscribe("evt", handler, weak=True)
        bus.emit("evt")
        assert received == [1]
