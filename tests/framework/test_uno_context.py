
import builtins
import sys
import threading
import time
from plugin.testing_runner import native_test
from unittest.mock import MagicMock, patch

import pytest

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


def test_get_ctx_with_uno():
    mock_uno = MagicMock()
    mock_ctx = MagicMock()
    mock_uno.getComponentContext.return_value = mock_ctx
    # patch.dict RESTORES the previous sys.modules['uno'] (the session-wide mock installed by
    # tests/conftest.py). The old pop('uno') left the whole run without a 'uno' module, so any
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


def test_clear_default_focus_restore_if_keeps_another_panels_query():
    from plugin.framework.uno_context import clear_default_focus_restore_if, set_default_focus_restore
    from plugin.framework import uno_context as uc

    mine = MagicMock()
    other = MagicMock()
    set_default_focus_restore(other)
    try:
        clear_default_focus_restore_if(mine)
        assert uc._default_focus_restore is other
        clear_default_focus_restore_if(other)
        assert uc._default_focus_restore is None
    finally:
        set_default_focus_restore(None)


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


def test_wait_while_pumping_returns_true_when_event_set():
    from plugin.framework.uno_context import wait_while_pumping

    done = threading.Event()
    pumps: list[bool] = []

    def _pe2i(_ctx: object, rounds: int = 1, force: bool = False) -> bool:
        del rounds
        pumps.append(force)
        done.set()
        return True

    with patch("plugin.framework.uno_context.process_events_to_idle", side_effect=_pe2i):
        assert wait_while_pumping(done, MagicMock(), timeout=1.0) is True
    assert pumps
    assert all(force is False for force in pumps)


def test_wait_while_pumping_timeout_returns_false():
    from plugin.framework.uno_context import wait_while_pumping

    done = threading.Event()
    with patch("plugin.framework.uno_context.process_events_to_idle", return_value=False):
        assert wait_while_pumping(done, MagicMock(), timeout=0.05, poll_sec=0.01) is False
    assert not done.is_set()


def test_wait_while_pumping_swallows_pe2i_errors():
    from plugin.framework.uno_context import wait_while_pumping

    done = threading.Event()
    n = {"i": 0}

    def _pe2i(_ctx: object, rounds: int = 1, force: bool = False) -> bool:
        del rounds, force
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("no toolkit")
        done.set()
        return True

    with patch("plugin.framework.uno_context.process_events_to_idle", side_effect=_pe2i):
        assert wait_while_pumping(done, MagicMock(), timeout=1.0) is True


def test_wait_while_pumping_under_drain_owner_still_waits():
    from plugin.framework.queue_executor import drain_owner_scope, reset_suppressed_vcl_pump_count
    from plugin.framework.uno_context import wait_while_pumping

    reset_suppressed_vcl_pump_count()
    done = threading.Event()
    toolkit = MagicMock()

    def _set_done() -> None:
        time.sleep(0.02)
        done.set()

    worker = threading.Thread(target=_set_done)
    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        with drain_owner_scope("stream"):
            worker.start()
            assert wait_while_pumping(done, MagicMock(), timeout=1.0, poll_sec=0.01) is True
            worker.join()
    toolkit.processEventsToIdle.assert_not_called()


def test_wait_while_pumping_off_main_posts_instead_of_pe2i():
    """Writer doProofreading is Dummy-*; PE2I on that stack is a thread violation."""
    from plugin.framework.uno_context import wait_while_pumping

    done = threading.Event()
    posts: list[object] = []
    pe2i_threads: list[str] = []
    result: dict[str, bool] = {}

    def _pe2i(_ctx: object, rounds: int = 1, force: bool = False) -> bool:
        del rounds, force
        pe2i_threads.append(threading.current_thread().name)
        return True

    def _post(fn: object, *args: object, **kwargs: object) -> None:
        del args, kwargs
        posts.append(fn)
        done.set()

    def _waiter() -> None:
        with (
            patch("plugin.framework.uno_context.process_events_to_idle", side_effect=_pe2i),
            patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_post),
        ):
            result["ok"] = wait_while_pumping(done, MagicMock(), timeout=1.0, poll_sec=0.01)

    worker = threading.Thread(target=_waiter, name="Dummy-21")
    worker.start()
    worker.join(timeout=2.0)
    assert result.get("ok") is True
    assert posts
    assert pe2i_threads == []


def test_wait_while_pumping_off_main_post_fallback_skips_pe2i():
    """QueueExecutor.post can run the callback on the waiter; still no PE2I off-main."""
    from plugin.framework.uno_context import wait_while_pumping

    done = threading.Event()
    pe2i_threads: list[str] = []
    result: dict[str, bool] = {}

    def _pe2i(_ctx: object, rounds: int = 1, force: bool = False) -> bool:
        del rounds, force
        pe2i_threads.append(threading.current_thread().name)
        return True

    def _post(fn: object, *args: object, **kwargs: object) -> None:
        del args, kwargs
        fn()  # type: ignore[operator]
        done.set()

    def _waiter() -> None:
        with (
            patch("plugin.framework.uno_context.process_events_to_idle", side_effect=_pe2i),
            patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_post),
        ):
            result["ok"] = wait_while_pumping(done, MagicMock(), timeout=1.0, poll_sec=0.01)

    worker = threading.Thread(target=_waiter, name="Dummy-21")
    worker.start()
    worker.join(timeout=2.0)
    assert result.get("ok") is True
    assert pe2i_threads == []


def test_wait_while_pumping_skips_post_while_work_queue_nonempty():
    """One queued pump is enough; a later empty queue must post again (not a sticky flag)."""
    import queue as queue_mod

    from plugin.framework.queue_executor import default_executor
    from plugin.framework.uno_context import wait_while_pumping

    saved: list[object] = []
    while True:
        try:
            saved.append(default_executor._work_queue.get_nowait())
        except queue_mod.Empty:
            break
    default_executor._work_queue.put(object())
    posts: list[int] = []
    result: dict[str, bool] = {}
    try:
        done = threading.Event()

        def _post_ignored(fn: object, *args: object, **kwargs: object) -> None:
            del fn, args, kwargs
            posts.append(1)
            done.set()

        def _waiter() -> None:
            with patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_post_ignored):
                result["skipped"] = wait_while_pumping(done, MagicMock(), timeout=0.04, poll_sec=0.01)

        worker = threading.Thread(target=_waiter, name="Dummy-21")
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert result.get("skipped") is False
        assert posts == []

        default_executor._work_queue.get_nowait()
        done2 = threading.Event()

        def _post_once(fn: object, *args: object, **kwargs: object) -> None:
            del fn, args, kwargs
            posts.append(1)
            done2.set()

        def _waiter2() -> None:
            with patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_post_once):
                result["posted"] = wait_while_pumping(done2, MagicMock(), timeout=1.0, poll_sec=0.01)

        worker2 = threading.Thread(target=_waiter2, name="Dummy-22")
        worker2.start()
        worker2.join(timeout=2)
        assert result.get("posted") is True
        assert posts == [1]
    finally:
        while True:
            try:
                default_executor._work_queue.get_nowait()
            except queue_mod.Empty:
                break
        for item in saved:
            default_executor._work_queue.put(item)


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
        patch("plugin.framework.thread_guard.guard_uno", side_effect=lambda obj: obj),
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


def test_get_active_document_reraises_disposed():
    """A document that dies mid-call must not look like nothing is open."""
    from plugin.framework.errors import DocumentDisposedError
    from plugin.framework.uno_context import get_active_document

    class DisposedException(Exception):
        pass

    desktop = MagicMock()
    desktop.getCurrentComponent.side_effect = DisposedException("gone")
    with (
        patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
        pytest.raises(DocumentDisposedError),
    ):
        get_active_document(MagicMock())


def test_get_active_document_none_when_component_missing():
    from plugin.framework.uno_context import get_active_document

    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = None
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        assert get_active_document(MagicMock()) is None


def test_get_active_document_none_on_other_uno_object_error():
    from plugin.framework.uno_context import get_active_document

    desktop = MagicMock()
    desktop.getCurrentComponent.side_effect = RuntimeError("not disposed")
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        assert get_active_document(MagicMock()) is None


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
    saved_query_listener = uc._query_focus_listener
    uc._stream_focus_trackers[:] = [object()]
    uc._query_focus_listener = object()
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
        uc._query_focus_listener = saved_query_listener


def test_install_click_handler_follows_each_document_controller():
    """A later document must get its own page-click handler; disposing removes it."""
    import types

    from plugin.framework import uno_context as uc

    class _Base:
        pass

    class XFocusListener:
        pass

    class XMouseClickHandler:
        pass

    class XMouseListener:
        pass

    class _Controller:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.removed: list[object] = []

        def addMouseClickHandler(self, handler: object) -> None:
            self.added.append(handler)

        def removeMouseClickHandler(self, handler: object) -> None:
            self.removed.append(handler)

    first = _Controller()
    second = _Controller()
    saved_trackers = list(uc._stream_focus_trackers)
    saved_bindings = list(uc._doc_click_bindings)
    saved_query_listener = uc._query_focus_listener
    uc._stream_focus_trackers.clear()
    uc._doc_click_bindings.clear()
    uc._query_focus_listener = None
    fake_awt = types.SimpleNamespace(
        XFocusListener=XFocusListener,
        XMouseListener=XMouseListener,
        XMouseClickHandler=XMouseClickHandler,
    )
    controllers = iter([first, second, second])
    try:
        with (
            patch.dict(
                sys.modules,
                {"unohelper": types.SimpleNamespace(Base=_Base), "com.sun.star.awt": fake_awt},
            ),
            patch.object(uc, "_current_document_controller", side_effect=lambda ctx: next(controllers)),
        ):
            uc.install_stream_focus_tracker(MagicMock(), query=MagicMock())
            uc.install_stream_focus_tracker(MagicMock(), query=MagicMock())
            uc.install_stream_focus_tracker(MagicMock(), query=MagicMock())
        assert len(first.added) == 1
        assert len(second.added) == 1
        handler = second.added[0]
        handler.disposing(None)
        assert second.removed == [handler]
        assert all(pair[1] is not handler for pair in uc._doc_click_bindings)
    finally:
        uc._stream_focus_trackers[:] = saved_trackers
        uc._doc_click_bindings[:] = saved_bindings
        uc._query_focus_listener = saved_query_listener


def test_disposed_query_listener_lets_the_next_sidebar_attach():
    """disposing() must clear the query listener so a reopened sidebar hears focusGained."""
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

    saved_trackers = list(uc._stream_focus_trackers)
    saved_query_listener = uc._query_focus_listener
    uc._stream_focus_trackers.clear()
    uc._query_focus_listener = None
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
            first = MagicMock()
            uc.install_stream_focus_tracker(MagicMock(), query=first)
            listener = first.addFocusListener.call_args[0][0]
            listener.disposing(None)
            assert uc._query_focus_listener is None
            second = MagicMock()
            uc.install_stream_focus_tracker(MagicMock(), query=second)
        second.addFocusListener.assert_called_once()
        second.addFocusListener.call_args[0][0].focusGained(None)
    finally:
        uc._stream_focus_trackers[:] = saved_trackers
        uc._query_focus_listener = saved_query_listener


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
    from tests.harness.strip_bundle import skip_if_release_build

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
    from tests.harness.strip_bundle import skip_if_release_build

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


def test_uno_same_off_thread_raises_on_thread_issame_still_works(monkeypatch):
    from plugin.framework import thread_guard as tg
    from plugin.framework.uno_context import uno_same
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("release bundles stub main_thread_only")
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    a, b = _NeverEq(), _NeverEq()
    with patch.object(sys.modules["uno"], "isSame", return_value=True, create=True):
        assert uno_same(a, b) is True

    was = tg.GUARD_ON
    tg.GUARD_ON = True
    holder: dict[str, BaseException] = {}

    def _call() -> None:
        try:
            uno_same(object(), object())
        except BaseException as exc:
            holder["exc"] = exc

    try:
        worker = threading.Thread(target=_call, name="bg-uno-same")
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        exc = holder.get("exc")
        assert isinstance(exc, RuntimeError)
        assert "UNO thread violation" in str(exc)
    finally:
        tg.GUARD_ON = was


def test_uno_boundaries_import_guard_uno_at_the_return():
    """Patching thread_guard.guard_uno must see boundary returns (not import-time _wrap_uno)."""
    from plugin.doc.doc_type import DocumentType
    from plugin.framework import uno_context as uc

    seen: list[object] = []

    def _guard(obj: object) -> object:
        seen.append(obj)
        return obj

    ctx = MagicMock(name="ctx")
    desktop = MagicMock(name="desktop")
    doc = MagicMock(name="doc")
    pip = MagicMock(name="pip")
    toolkit = MagicMock(name="tk")
    model = MagicMock(name="model")
    model.getURL.return_value = "file:///tmp/note.odt"
    smgr = MagicMock()
    smgr.createInstanceWithContext.side_effect = lambda name, _ctx: toolkit if "Toolkit" in name else desktop
    ctx.ServiceManager = smgr
    ctx.getValueByName.return_value = pip
    desktop.getCurrentComponent.return_value = doc
    enum = MagicMock()
    enum.hasMoreElements.side_effect = [True, False]
    enum.nextElement.return_value = model
    comps = MagicMock()
    comps.createEnumeration.return_value = enum
    desktop.getComponents.return_value = comps
    frame_model = MagicMock(name="frame_model")
    controller = MagicMock()
    controller.getModel.return_value = frame_model
    frame = MagicMock()
    frame.getController.return_value = controller

    saved = uc._fallback_ctx
    try:
        uc.set_fallback_ctx(ctx)
        with (
            patch("plugin.framework.thread_guard.guard_uno", side_effect=_guard),
            patch.object(sys, "argv", ["soffice"]),
            patch("plugin.framework.uno_context._linux_process_tokens", return_value=["soffice.bin"]),
            patch("plugin.doc.doc_type.get_document_type", return_value=DocumentType.WRITER),
        ):
            assert uc.get_ctx() is ctx
            assert uc.get_desktop(ctx) is desktop
            assert uc.get_active_document(ctx) is doc
            assert uc.get_package_info(ctx) is pip
            assert uc.get_toolkit(ctx) is toolkit
            resolved, label = uc.resolve_document_by_url(ctx, "file:///tmp/note.odt")
            assert resolved is model
            assert label == "writer"
            assert uc.get_document_from_frame(frame) is frame_model
    finally:
        uc.set_fallback_ctx(saved)
    for obj in (ctx, desktop, doc, pip, toolkit, model, frame_model):
        assert obj in seen


def _scratch_doc(text="", tables=(), frames=(), shapes=0):
    """A MagicMock Writer with real-looking containers (a bare MagicMock never empties)."""
    from unittest.mock import MagicMock

    doc = MagicMock()
    body = MagicMock()
    body.getString.return_value = text
    doc.getText.return_value = body

    def _container(names):
        c = MagicMock()
        items = {n: MagicMock() for n in names}
        c.getElementNames.return_value = list(items)
        c.hasByName.side_effect = lambda n: n in items
        c.getByName.side_effect = lambda n: items[n]
        return c, items

    doc.getTextTables.return_value, tbls = _container(tables)
    doc.getTextFrames.return_value, frms = _container(frames)
    page = MagicMock()
    count = [shapes]
    page.getCount.side_effect = lambda: count[0]
    page.remove.side_effect = lambda _s: count.__setitem__(0, count[0] - 1)
    doc.getDrawPage.return_value = page
    return doc, body, tbls, frms, count


def test_clear_writer_body_empties_a_default_template_scratch_doc():
    """A scratch Writer must not carry the user's default template.

    Regression for the "ghost block": a firm whose default template is its
    petition model got that model's header glued into range reads and
    plain-text conversions, because the factory URL honours the template and
    the scratch body was never emptied.
    """
    from plugin.framework.uno_context import clear_writer_body

    doc, body, _, _, _ = _scratch_doc(text="AO DOUTO JUIZO DO XXXX\nParte autora: xxxxx")
    assert clear_writer_body(doc) is True
    body.setString.assert_called_with("")


def test_clear_writer_body_drops_a_letterhead_with_no_text():
    """An empty table and a page-anchored logo have an empty body string; testing the
    text alone left the table in the scratch doc and it came back in range reads."""
    from plugin.framework.uno_context import clear_writer_body

    doc, _, tbls, frms, count = _scratch_doc(text="", tables=("Table1",), frames=("Frame1",), shapes=2)
    assert clear_writer_body(doc) is True
    tbls["Table1"].dispose.assert_called_once()
    frms["Frame1"].dispose.assert_called_once()
    assert count[0] == 0


def test_clear_writer_body_leaves_an_already_empty_doc_alone():
    from plugin.framework.uno_context import clear_writer_body

    for empty in ("", "   \n  "):
        doc, _, _, _, _ = _scratch_doc(text=empty)
        assert clear_writer_body(doc) is False


def test_clear_writer_body_cannot_spin_on_a_page_that_never_empties():
    """A remove that silently fails must not loop forever on the main thread."""
    from unittest.mock import MagicMock

    from plugin.framework.uno_context import clear_writer_body

    doc, _, _, _, _ = _scratch_doc()
    stuck = MagicMock()
    stuck.getCount.return_value = 3  # remove() never changes it
    doc.getDrawPage.return_value = stuck
    clear_writer_body(doc)
    assert stuck.remove.call_count == 1


def test_clear_writer_body_reraises_disposal():
    """A disposed scratch doc must not look like an empty one."""
    from unittest.mock import MagicMock

    from plugin.framework.errors import DocumentDisposedError
    from plugin.framework.uno_context import clear_writer_body

    class DisposedException(Exception):
        pass

    doc = MagicMock()
    doc.getTextTables.side_effect = DisposedException("dead")
    with pytest.raises(DocumentDisposedError):
        clear_writer_body(doc)


def test_clear_writer_body_survives_a_hostile_doc():
    """Never let a scratch-buffer cleanup take down the caller."""
    from unittest.mock import MagicMock

    from plugin.framework.uno_context import clear_writer_body

    doc = MagicMock()
    doc.getText.side_effect = RuntimeError("disposed")
    assert clear_writer_body(doc) is False
    assert clear_writer_body(None) is False


def test_doc_identity_url_repairs_file_slash_without_changing_normalize():
    from plugin.framework.uno_context import _doc_identity_url, normalize_doc_url

    assert normalize_doc_url("file:/tmp/note.odt") == "file:/tmp/note.odt"
    assert _doc_identity_url("file:/tmp/note.odt") == _doc_identity_url("file:///tmp/note.odt")
