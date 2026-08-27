
import builtins
import sys
from plugin.testing_runner import native_test
from unittest.mock import MagicMock, patch
from plugin.tests.testing_utils import setup_uno_mocks
from plugin.framework.uno_context import set_fallback_ctx, get_ctx
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


def test_uno_module_restored_after_get_ctx_with_uno():
    """B9 regression: patch.dict must restore the session-wide uno mock for later tests."""
    test_get_ctx_with_uno()
    assert "uno" in sys.modules


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

    from plugin.framework.uno_context import focus_preserved, set_default_focus_restore

    set_default_focus_restore(None)
    focus_window = MagicMock()
    toolkit = MagicMock()
    toolkit.getFocusWindow.return_value = focus_window

    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        with focus_preserved(MagicMock()):
            pass

    focus_window.setFocus.assert_called_once()


def test_focus_preserved_prefers_pinned_query_over_toolkit():
    from plugin.framework.uno_context import focus_preserved, set_default_focus_restore

    query = MagicMock()
    send_btn = MagicMock()
    toolkit = MagicMock()
    toolkit.getFocusWindow.return_value = send_btn
    set_default_focus_restore(query)
    try:
        with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
            with focus_preserved(MagicMock()):
                pass
    finally:
        set_default_focus_restore(None)

    query.setFocus.assert_called_once()
    send_btn.setFocus.assert_not_called()


def test_restore_query_if_user_still_there():
    from plugin.framework import uno_context as uc

    query = MagicMock()
    uc.set_default_focus_restore(query)
    uc.note_user_wants_query()
    try:
        uc.restore_query_if_user_still_there()
        query.setFocus.assert_called_once()
        query.reset_mock()
        uc._restore_query_after_scroll = False
        uc.restore_query_if_user_still_there()
        query.setFocus.assert_not_called()
    finally:
        uc.set_default_focus_restore(None)
        uc._restore_query_after_scroll = True


def test_process_events_to_idle_calls_toolkit():
    from plugin.framework.uno_context import process_events_to_idle
    from plugin.framework.queue_executor import reset_suppressed_vcl_pump_count

    reset_suppressed_vcl_pump_count()
    toolkit = MagicMock()
    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        assert process_events_to_idle(MagicMock(), rounds=3) is True

    assert toolkit.processEventsToIdle.call_count == 3


def test_process_events_to_idle_suppressed_under_drain_owner():
    from plugin.framework.queue_executor import (
        drain_owner_scope,
        get_suppressed_vcl_pump_count,
        reset_suppressed_vcl_pump_count,
    )
    from plugin.framework.uno_context import process_events_to_idle

    reset_suppressed_vcl_pump_count()
    toolkit = MagicMock()
    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        with drain_owner_scope("stream"):
            assert process_events_to_idle(MagicMock(), rounds=2) is False

    assert toolkit.processEventsToIdle.call_count == 0
    assert get_suppressed_vcl_pump_count() >= 1


def test_process_events_to_idle_force_under_drain_owner():
    from plugin.framework.queue_executor import drain_owner_scope, reset_suppressed_vcl_pump_count
    from plugin.framework.uno_context import process_events_to_idle

    reset_suppressed_vcl_pump_count()
    toolkit = MagicMock()
    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        with drain_owner_scope("stream"):
            assert process_events_to_idle(MagicMock(), rounds=2, force=True) is True

    assert toolkit.processEventsToIdle.call_count == 2


def test_resolve_package_extension_id_prefers_librepy():
    from plugin.framework.constants import EXTENSION_ID_LIBREPY
    from plugin.framework.uno_context import (
        get_extension_url,
        reset_package_extension_id_for_tests,
        resolve_package_extension_id,
    )

    reset_package_extension_id_for_tests()
    pip = MagicMock()
    pip.getPackageLocation.side_effect = lambda eid: (
        "file:///tmp/LibrePy.oxt" if eid == EXTENSION_ID_LIBREPY else ""
    )
    with patch("plugin.framework.uno_context.get_package_info", return_value=pip):
        assert resolve_package_extension_id() == EXTENSION_ID_LIBREPY
        assert get_extension_url() == "file:///tmp/LibrePy.oxt"
    reset_package_extension_id_for_tests()


def test_set_package_extension_id_override():
    from plugin.framework.constants import EXTENSION_ID_LIBREPY
    from plugin.framework.uno_context import (
        reset_package_extension_id_for_tests,
        resolve_package_extension_id,
        set_package_extension_id,
    )

    reset_package_extension_id_for_tests()
    set_package_extension_id(EXTENSION_ID_LIBREPY)
    assert resolve_package_extension_id() == EXTENSION_ID_LIBREPY
    reset_package_extension_id_for_tests()


def test_product_display_name_follows_extension_id():
    from plugin.framework.constants import EXTENSION_ID_LIBREPY, EXTENSION_ID_WRITERAGENT
    from plugin.framework.uno_context import (
        product_display_name,
        reset_package_extension_id_for_tests,
        set_package_extension_id,
    )

    reset_package_extension_id_for_tests()
    set_package_extension_id(EXTENSION_ID_LIBREPY)
    try:
        assert product_display_name() == "LibrePy"
    finally:
        reset_package_extension_id_for_tests()

    set_package_extension_id(EXTENSION_ID_WRITERAGENT)
    try:
        assert product_display_name() == "WriterAgent"
    finally:
        reset_package_extension_id_for_tests()


def test_extension_id_constants_match_package_ids():
    from plugin.framework.constants import (
        EXTENSION_ID_LIBREHARPER,
        EXTENSION_ID_LIBREPY,
        EXTENSION_ID_WRITERAGENT,
    )
    from plugin.framework.uno_context import _KNOWN_EXTENSION_IDS

    assert EXTENSION_ID_LIBREPY == "org.extension.librepy"
    assert EXTENSION_ID_WRITERAGENT == "org.extension.writeragent"
    assert EXTENSION_ID_LIBREHARPER == "org.extension.libreharper"
    assert _KNOWN_EXTENSION_IDS == (
        EXTENSION_ID_LIBREPY,
        EXTENSION_ID_WRITERAGENT,
        EXTENSION_ID_LIBREHARPER,
    )
