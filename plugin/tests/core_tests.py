import traceback

from plugin.framework.document import DocumentCache
from plugin.framework.uno_helpers import get_desktop


def run_framework_tests(ctx, doc=None):
    """Entry point for testing the core framework functionality inside LibreOffice."""
    passed = 0
    failed = 0
    log = []

    def ok(msg):
        log.append("OK: " + msg)

    def fail(msg):
        log.append("FAIL: " + msg)

    try:
        log.append("Starting Framework Tests...")

        desktop = get_desktop(ctx)
        from com.sun.star.beans import PropertyValue
        hidden_prop = PropertyValue()
        hidden_prop.Name = "Hidden"
        hidden_prop.Value = True

        doc1 = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))
        doc2 = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))

        try:
            cache1 = DocumentCache.get(doc1)
            cache1_again = DocumentCache.get(doc1)
            if cache1 is cache1_again:
                passed += 1
                ok("DocumentCache returns same instance for same model")
            else:
                failed += 1
                fail("DocumentCache returned different instances for same model")

            cache2 = DocumentCache.get(doc2)
            if cache1 is not cache2:
                passed += 1
                ok("DocumentCache returns different instance for different model")
            else:
                failed += 1
                fail("DocumentCache returned same instance for different model")

            DocumentCache.invalidate_all()
            cache1_new = DocumentCache.get(doc1)
            if cache1 is not cache1_new:
                passed += 1
                ok("DocumentCache.invalidate_all clears the entire cache")
            else:
                failed += 1
                fail("DocumentCache.invalidate_all failed")
        except Exception as e:
            failed += 1
            fail(f"DocumentCache test failed: {e}")

        try:
            # EventBus test (callbacks are invoked with **data only, not event name)
            from plugin.framework.event_bus import EventBus
            events = EventBus()
            event_received = []
            def handler(**kwargs):
                event_received.append(kwargs)
            events.subscribe("test_event", handler)
            events.emit("test_event", data=123)
            if len(event_received) == 1 and event_received[0].get("data") == 123:
                passed += 1
                ok("EventBus subscribe and emit passed")
            else:
                failed += 1
                fail(f"EventBus failed: {event_received}")
        except Exception as e:
            failed += 1
            fail(f"EventBus test failed: {e}")

        try:
            # ServiceRegistry test
            from plugin.framework.service_registry import ServiceRegistry
            registry = ServiceRegistry()
            class DummyService:
                pass
            svc = DummyService()
            registry.register_instance("dummy", svc)
            if registry.get("dummy") is svc:
                passed += 1
                ok("ServiceRegistry register and get passed")
            else:
                failed += 1
                fail("ServiceRegistry failed")
        except Exception as e:
            failed += 1
            fail(f"ServiceRegistry test failed: {e}")

        finally:
            if doc1:
                doc1.close(True)
            if doc2:
                doc2.close(True)

    except Exception as e:
        failed += 1
        fail(f"Exception during framework tests setup: {e}\n{traceback.format_exc()}")

    return passed, failed, log
