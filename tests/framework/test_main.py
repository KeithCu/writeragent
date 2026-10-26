"""Unit tests for plugin.main: bootstrap lifecycle, dispatch, listeners, and icon caching."""

from __future__ import annotations

import logging
import os
import sys
import threading
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _ensure_main_import_stubs() -> None:
    task = sys.modules.get("com.sun.star.task")
    if task is None:
        task = types.ModuleType("com.sun.star.task")
        sys.modules["com.sun.star.task"] = task
    if not hasattr(task, "XJobExecutor"):
        task.XJobExecutor = type("XJobExecutor", (), {})
    if not hasattr(task, "XJob"):
        task.XJob = type("XJob", (), {})

    frame = sys.modules.get("com.sun.star.frame")
    if frame is None:
        frame = types.ModuleType("com.sun.star.frame")
        sys.modules["com.sun.star.frame"] = frame
    if not hasattr(frame, "DispatchDescriptor"):
        frame.DispatchDescriptor = type("DispatchDescriptor", (), {})
    if not hasattr(frame, "XDispatch"):
        frame.XDispatch = type("XDispatch", (), {})
    if not hasattr(frame, "XDispatchProvider"):
        frame.XDispatchProvider = type("XDispatchProvider", (), {})

    lang = sys.modules.get("com.sun.star.lang")
    if lang is not None:
        if not hasattr(lang, "XInitialization"):
            lang.XInitialization = type("XInitialization", (), {})
        if not hasattr(lang, "XServiceInfo"):
            lang.XServiceInfo = type("XServiceInfo", (), {})


def _patch_unohelper_implementation_helper() -> None:
    class _FakeImplHelper:
        def addImplementation(self, *args: Any, **kwargs: Any) -> None:
            pass

    uh = sys.modules.get("unohelper")
    if uh is not None:
        uh.ImplementationHelper = lambda: _FakeImplHelper()


_ensure_main_import_stubs()
_patch_unohelper_implementation_helper()

import plugin.main as main
from plugin.main import BootstrapState
from plugin.doc.doc_type import DocumentType
from plugin.framework.errors import WriterAgentException
from plugin.framework.event_bus import EventBus
from plugin.framework.main_shared import (
    _ACTION_HANDLERS,
    get_action_handler,
    register_action_handler,
    report_action_failure,
)


@pytest.fixture(autouse=True)
def _isolate_main_state():
    """Isolate global state in plugin.main across tests."""
    prev_state = main._bootstrap_state
    prev_services = main._services
    prev_tools = main._tools
    prev_modules = list(main._modules)
    prev_listeners = list(main._status_listeners)
    prev_err = getattr(main, "_bootstrap_error", None)
    prev_graphics = dict(main._loaded_icon_graphics)
    prev_stamp = main._icon_apply_stamp
    try:
        yield
    finally:
        main._bootstrap_state = prev_state
        main._services = prev_services
        main._tools = prev_tools
        main._modules[:] = prev_modules
        main._status_listeners[:] = prev_listeners
        main._bootstrap_error = prev_err
        main._loaded_icon_graphics.clear()
        main._loaded_icon_graphics.update(prev_graphics)
        main._icon_apply_stamp = prev_stamp


# --- Bug 1 tests: bootstrap error handling and retry ---


def test_bootstrap_same_thread_reentry_returns(monkeypatch):
    """Re-entrant bootstrap() call on the same thread returns without deadlocking."""
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    fake_ctx = MagicMock()
    monkeypatch.setattr(main, "get_ctx", lambda: fake_ctx)

    called_reentrant = False

    def mock_init_config(ctx=None):
        nonlocal called_reentrant
        if not called_reentrant:
            called_reentrant = True
            main.bootstrap(ctx)
        return ""

    monkeypatch.setattr("plugin.framework.config.init_config", mock_init_config)
    monkeypatch.setattr("plugin.framework.i18n.init_i18n", lambda ctx: None)
    monkeypatch.setattr("plugin.framework.main_shared.register_core_handlers", lambda: None)
    monkeypatch.setattr(main, "set_package_extension_id", lambda extension_id: None)
    monkeypatch.setattr("plugin.framework.module_base.ModuleLoader.load_modules", lambda services: [])

    main._bootstrap_state = BootstrapState.UNINITIALIZED
    main._modules = []
    main._bootstrap_error = None

    main.bootstrap(fake_ctx)
    assert called_reentrant is True
    assert main._bootstrap_state is BootstrapState.INITIALIZED


def test_bootstrap_load_modules_failure_resets_initialized(monkeypatch):
    """When load_modules raises, _bootstrap_state resets to UNINITIALIZED so bootstrap can be retried."""
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr(main, "get_ctx", lambda: MagicMock())
    monkeypatch.setattr("plugin.framework.config.init_config", lambda ctx=None: "")
    monkeypatch.setattr("plugin.framework.i18n.init_i18n", lambda ctx: None)
    monkeypatch.setattr("plugin.framework.main_shared.register_core_handlers", lambda: None)
    monkeypatch.setattr(main, "set_package_extension_id", lambda extension_id: None)

    main._bootstrap_state = BootstrapState.UNINITIALIZED
    main._modules = []
    main._bootstrap_error = None

    fail = True

    def mock_load_modules(services: Any) -> list[Any]:
        nonlocal fail
        if fail:
            raise RuntimeError("load_modules failed")
        return []

    monkeypatch.setattr("plugin.framework.module_base.ModuleLoader.load_modules", mock_load_modules)

    # First call: load_modules raises
    main.bootstrap(MagicMock())
    assert main._bootstrap_state is BootstrapState.UNINITIALIZED
    assert isinstance(main._bootstrap_error, RuntimeError)

    # Second call: retry succeeds
    fail = False
    main.bootstrap(MagicMock())
    assert main._bootstrap_state is BootstrapState.INITIALIZED
    assert main._bootstrap_error is None


def test_bootstrap_start_background_failure_keeps_initialized(monkeypatch):
    """When start_background raises after modules are loaded, _bootstrap_state stays INITIALIZED."""
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr(main, "get_ctx", lambda: MagicMock())
    monkeypatch.setattr("plugin.framework.config.init_config", lambda ctx=None: "")
    monkeypatch.setattr("plugin.framework.i18n.init_i18n", lambda ctx: None)
    core_handlers_mock = MagicMock()
    monkeypatch.setattr("plugin.framework.main_shared.register_core_handlers", core_handlers_mock)
    monkeypatch.setattr(main, "set_package_extension_id", lambda extension_id: None)

    main._bootstrap_state = BootstrapState.UNINITIALIZED
    main._modules = []
    main._bootstrap_error = None

    mock_mod = MagicMock()
    mock_mod.start_background.side_effect = RuntimeError("bg error")
    monkeypatch.setattr("plugin.framework.module_base.ModuleLoader.load_modules", lambda services: [mock_mod])

    main.bootstrap(MagicMock())
    assert main._bootstrap_state is BootstrapState.INITIALIZED
    assert isinstance(main._bootstrap_error, RuntimeError)
    core_handlers_mock.assert_called_once()

    # Next call returns early because _bootstrap_state is INITIALIZED
    with patch("plugin.framework.module_base.ModuleLoader.load_modules") as mock_load:
        main.bootstrap(MagicMock())
        mock_load.assert_not_called()


def test_bootstrap_start_background_failure_isolates_modules(monkeypatch):
    """When one module's start_background raises, later modules still start their background work."""
    monkeypatch.setattr("plugin.framework.thread_guard.on_python_main_thread", lambda: True)
    monkeypatch.setattr(main, "get_ctx", lambda: MagicMock())
    monkeypatch.setattr("plugin.framework.config.init_config", lambda ctx=None: "")
    monkeypatch.setattr("plugin.framework.i18n.init_i18n", lambda ctx: None)
    monkeypatch.setattr(main, "set_package_extension_id", lambda extension_id: None)
    core_handlers_mock = MagicMock()
    monkeypatch.setattr("plugin.framework.main_shared.register_core_handlers", core_handlers_mock)
    monkeypatch.setattr("plugin.framework.worker_pool.run_in_background", lambda fn: None)

    first_mod = MagicMock()
    first_mod.name = "first"
    first_mod.start_background.side_effect = RuntimeError("bg error")

    second_mod = MagicMock()
    second_mod.name = "second"

    monkeypatch.setattr(
        "plugin.framework.module_base.ModuleLoader.load_modules",
        lambda services: [first_mod, second_mod],
    )

    main._bootstrap_state = BootstrapState.UNINITIALIZED
    main._modules = []
    main._bootstrap_error = None

    main.bootstrap(MagicMock())

    first_mod.start_background.assert_called_once()
    second_mod.start_background.assert_called_once()
    assert main._bootstrap_state is BootstrapState.INITIALIZED
    assert isinstance(main._bootstrap_error, RuntimeError)
    core_handlers_mock.assert_called_once()


def test_bootstrap_eval_harness_skips_terminate_listener(monkeypatch):
    """When WRITERAGENT_EVAL_HARNESS=1, bootstrap skips _install_terminate_listener."""
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    fake_ctx = MagicMock()
    monkeypatch.setattr(main, "get_ctx", lambda: fake_ctx)
    monkeypatch.setattr("plugin.framework.config.init_config", lambda ctx=None: "")
    monkeypatch.setattr("plugin.framework.i18n.init_i18n", lambda ctx: None)
    monkeypatch.setattr("plugin.framework.main_shared.register_core_handlers", lambda: None)
    monkeypatch.setattr(main, "set_package_extension_id", lambda extension_id: None)
    monkeypatch.setattr("plugin.framework.module_base.ModuleLoader.load_modules", lambda services: [])

    orig_env = os.environ.get("WRITERAGENT_EVAL_HARNESS")
    try:
        os.environ["WRITERAGENT_EVAL_HARNESS"] = "1"
        monkeypatch.setattr(
            "plugin.framework.uno_context.get_desktop",
            MagicMock(side_effect=AssertionError("get_desktop must not be called")),
        )
        main._bootstrap_state = BootstrapState.UNINITIALIZED
        main._modules = []
        main._bootstrap_error = None
        main.bootstrap(fake_ctx)
        assert main._bootstrap_state is BootstrapState.INITIALIZED

        # Without eval harness, _install_terminate_listener is called
        os.environ.pop("WRITERAGENT_EVAL_HARNESS", None)
        mock_install = MagicMock()
        monkeypatch.setattr(main, "_install_terminate_listener", mock_install)
        main._bootstrap_state = BootstrapState.UNINITIALIZED
        main._modules = []
        main._bootstrap_error = None
        main.bootstrap(fake_ctx)
        mock_install.assert_called_once_with(fake_ctx)
    finally:
        if orig_env is not None:
            os.environ["WRITERAGENT_EVAL_HARNESS"] = orig_env
        else:
            os.environ.pop("WRITERAGENT_EVAL_HARNESS", None)


def test_dispatch_command_unhandled_raises_when_bootstrap_failed():
    """_dispatch_command raises BOOTSTRAP_FAILED only when a command is unhandled."""
    main._bootstrap_state = BootstrapState.INITIALIZED
    main._bootstrap_error = RuntimeError("fatal startup error")

    # Handled command does not raise WriterAgentException
    handled_ran = False

    def handler():
        nonlocal handled_ran
        handled_ran = True

    register_action_handler("test", "handled_action", handler)
    try:
        main._dispatch_command("test.handled_action")
        assert handled_ran is True

        # Unhandled command raises BOOTSTRAP_FAILED
        with pytest.raises(WriterAgentException) as exc_info:
            main._dispatch_command("test.unhandled_command")
        assert exc_info.value.code == "BOOTSTRAP_FAILED"
    finally:
        _ACTION_HANDLERS.pop("test.handled_action", None)


# --- Bug 2 tests: off-thread bootstrap guards ---


def test_bootstrap_off_main_thread_raises_bootstrap_off_thread():
    """Calling bootstrap off-thread when uninitialized raises BOOTSTRAP_OFF_THREAD."""
    main._bootstrap_state = BootstrapState.UNINITIALIZED
    caught: list[Exception] = []

    def run_boot():
        try:
            main.bootstrap(MagicMock())
        except Exception as exc:
            caught.append(exc)

    t = threading.Thread(target=run_boot)
    t.start()
    t.join()

    assert len(caught) == 1
    assert isinstance(caught[0], WriterAgentException)
    assert caught[0].code == "BOOTSTRAP_OFF_THREAD"
    assert main._bootstrap_state is BootstrapState.UNINITIALIZED


def test_get_tools_off_main_thread_when_initialized():
    """get_tools off-thread succeeds when bootstrap is already initialized."""
    main._bootstrap_state = BootstrapState.INITIALIZED
    mock_tools = MagicMock()
    main._tools = mock_tools

    result: list[Any] = []

    def fetch_tools():
        result.append(main.get_tools())

    t = threading.Thread(target=fetch_tools)
    t.start()
    t.join()

    assert result == [mock_tools]


def test_get_tools_and_get_services_raise_bootstrap_failed_when_registry_none(monkeypatch):
    """get_tools and get_services raise BOOTSTRAP_FAILED if bootstrap leaves them None."""
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    main._bootstrap_state = BootstrapState.UNINITIALIZED
    main._tools = None
    main._services = None

    monkeypatch.setattr(main, "bootstrap", lambda ctx=None: None)

    with pytest.raises(WriterAgentException) as exc1:
        main.get_tools()
    assert exc1.value.code == "BOOTSTRAP_FAILED"

    with pytest.raises(WriterAgentException) as exc2:
        main.get_services()
    assert exc2.value.code == "BOOTSTRAP_FAILED"


# --- Bug 3 tests: menu:update event triggers background worker ---


def test_menu_update_subscriber_runs_in_background(monkeypatch):
    """menu:update subscriber must use run_in_background, not QueueExecutor.execute."""
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr(main, "get_ctx", lambda: MagicMock())
    monkeypatch.setattr("plugin.framework.config.init_config", lambda ctx=None: "")
    monkeypatch.setattr("plugin.framework.i18n.init_i18n", lambda ctx: None)
    monkeypatch.setattr("plugin.framework.module_base.ModuleLoader.load_modules", lambda services: [])
    monkeypatch.setattr("plugin.framework.main_shared.register_core_handlers", lambda: None)
    monkeypatch.setattr(main, "set_package_extension_id", lambda extension_id: None)

    bus = EventBus()
    monkeypatch.setattr("plugin.framework.event_bus.get_event_bus", lambda: bus)

    bg_calls: list[Any] = []
    main_calls: list[Any] = []

    monkeypatch.setattr("plugin.framework.worker_pool.run_in_background", lambda fn, *a, **kw: bg_calls.append(fn))
    monkeypatch.setattr("plugin.framework.queue_executor.QueueExecutor.execute", lambda fn, *a, **kw: main_calls.append(fn))

    main._bootstrap_state = BootstrapState.UNINITIALIZED
    main._bootstrap_error = None
    main.bootstrap(MagicMock())

    bus.emit("menu:update")

    assert main.notify_menu_update in bg_calls
    assert main.notify_menu_update not in main_calls


# --- Bug 4 tests: handler failures reported safely without raising ---


def test_dispatch_command_handler_exception_reported_safely(monkeypatch):
    """Handler exceptions in _dispatch_command are caught and reported; command does not raise."""
    reported: list[tuple[str, BaseException]] = []
    monkeypatch.setattr("plugin.framework.main_shared.report_action_failure", lambda cmd, exc: reported.append((cmd, exc)))

    def faulty_handler():
        raise ValueError("broken handler")

    register_action_handler("test", "failing", faulty_handler)
    try:
        main._dispatch_command("test.failing")
        assert len(reported) == 1
        assert reported[0][0] == "test.failing"
        assert isinstance(reported[0][1], ValueError)
    finally:
        _ACTION_HANDLERS.pop("test.failing", None)


def test_dispatch_command_module_on_action_propagates():
    """Module on_action exceptions are not caught in _dispatch_command and propagate."""
    main._bootstrap_state = BootstrapState.INITIALIZED
    mock_mod = MagicMock()
    mock_mod.name = "mod_prefix"
    mock_mod.on_action.side_effect = RuntimeError("module on_action exploded")

    main._modules = [mock_mod]
    with pytest.raises(RuntimeError, match="module on_action exploded"):
        main._dispatch_command("mod_prefix.run")


def test_report_action_failure_survives_dialog_exceptions(monkeypatch):
    """report_action_failure survives both msgbox_with_report and msgbox raising."""
    monkeypatch.setattr(
        "plugin.chatbot.dialogs.msgbox_with_report",
        MagicMock(side_effect=RuntimeError("msgbox_with_report failed")),
    )
    monkeypatch.setattr(
        "plugin.chatbot.dialogs.msgbox",
        MagicMock(side_effect=RuntimeError("msgbox fallback failed")),
    )

    # Must log both and not propagate
    report_action_failure("test.action", ValueError("original problem"))


# --- Bug 5 tests: DispatchHandler.dispatch catches dialog failures ---


def test_dispatch_handler_shows_error_report(monkeypatch):
    """DispatchHandler.dispatch shows error report when command fails."""
    monkeypatch.setattr(
        "plugin.main._dispatch_command",
        MagicMock(side_effect=RuntimeError("command dispatch failure")),
    )
    mock_report = MagicMock()
    monkeypatch.setattr(
        "plugin.chatbot.dialogs.msgbox_with_report",
        mock_report,
    )

    handler = main.DispatchHandler(MagicMock())
    url = MagicMock()
    url.Path = "test.command"
    url.Query = ""

    handler.dispatch(url, ())
    mock_report.assert_called_once()


# --- Bug 6 tests: removeStatusListener drops proxy with uno_same ---


def test_remove_status_listener_drops_proxy_with_uno_same():
    """removeStatusListener compares listeners with uno_same rather than identity."""
    handler = main.DispatchHandler(MagicMock())

    class ListenerProxy:
        def __init__(self, val: int):
            self.val = val

        def __eq__(self, other: object) -> bool:
            return isinstance(other, ListenerProxy) and self.val == other.val

    l1 = ListenerProxy(42)
    l2 = ListenerProxy(42)
    assert l1 is not l2
    assert l1 == l2

    u1 = MagicMock()
    u1.Complete = ".uno:WriterAgentAction"
    u1.Path = "test.action"
    u1.Query = ""

    handler.addStatusListener(l1, u1)
    assert len(main._status_listeners) == 1

    # Different URL Complete does not drop listener
    u_diff = MagicMock()
    u_diff.Complete = ".uno:OtherAction"
    u_diff.Path = "test.action"
    u_diff.Query = ""
    handler.removeStatusListener(l2, u_diff)
    assert len(main._status_listeners) == 1

    # Same Complete with equivalent proxy drops listener
    handler.removeStatusListener(l2, u1)
    assert len(main._status_listeners) == 0


def test_add_status_listener_replaces_same_url():
    """addStatusListener replaces existing listener for the same Complete URL if uno_same."""
    handler = main.DispatchHandler(MagicMock())

    class ListenerProxy:
        def __init__(self, val: int):
            self.val = val

        def __eq__(self, other: object) -> bool:
            return isinstance(other, ListenerProxy) and self.val == other.val

    l1 = ListenerProxy(42)
    l2 = ListenerProxy(42)
    assert l1 is not l2
    assert l1 == l2

    u1 = MagicMock()
    u1.Complete = ".uno:WriterAgentAction"
    u1.Path = "test.action"
    u1.Query = ""

    handler.addStatusListener(l1, u1)
    assert len(main._status_listeners) == 1
    assert main._status_listeners[0][0] is l1

    # Adding equal proxy for same Complete replaces the entry
    handler.addStatusListener(l2, u1)
    assert len(main._status_listeners) == 1
    assert main._status_listeners[0][0] is l2

    # A different Complete appends
    u2 = MagicMock()
    u2.Complete = ".uno:OtherAction"
    u2.Path = "test.action"
    u2.Query = ""
    handler.addStatusListener(l2, u2)
    assert len(main._status_listeners) == 2


def test_notify_menu_update_prune_uses_uno_same(monkeypatch):
    """Two entries comparing equal under uno_same are both pruned when statusChanged raises."""
    class ListenerProxy:
        def __init__(self, val: int):
            self.val = val

        def __eq__(self, other: object) -> bool:
            return isinstance(other, ListenerProxy) and self.val == other.val

        def statusChanged(self, ev: object) -> None:
            raise RuntimeError("dead listener")

    l1 = ListenerProxy(10)
    l2 = ListenerProxy(10)
    assert l1 is not l2
    assert l1 == l2

    u1 = MagicMock()
    u1.Complete = ".uno:WriterAgentAction"
    u1.Path = "test.action"

    main._status_listeners.clear()
    main._status_listeners.append((l1, u1))
    main._status_listeners.append((l2, u1))
    assert len(main._status_listeners) == 2

    monkeypatch.setattr(main, "get_menu_text", lambda cmd: "Test Label")
    monkeypatch.setattr("plugin.framework.worker_pool.run_in_background", lambda fn: None)

    main.notify_menu_update()
    assert len(main._status_listeners) == 0


def test_notify_menu_update_posts_prune_off_main_thread(monkeypatch):
    """Off the main thread, dead listeners are posted to the main thread for uno_same pruning."""
    class ListenerProxy:
        def __init__(self, val: int):
            self.val = val

        def __eq__(self, other: object) -> bool:
            return isinstance(other, ListenerProxy) and self.val == other.val

        def statusChanged(self, ev: object) -> None:
            raise RuntimeError("dead listener")

    l1 = ListenerProxy(10)
    l2 = ListenerProxy(10)
    assert l1 is not l2
    assert l1 == l2

    u1 = MagicMock()
    u1.Complete = ".uno:WriterAgentAction"
    u1.Path = "test.action"

    main._status_listeners.clear()
    try:
        main._status_listeners.append((l1, u1))
        main._status_listeners.append((l2, u1))
        assert len(main._status_listeners) == 2

        monkeypatch.setattr(main, "get_menu_text", lambda cmd: "Test Label")
        monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
        monkeypatch.delenv("WRITERAGENT_EVAL_HARNESS", raising=False)

        bg_calls: list[Any] = []
        monkeypatch.setattr(
            "plugin.framework.worker_pool.run_in_background",
            lambda fn, *args, **kwargs: bg_calls.append(fn),
        )

        posted: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

        def mock_post(fn: Any, *args: Any, **kwargs: Any) -> None:
            posted.append((fn, args, kwargs))

        monkeypatch.setattr("plugin.framework.queue_executor.post_to_main_thread", mock_post)

        uno_same_threads: list[threading.Thread] = []
        real_uno_same = main.uno_same

        def wrapped_uno_same(a: Any, b: Any) -> bool:
            uno_same_threads.append(threading.current_thread())
            return real_uno_same(a, b)

        monkeypatch.setattr(main, "uno_same", wrapped_uno_same)

        worker = threading.Thread(target=main.notify_menu_update)
        worker.start()
        worker.join()

        assert len(main._status_listeners) == 2
        assert len(uno_same_threads) == 0
        assert len(posted) == 1
        assert posted[0][0] is main._prune_failed_status_listeners
        assert posted[0][1] == ([(l1, u1), (l2, u1)],)
        assert posted[0][2].get("bound_scope") is None
        assert main._update_menu_icons in bg_calls

        # On the test thread, call that posted function with the posted args.
        posted[0][0](*posted[0][1])
        assert len(main._status_listeners) == 0
        assert len(uno_same_threads) > 0
        test_thread = threading.current_thread()
        assert all(t == test_thread for t in uno_same_threads)
    finally:
        main._status_listeners.clear()


def test_notify_menu_update_text_failure_does_not_prune_or_stop(monkeypatch):
    """Failure in get_menu_text skips that listener without pruning or stopping icon updates."""
    l1 = MagicMock()
    l2 = MagicMock()
    u1 = MagicMock()
    u1.Complete = "org.extension.writeragent:cmd.fail"
    u1.Path = "cmd.fail"
    u1.Query = ""
    u2 = MagicMock()
    u2.Complete = "org.extension.writeragent:cmd.ok"
    u2.Path = "cmd.ok"
    u2.Query = ""

    main._status_listeners.clear()
    try:
        main._status_listeners.append((l1, u1))
        main._status_listeners.append((l2, u2))
        assert len(main._status_listeners) == 2

        def fake_get_menu_text(cmd: str) -> str:
            if cmd == "cmd.fail":
                raise RuntimeError("menu text boom")
            return "OK Label"

        monkeypatch.setattr(main, "get_menu_text", fake_get_menu_text)
        monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
        monkeypatch.delenv("WRITERAGENT_EVAL_HARNESS", raising=False)

        bg_calls: list[Any] = []
        monkeypatch.setattr(
            "plugin.framework.worker_pool.run_in_background",
            lambda fn, *args, **kwargs: bg_calls.append(fn),
        )

        main.notify_menu_update()

        l1.statusChanged.assert_not_called()
        l2.statusChanged.assert_called_once()
        assert len(main._status_listeners) == 2
        assert main._update_menu_icons in bg_calls
    finally:
        main._status_listeners.clear()


def test_update_menu_icons_posts_unscoped(monkeypatch):
    posted = []

    def mock_post(fn: Any, *args: Any, **kwargs: Any) -> None:
        posted.append((fn, args, kwargs))

    monkeypatch.setattr("plugin.framework.queue_executor.post_to_main_thread", mock_post)
    main._update_menu_icons()

    assert len(posted) == 1
    assert posted[0][0] is main._update_menu_icons_impl
    assert posted[0][2].get("bound_scope") is None



# --- Bug 7 tests: menu icon apply stamp and graphic caching ---


def test_icon_apply_signature_and_icons_unchanged():
    """_icon_apply_signature and _icons_unchanged track prefix and pixel size."""
    cmds: dict[str, tuple[str, str]] = {
        ".uno:Cmd1": ("writer", "prefix_a"),
        ".uno:Cmd2": ("calc", "prefix_b"),
    }
    stamp = main._icon_apply_signature(cmds, 16)

    assert main._icons_unchanged(cmds, 16, stamp) is True

    # Changed prefix -> False
    cmds_changed_prefix = {
        ".uno:Cmd1": ("writer", "prefix_c"),
        ".uno:Cmd2": ("calc", "prefix_b"),
    }
    assert main._icons_unchanged(cmds_changed_prefix, 16, stamp) is False

    # Changed pixel size -> False
    assert main._icons_unchanged(cmds, 24, stamp) is False


def test_update_menu_icons_impl_caches_and_skips_on_stamp(monkeypatch):
    """_update_menu_icons_impl caches loaded graphics and skips reload when stamp matches."""
    loader_calls: list[tuple[str, int]] = []
    fake_graphic = MagicMock()

    def mock_loader(module_name: str, filename: str, ctx: Any) -> Any:
        loader_calls.append((filename, 16))
        return fake_graphic

    monkeypatch.setattr(main, "_load_icon_graphic", mock_loader)

    mock_img_mgr = MagicMock()
    mock_img_mgr.hasImage.return_value = False

    mock_module_mgr = MagicMock()
    mock_module_mgr.getByName.return_value = mock_img_mgr

    mock_sm = MagicMock()
    mock_sm.createInstanceWithContext.return_value = mock_module_mgr

    mock_ctx = MagicMock()
    mock_ctx.ServiceManager = mock_sm
    monkeypatch.setattr(main, "get_ctx", lambda: mock_ctx)

    monkeypatch.setattr(main, "_collect_icon_commands", lambda: {".uno:CmdA": ("writer", "prefix_1")})
    monkeypatch.setattr("plugin.framework.menu_icon_dpi.resolve_menu_icon_pixel_size", lambda doc: 16)
    monkeypatch.setattr("plugin.framework.menu_icon_dpi.menu_icon_filename", lambda prefix, px, ctx=None: f"{prefix}_{px}.png")
    monkeypatch.setattr("plugin.framework.appearance.get_style_window", lambda ctx=None: None)

    main._loaded_icon_graphics.clear()
    main._icon_apply_stamp = None

    # First call: loads graphic and sets stamp
    main._update_menu_icons_impl()
    assert len(loader_calls) == 1
    assert main._icon_apply_stamp is not None

    # Second call: stamp matches -> early return, loader not called again
    main._update_menu_icons_impl()
    assert len(loader_calls) == 1

    # Third call: new prefix -> loader called again
    monkeypatch.setattr(main, "_collect_icon_commands", lambda: {".uno:CmdA": ("writer", "prefix_2")})
    main._update_menu_icons_impl()
    assert len(loader_calls) == 2


# --- Bug 8 tests: icon context uses get_ctx() only ---


def test_update_menu_icons_impl_uses_get_ctx_not_get_component_context(monkeypatch):
    """_update_menu_icons_impl uses get_ctx() and never uno.getComponentContext()."""
    import uno

    sentinel_ctx = MagicMock()
    monkeypatch.setattr(main, "get_ctx", lambda: sentinel_ctx)
    monkeypatch.setattr(
        uno,
        "getComponentContext",
        MagicMock(side_effect=AssertionError("uno.getComponentContext must not be called")),
    )
    monkeypatch.setattr("plugin.framework.menu_icon_dpi.resolve_menu_icon_pixel_size", lambda doc: 16)
    monkeypatch.setattr("plugin.framework.appearance.get_style_window", lambda ctx=None: None)
    monkeypatch.setattr(main, "_collect_icon_commands", lambda: {})
    main._services = None

    # _update_menu_icons_impl should retrieve sentinel_ctx from get_ctx() and not call uno.getComponentContext()
    main._update_menu_icons_impl()
    uno.getComponentContext.assert_not_called()


def test_load_icon_graphic_when_ctx_none_returns_none_without_get_component_context(monkeypatch):
    """_load_icon_graphic returns None when get_ctx() is None without calling getComponentContext."""
    import uno

    monkeypatch.setattr(main, "get_ctx", lambda: None)
    monkeypatch.setattr(
        uno,
        "getComponentContext",
        MagicMock(side_effect=AssertionError("uno.getComponentContext must not be called")),
    )

    result = main._load_icon_graphic("test_mod", "test_file.png", None)
    assert result is None
    uno.getComponentContext.assert_not_called()


def test_load_icon_graphic_uses_service_manager_attribute(monkeypatch):
    """_load_icon_graphic accesses ctx.ServiceManager without calling getServiceManager()."""
    fake_sm = MagicMock()
    fake_sm.createInstanceWithContext.return_value = None

    ctx = MagicMock()
    ctx.ServiceManager = fake_sm
    ctx.getServiceManager.side_effect = AssertionError("getServiceManager must not be called")

    monkeypatch.setattr(main, "get_ctx", lambda: ctx)
    result = main._load_icon_graphic("test_mod", "test_file.png", None)
    assert result is None
    ctx.getServiceManager.assert_not_called()
    fake_sm.createInstanceWithContext.assert_called_once_with("com.sun.star.graphic.GraphicProvider", ctx)



# --- Bug 9 tests: MainBootstrapJob exception handling ---


def test_main_bootstrap_job_execute_logging_failure():
    """MainBootstrapJob.execute logs init_logging error and proceeds with bootstrap."""
    job = main.MainBootstrapJob(MagicMock())
    with (
        patch("plugin.main.init_logging", side_effect=RuntimeError("logging failed")),
        patch("plugin.main.bootstrap") as mock_boot,
        patch("plugin.writer.locale.ai_grammar_proofreader.ensure_writeragent_proofreader_configured"),
    ):
        res = job.execute(())
        assert res == ()
        mock_boot.assert_called_once()


def test_main_bootstrap_job_trigger_logging_failure():
    """MainBootstrapJob.trigger logs init_logging error and proceeds with dispatch."""
    job = main.MainBootstrapJob(MagicMock())
    with (
        patch("plugin.main.init_logging", side_effect=RuntimeError("logging failed")),
        patch("plugin.main._dispatch_command") as mock_disp,
    ):
        job.trigger("main.settings")
        mock_disp.assert_called_once_with("main.settings")


def test_main_bootstrap_job_trigger_extend_selection_none_model():
    """MainBootstrapJob.trigger with ExtendSelection on None document does not raise or handle."""
    job = main.MainBootstrapJob(MagicMock())
    with (
        patch("plugin.main.get_active_document", return_value=None),
        patch.object(job, "_handle_selection_actions") as mock_sel,
    ):
        job.trigger("ExtendSelection")
        mock_sel.assert_not_called()


# --- Bug 10 tests: TestTypes removed from framework actions ---


def test_main_bootstrap_job_trigger_test_types_and_noop():
    """TestTypes is ignored and does not dispatch; NoOp still dispatches."""
    job = main.MainBootstrapJob(MagicMock())
    with patch("plugin.main._dispatch_command") as mock_disp:
        # TestTypes is no longer recognized as framework action
        job.trigger("TestTypes")
        mock_disp.assert_not_called()

        # NoOp is recognized and dispatched
        job.trigger("NoOp")
        mock_disp.assert_called_once_with("main.NoOp")


# --- Bug 11 tests: module exports ---


def test_module_exports_expected_classes():
    """plugin.main must export g_ImplementationHelper, DispatchHandler, and MainBootstrapJob."""
    assert hasattr(main, "g_ImplementationHelper")
    assert hasattr(main, "DispatchHandler")
    assert hasattr(main, "MainBootstrapJob")


def test_trigger_body_exception_is_reported():
    """MainBootstrapJob.trigger catches body exceptions and reports them via report_action_failure."""
    job = main.MainBootstrapJob(MagicMock())
    err = RuntimeError("dispatch exploded")
    with (
        patch("plugin.main._dispatch_command", side_effect=err),
        patch("plugin.framework.main_shared.report_action_failure") as mock_report,
    ):
        job.trigger("main.settings")
        mock_report.assert_called_once_with("main.settings", err)


def test_trigger_extend_selection_bootstraps():
    """Triggering ExtendSelection on a Writer document bootstraps before dispatching."""
    fake_ctx = MagicMock()
    job = main.MainBootstrapJob(fake_ctx)
    fake_doc = MagicMock()

    with (
        patch("plugin.main.get_active_document", return_value=fake_doc),
        patch("plugin.doc.doc_type.get_document_type", return_value=DocumentType.WRITER),
        patch("plugin.main.bootstrap") as mock_boot,
        patch("plugin.chatbot.selection.do_selection_action_for_document") as mock_do,
    ):
        job.trigger("ExtendSelection")
        mock_boot.assert_called_once()
        mock_do.assert_called_once()


def test_main_calls_execute_not_hello(monkeypatch):
    """plugin.main.main() creates MainBootstrapJob and calls execute(()), not trigger."""
    fake_ctx = MagicMock()
    monkeypatch.setitem(main.__dict__, "XSCRIPTCONTEXT", fake_ctx)
    with (
        patch.object(main.MainBootstrapJob, "execute") as mock_exec,
        patch.object(main.MainBootstrapJob, "trigger") as mock_trig,
    ):
        main.main()
        mock_exec.assert_called_once_with(())
        mock_trig.assert_not_called()


def test_command_from_dispatch_url_and_dispatch(monkeypatch):
    class _Url:
        def __init__(self, path="", query="", complete="", protocol=""):
            self.Path = path
            self.Query = query
            self.Complete = complete
            self.Protocol = protocol

    # Path="main.settings", Query="" -> main.settings; dispatch passes to _dispatch_command
    dispatched = []
    monkeypatch.setattr(main, "_dispatch_command", lambda cmd: dispatched.append(cmd))
    monkeypatch.setattr(main, "init_logging", lambda _ctx: None)
    monkeypatch.setattr("plugin.framework.worker_pool.run_in_background", lambda _fn: None)

    handler = main.DispatchHandler(MagicMock())
    u1 = _Url(path="main.settings", query="")
    assert main._command_from_dispatch_url(u1) == "main.settings"
    handler.dispatch(u1, ())
    assert dispatched == ["main.settings"]

    # Path="", Protocol="", Complete="org.extension.writeragent:main.settings" -> main.settings. queryDispatch returns the handler.
    u2 = _Url(complete="org.extension.writeragent:main.settings")
    assert main._command_from_dispatch_url(u2) == "main.settings"
    assert handler.queryDispatch(u2, "", 0) is handler

    # Path="chatbot.debug_sidebar", Query="RECORD_CLICKED" -> chatbot.debug_sidebar.RECORD_CLICKED
    u3 = _Url(path="chatbot.debug_sidebar", query="RECORD_CLICKED")
    assert main._command_from_dispatch_url(u3) == "chatbot.debug_sidebar.RECORD_CLICKED"

    # Path="chatbot.debug_sidebar?OPEN_CALC", Query="" -> that path unchanged
    u4 = _Url(path="chatbot.debug_sidebar?OPEN_CALC", query="")
    assert main._command_from_dispatch_url(u4) == "chatbot.debug_sidebar?OPEN_CALC"

    # Path="", Complete="org.extension.writeragent:chatbot.debug_sidebar?OPEN_CALC", Query="OPEN_CALC" -> chatbot.debug_sidebar?OPEN_CALC (no second append)
    u5 = _Url(complete="org.extension.writeragent:chatbot.debug_sidebar?OPEN_CALC", query="OPEN_CALC")
    assert main._command_from_dispatch_url(u5) == "chatbot.debug_sidebar?OPEN_CALC"

    # Path="", Query="RECORD_CLICKED", Complete="" -> .RECORD_CLICKED
    u6 = _Url(query="RECORD_CLICKED")
    assert main._command_from_dispatch_url(u6) == ".RECORD_CLICKED"

    # queryDispatch with Protocol="org.extension.librepy:" returns the null dispatch
    u7 = _Url(protocol="org.extension.librepy:")
    assert handler.queryDispatch(u7, "", 0) is None


def test_split_command():
    assert main._split_command("main.settings") == ("main", "settings")
    assert main._split_command("chatbot.debug_sidebar.RECORD_CLICKED") == (
        "chatbot",
        "debug_sidebar.RECORD_CLICKED",
    )
    assert main._split_command("noperiod") is None
    assert main._split_command(".leading") is None
    assert main._split_command("") is None


def test_register_core_handlers_continues_after_optional_import_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    orig_handlers = dict(_ACTION_HANDLERS)
    had_module = "plugin.calc.excel_py_convert.auto_open" in sys.modules
    prev_module = sys.modules.get("plugin.calc.excel_py_convert.auto_open")

    recorded: list[str] = []
    try:
        sys.modules["plugin.calc.excel_py_convert.auto_open"] = types.ModuleType(
            "plugin.calc.excel_py_convert.auto_open"
        )
        monkeypatch.setattr(
            "plugin.calc.python.editor_context_menu.install_calc_cell_context_menu",
            lambda _ctx: None,
        )
        monkeypatch.setattr(
            "plugin.notebook.notebook_controls.install_notebook_run_button_wiring",
            lambda _ctx: None,
        )
        monkeypatch.setattr(
            "plugin.calc.python.geometric_recalc.install_geometric_recalc",
            lambda: recorded.append("geo"),
        )
        monkeypatch.setattr(
            "plugin.writer.change_context_menu.install_writer_change_context_menu",
            lambda _ctx: recorded.append("writer_menu"),
        )
        monkeypatch.setattr(
            "plugin.writer.review_toolbar.install_review_toolbar",
            lambda _ctx: recorded.append("toolbar"),
        )
        monkeypatch.setattr("plugin.framework.uno_context.get_ctx", lambda: MagicMock())

        caplog.set_level(logging.DEBUG, logger="plugin.framework.main_shared")
        from plugin.framework.main_shared import register_core_handlers

        register_core_handlers()

        assert "Excel PY auto-convert on open install failed" in caplog.text
        assert recorded == ["geo", "writer_menu", "toolbar"]
        assert get_action_handler("embeddings.search_dialog") is not None
        assert get_action_handler("writer.accept_change") is not None
        assert get_action_handler("writer.reject_change") is not None
        assert get_action_handler("writer.review_accept_all") is not None
        assert get_action_handler("writer.review_reject_all") is not None
        assert get_action_handler("writer.review_next") is not None
        assert get_action_handler("writer.review_prev") is not None
    finally:
        _ACTION_HANDLERS.clear()
        _ACTION_HANDLERS.update(orig_handlers)
        if had_module:
            sys.modules["plugin.calc.excel_py_convert.auto_open"] = prev_module
        else:
            sys.modules.pop("plugin.calc.excel_py_convert.auto_open", None)


def test_bootstrap_state_enum(monkeypatch):
    """BootstrapState enum tracks bootstrap lifecycle states."""
    from plugin.main import BootstrapState

    assert BootstrapState.UNINITIALIZED.value == "uninitialized"
    assert BootstrapState.BOOTSTRAPPING.value == "bootstrapping"
    assert BootstrapState.INITIALIZED.value == "initialized"

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr(main, "get_ctx", lambda: MagicMock())
    monkeypatch.setattr("plugin.framework.config.init_config", lambda ctx=None: "")
    monkeypatch.setattr("plugin.framework.i18n.init_i18n", lambda ctx: None)
    monkeypatch.setattr("plugin.framework.main_shared.register_core_handlers", lambda: None)
    monkeypatch.setattr(main, "set_package_extension_id", lambda extension_id: None)
    monkeypatch.setattr("plugin.framework.module_base.ModuleLoader.load_modules", lambda services: [])

    main._bootstrap_state = BootstrapState.UNINITIALIZED
    assert main._bootstrap_state is BootstrapState.UNINITIALIZED

    main.bootstrap(MagicMock())
    assert main._bootstrap_state is BootstrapState.INITIALIZED



