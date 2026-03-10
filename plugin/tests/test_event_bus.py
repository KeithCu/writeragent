import gc
from plugin.framework.event_bus import EventBus, get_event_bus
from plugin.framework.events import EventBusService

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
