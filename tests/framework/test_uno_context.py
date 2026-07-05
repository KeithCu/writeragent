
import builtins
import sys
from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test
from unittest.mock import MagicMock, patch
from plugin.tests.testing_utils import setup_uno_mocks
from plugin.framework.uno_context import set_fallback_ctx, get_ctx
_test_doc1 = None
_test_doc2 = None
_test_ctx = None

@setup
def setup_framework_tests(ctx):
    global _test_doc1, _test_doc2, _test_ctx
    _test_ctx = ctx
    desktop = get_desktop(ctx)
    import uno
    hidden_prop = uno.createUnoStruct('com.sun.star.beans.PropertyValue', Name='Hidden', Value=True)
    _test_doc1 = desktop.loadComponentFromURL('private:factory/swriter', '_blank', 0, (hidden_prop,))
    _test_doc2 = desktop.loadComponentFromURL('private:factory/swriter', '_blank', 0, (hidden_prop,))
    assert ((_test_doc1 is not None) and (_test_doc2 is not None)), 'Could not create test documents'

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
    events.subscribe('test_event', handler)
    events.emit('test_event', data=123)
    assert (len(event_received) == 1), 'Handler not called exactly once'
    assert (event_received[0].get('data') == 123), f'EventBus failed, received: {event_received}'

@native_test
def test_service_registry():
    from plugin.framework.service import ServiceRegistry
    registry = ServiceRegistry()

    class DummyService():
        pass
    svc = DummyService()
    registry.register('dummy', svc)
    assert (registry.get('dummy') is svc), 'ServiceRegistry failed'
setup_uno_mocks()

def test_get_ctx_with_uno():
    mock_uno = MagicMock()
    mock_ctx = MagicMock()
    mock_uno.getComponentContext.return_value = mock_ctx
    # patch.dict RESTORES the previous sys.modules['uno'] (the session-wide mock installed by
    # setup_uno_mocks). The old pop('uno') left the whole run without a 'uno' module, so any
    # later test that imports uno lazily hit the real uno.py -> "No module named 'pyuno'"
    # (this is what broke tests/mcp/test_long_running_concurrency.py in combined runs).
    with patch.dict(sys.modules, {'uno': mock_uno}):
        assert (get_ctx() == mock_ctx)
        mock_uno.getComponentContext.assert_called_once()

def test_get_ctx_fallback():
    mock_fallback = MagicMock()
    set_fallback_ctx(mock_fallback)
    orig_import = builtins.__import__

    def failing_import(name, globals=None, locals=None, fromlist=(), level=0):
        if (name == 'uno'):
            raise ImportError('simulated missing uno')
        return orig_import(name, globals, locals, fromlist, level)
    try:
        with patch.object(builtins, '__import__', failing_import):
            assert (get_ctx() == mock_fallback)
    finally:
        set_fallback_ctx(None)

def test_get_ctx_fallback_uno_returns_none():
    mock_uno = MagicMock()
    mock_uno.getComponentContext.return_value = None
    mock_fallback = MagicMock()
    # patch.dict restores the session-wide uno mock afterwards (see test_get_ctx_with_uno).
    with patch.dict(sys.modules, {'uno': mock_uno}):
        try:
            set_fallback_ctx(mock_fallback)
            assert (get_ctx() == mock_fallback)
        finally:
            set_fallback_ctx(None)


def test_focus_preserved_restores_focus_window():
    from contextlib import contextmanager

    from plugin.framework.uno_context import focus_preserved

    focus_window = MagicMock()
    toolkit = MagicMock()
    toolkit.getFocusWindow.return_value = focus_window

    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        with focus_preserved(MagicMock()):
            pass

    focus_window.setFocus.assert_called_once()


def test_process_events_to_idle_calls_toolkit():
    from plugin.framework.uno_context import process_events_to_idle

    toolkit = MagicMock()
    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        process_events_to_idle(MagicMock(), rounds=3)

    assert toolkit.processEventsToIdle.call_count == 3
