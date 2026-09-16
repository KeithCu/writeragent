
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


def test_note_user_left_query_skips_restore():
    """Packet B1: hovering/clicking Stop must stop query.setFocus on stream chunks."""
    from plugin.framework import uno_context as uc

    query = MagicMock()
    uc.set_default_focus_restore(query)
    uc.note_user_wants_query()
    try:
        uc.note_user_left_query()
        uc.restore_query_if_user_still_there()
        query.setFocus.assert_not_called()
        uc.note_user_wants_query()
        uc.restore_query_if_user_still_there()
        query.setFocus.assert_called_once()
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


def test_resolve_package_extension_id_off_main_skips_package_info(monkeypatch):
    from plugin.framework.constants import EXTENSION_ID_WRITERAGENT
    from plugin.framework.uno_context import (
        reset_package_extension_id_for_tests,
        resolve_package_extension_id,
    )

    reset_package_extension_id_for_tests()
    monkeypatch.setattr("plugin.framework.uno_context.on_main_thread", lambda: False)
    with patch("plugin.framework.uno_context.get_package_info") as pip:
        assert resolve_package_extension_id() == EXTENSION_ID_WRITERAGENT
        pip.assert_not_called()
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


def test_get_desktop_skips_create_on_uno_bin_helper():
    """Register/enable uno.bin must not createInstance(Desktop) (#768)."""
    from plugin.framework.uno_context import get_desktop

    smgr = MagicMock()
    ctx = MagicMock()
    ctx.ServiceManager = smgr
    with patch.object(sys, "argv", ["/usr/lib64/libreoffice/program/uno.bin", "--singleaccept"]):
        assert get_desktop(ctx) is None
    smgr.createInstanceWithContext.assert_not_called()


def test_get_desktop_skips_create_when_proc_exe_is_uno_bin():
    """pythonloader may rewrite sys.argv; /proc/self/exe is the real process (#768)."""
    from plugin.framework.uno_context import get_desktop

    smgr = MagicMock()
    ctx = MagicMock()
    ctx.ServiceManager = smgr
    proc = ["/usr/lib64/libreoffice/program/uno.bin", "--quiet", "--singleaccept"]
    with (
        patch.object(sys, "argv", [""]),
        patch("plugin.framework.uno_context._linux_process_tokens", return_value=proc),
    ):
        assert get_desktop(ctx) is None
    smgr.createInstanceWithContext.assert_not_called()


def test_get_desktop_creates_on_soffice():
    from plugin.framework.uno_context import get_desktop

    desktop = MagicMock()
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = desktop
    ctx = MagicMock()
    ctx.ServiceManager = smgr
    with (
        patch.object(sys, "argv", ["soffice"]),
        patch("plugin.framework.uno_context._linux_process_tokens", return_value=["/usr/lib64/libreoffice/program/soffice.bin"]),
        patch("plugin.framework.uno_context._wrap_uno", side_effect=lambda obj: obj),
    ):
        assert get_desktop(ctx) is desktop
    smgr.createInstanceWithContext.assert_called_once_with("com.sun.star.frame.Desktop", ctx)


def test_get_active_document_skips_desktop_create_on_no_vcl():
    from plugin.framework.uno_context import get_active_document

    smgr = MagicMock()
    ctx = MagicMock()
    ctx.ServiceManager = smgr
    with patch.object(sys, "argv", ["/usr/lib64/libreoffice/program/uno.bin", "--singleaccept"]):
        assert get_active_document(ctx) is None
    smgr.createInstanceWithContext.assert_not_called()


def test_current_document_controller_skips_desktop_create_on_no_vcl():
    from plugin.framework.uno_context import _current_document_controller

    smgr = MagicMock()
    ctx = MagicMock()
    ctx.ServiceManager = smgr
    with patch.object(sys, "argv", ["/usr/lib64/libreoffice/program/uno.bin", "--singleaccept"]):
        assert _current_document_controller(ctx) is None
    smgr.createInstanceWithContext.assert_not_called()


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


def test_attach_leave_query_listeners_adds_mouse_and_focus():
    """Sidebar Stop/Clear must get mouse listeners so stream restore does not steal the click."""
    import types

    from plugin.framework import uno_context as uc

    class _Base:
        pass

    class XFocusListener:
        pass

    class XMouseListener:
        pass

    control = MagicMock()
    saved = list(uc._stream_focus_trackers)
    uc._stream_focus_trackers.clear()
    fake_awt = types.SimpleNamespace(XFocusListener=XFocusListener, XMouseListener=XMouseListener)
    try:
        with patch.dict(
            sys.modules,
            {"unohelper": types.SimpleNamespace(Base=_Base), "com.sun.star.awt": fake_awt},
        ):
            uc._attach_leave_query_listeners(control)
        control.addMouseListener.assert_called_once()
        control.addFocusListener.assert_called_once()
        assert len(uc._stream_focus_trackers) == 2
    finally:
        uc._stream_focus_trackers[:] = saved


def test_install_attaches_leave_controls_when_trackers_already_exist():
    """Second sidebar must still get Stop/Clear leave listeners (global tracker early-return)."""
    import types

    from plugin.framework import uno_context as uc

    class _Base:
        pass

    class XFocusListener:
        pass

    class XMouseListener:
        pass

    class XMouseClickHandler:
        pass

    stop = MagicMock()
    query = MagicMock()
    saved = list(uc._stream_focus_trackers)
    uc._stream_focus_trackers[:] = [object()]
    fake_awt = types.SimpleNamespace(
        XFocusListener=XFocusListener,
        XMouseListener=XMouseListener,
        XMouseClickHandler=XMouseClickHandler,
    )
    try:
        with patch.dict(
            sys.modules,
            {"unohelper": types.SimpleNamespace(Base=_Base), "com.sun.star.awt": fake_awt},
        ):
            uc.install_stream_focus_tracker(
                MagicMock(),
                query=query,
                leave_query_controls=(stop,),
            )
        stop.addMouseListener.assert_called_once()
        stop.addFocusListener.assert_called_once()
        query.addFocusListener.assert_not_called()
    finally:
        uc._stream_focus_trackers[:] = saved


# ---- uno_same --------------------------------------------------------------


class _NeverEq:
    """Two instances compare unequal so the helper must fall through ``is`` / ``==``."""

    def __eq__(self, other: object) -> bool:
        return False


def test_uno_same_identity():
    from plugin.framework.uno_context import uno_same

    obj = object()
    assert uno_same(obj, obj) is True
    assert uno_same(None, None) is True
    assert uno_same(None, object()) is False


def test_uno_same_eq_when_not_same_ref():
    from plugin.framework.uno_context import uno_same

    class AlwaysEq:
        def __eq__(self, other: object) -> bool:
            return True

        def __hash__(self) -> int:
            return 0

    assert uno_same(AlwaysEq(), AlwaysEq()) is True


def test_uno_same_issame_when_is_and_eq_fail():
    from plugin.framework.uno_context import uno_same

    a, b = _NeverEq(), _NeverEq()
    with patch.object(sys.modules["uno"], "isSame", return_value=True, create=True):
        assert uno_same(a, b) is True


def test_uno_same_false_when_all_paths_differ():
    from plugin.framework.uno_context import uno_same

    a, b = _NeverEq(), _NeverEq()
    with patch.object(sys.modules["uno"], "isSame", return_value=False, create=True):
        assert uno_same(a, b) is False


def test_uno_same_false_when_issame_missing():
    from plugin.framework.uno_context import uno_same

    a, b = _NeverEq(), _NeverEq()
    with patch.object(sys.modules["uno"], "isSame", None, create=True):
        assert uno_same(a, b) is False


def test_uno_same_mocked_issame_is_not_treated_as_true():
    """A session-wide MagicMock ``uno.isSame`` is truthy; must not collapse every pair to same."""
    from plugin.framework.uno_context import uno_same

    a, b = _NeverEq(), _NeverEq()
    with patch.object(sys.modules["uno"], "isSame", MagicMock(), create=True):
        assert uno_same(a, b) is False


def test_uno_same_proxy_eq_unwraps_target():
    """GUARD_ON proxy ``__eq__`` unwraps ``_target`` so proxy↔unwrapped is same (step 2)."""
    from plugin.framework import thread_guard as tg
    from plugin.framework.uno_context import uno_same
    from tests.strip_bundle import skip_if_release_build

    skip_if_release_build("GUARD_ON thread guard proxy stripped in release bundle")
    real = object()
    proxy = tg._UnoThreadGuardProxy(real)
    assert proxy is not real
    assert uno_same(proxy, real) is True
    assert uno_same(real, proxy) is True


def test_uno_same_issame_unwraps_proxy_first():
    """``uno.isSame`` must see the real PyUNO target, not the viral proxy wrapper."""
    from plugin.framework import thread_guard as tg
    from plugin.framework.uno_context import uno_same
    from tests.strip_bundle import skip_if_release_build

    skip_if_release_build("GUARD_ON thread guard proxy stripped in release bundle")
    real_a, real_b = object(), object()
    proxy_a = tg._UnoThreadGuardProxy(real_a)
    seen: list[tuple[object, object]] = []

    def _issame(left: object, right: object) -> bool:
        seen.append((left, right))
        return left is real_a and right is real_b

    with patch.object(sys.modules["uno"], "isSame", _issame, create=True):
        # ``==`` is False (distinct objects / proxy target ≠ other), so ladder hits isSame.
        assert uno_same(proxy_a, real_b) is True
    assert seen == [(real_a, real_b)]
