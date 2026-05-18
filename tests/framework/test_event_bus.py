import gc
from plugin.framework.event_bus import EventBus, get_event_bus
from plugin.framework.event_bus import EventBusService

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

def test_event_bus_service():
    service = EventBusService()
    assert service.name == "events"
    assert hasattr(service, "subscribe")
    assert hasattr(service, "emit")

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
