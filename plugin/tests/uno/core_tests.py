from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test


_test_doc1 = None
_test_doc2 = None
_test_ctx = None


@setup
def setup_framework_tests(ctx):
    global _test_doc1, _test_doc2, _test_ctx
    _test_ctx = ctx

    desktop = get_desktop(ctx)
    from com.sun.star.beans import PropertyValue
    hidden_prop = PropertyValue()
    hidden_prop.Name = "Hidden"
    hidden_prop.Value = True

    _test_doc1 = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))
    _test_doc2 = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))
    assert _test_doc1 is not None and _test_doc2 is not None, "Could not create test documents"


@teardown
def teardown_framework_tests(ctx):
    global _test_doc1, _test_doc2, _test_ctx
    if _test_doc1:
        _test_doc1.close(True)
    if _test_doc2:
        _test_doc2.close(True)
    _test_doc1 = None
    _test_doc2 = None
    _test_ctx = None


@native_test
def test_event_bus():
    from plugin.framework.event_bus import EventBus
    events = EventBus()
    event_received = []

    def handler(**kwargs):
        event_received.append(kwargs)

    events.subscribe("test_event", handler)
    events.emit("test_event", data=123)

    assert len(event_received) == 1, "Handler not called exactly once"
    assert event_received[0].get("data") == 123, f"EventBus failed, received: {event_received}"


@native_test
def test_service_registry():
    from plugin.framework.service_registry import ServiceRegistry
    registry = ServiceRegistry()

    class DummyService:
        pass

    svc = DummyService()
    registry.register("dummy", svc)

    assert registry.get("dummy") is svc, "ServiceRegistry failed"
