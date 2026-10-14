"""Unit tests for plugin.main: bootstrap lifecycle, dispatch, listeners, and icon caching."""

from __future__ import annotations

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
from plugin.framework.errors import WriterAgentException
from plugin.framework.main_shared import (
    _ACTION_HANDLERS,
    register_action_handler,
    report_action_failure,
)


@pytest.fixture(autouse=True)
def _isolate_main_state():
    """Isolate global state in plugin.main across tests."""
    prev_init = main._initialized
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
        main._initialized = prev_init
        main._services = prev_services
        main._tools = prev_tools
        main._modules[:] = prev_modules
        main._status_listeners[:] = prev_listeners
        main._bootstrap_error = prev_err
        main._loaded_icon_graphics.clear()
        main._loaded_icon_graphics.update(prev_graphics)
        main._icon_apply_stamp = prev_stamp


# --- Bug 1 tests: bootstrap error handling and retry ---


def test_bootstrap_load_modules_failure_resets_initialized(monkeypatch):
    """When load_modules raises, _initialized resets to False so bootstrap can be retried."""
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr(main, "get_ctx", lambda: MagicMock())
    monkeypatch.setattr("plugin.framework.config.init_config", lambda ctx=None: "")
    monkeypatch.setattr("plugin.framework.i18n.init_i18n", lambda ctx: None)
    monkeypatch.setattr(main, "_register_core_handlers", lambda: None)
    monkeypatch.setattr(main, "set_package_extension_id", lambda extension_id: None)

    main._initialized = False
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
    assert main._initialized is False
    assert isinstance(main._bootstrap_error, RuntimeError)

    # Second call: retry succeeds
    fail = False
    main.bootstrap(MagicMock())
    assert main._initialized is True
    assert main._bootstrap_error is None


def test_bootstrap_start_background_failure_keeps_initialized(monkeypatch):
    """When start_background raises after modules are loaded, _initialized stays True."""
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr(main, "get_ctx", lambda: MagicMock())
    monkeypatch.setattr("plugin.framework.config.init_config", lambda ctx=None: "")
    monkeypatch.setattr("plugin.framework.i18n.init_i18n", lambda ctx: None)
    monkeypatch.setattr(main, "_register_core_handlers", lambda: None)
    monkeypatch.setattr(main, "set_package_extension_id", lambda extension_id: None)

    main._initialized = False
    main._modules = []
    main._bootstrap_error = None

    mock_mod = MagicMock()
    mock_mod.start_background.side_effect = RuntimeError("bg error")
    monkeypatch.setattr("plugin.framework.module_base.ModuleLoader.load_modules", lambda services: [mock_mod])

    main.bootstrap(MagicMock())
    assert main._initialized is True
    assert isinstance(main._bootstrap_error, RuntimeError)

    # Next call returns early because _initialized is True
    with patch("plugin.framework.module_base.ModuleLoader.load_modules") as mock_load:
        main.bootstrap(MagicMock())
        mock_load.assert_not_called()


def test_dispatch_command_unhandled_raises_when_bootstrap_failed():
    """_dispatch_command raises BOOTSTRAP_FAILED only when a command is unhandled."""
    main._initialized = True
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
    main._initialized = False
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
    assert main._initialized is False


def test_get_tools_off_main_thread_when_initialized():
    """get_tools off-thread succeeds when bootstrap is already initialized."""
    main._initialized = True
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
    main._initialized = False
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
    from plugin.framework.event_bus import EventBus

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    monkeypatch.setattr(main, "get_ctx", lambda: MagicMock())
    monkeypatch.setattr("plugin.framework.config.init_config", lambda ctx=None: "")
    monkeypatch.setattr("plugin.framework.i18n.init_i18n", lambda ctx: None)
    monkeypatch.setattr("plugin.framework.module_base.ModuleLoader.load_modules", lambda services: [])
    monkeypatch.setattr(main, "_register_core_handlers", lambda: None)
    monkeypatch.setattr(main, "set_package_extension_id", lambda extension_id: None)

    bus = EventBus()
    monkeypatch.setattr("plugin.framework.event_bus.get_event_bus", lambda: bus)

    bg_calls: list[Any] = []
    main_calls: list[Any] = []

    monkeypatch.setattr("plugin.framework.worker_pool.run_in_background", lambda fn, *a, **kw: bg_calls.append(fn))
    monkeypatch.setattr("plugin.framework.queue_executor.QueueExecutor.execute", lambda fn, *a, **kw: main_calls.append(fn))

    main._initialized = False
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
    main._initialized = True
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


def test_dispatch_handler_survives_dialog_exceptions(monkeypatch):
    """DispatchHandler.dispatch survives dialog errors during error reporting."""
    monkeypatch.setattr(
        "plugin.main._dispatch_command",
        MagicMock(side_effect=RuntimeError("command dispatch failure")),
    )
    monkeypatch.setattr(
        "plugin.chatbot.dialogs.msgbox_with_report",
        MagicMock(side_effect=RuntimeError("dialog error")),
    )
    monkeypatch.setattr(
        "plugin.chatbot.dialogs.msgbox",
        MagicMock(side_effect=RuntimeError("fallback dialog error")),
    )

    handler = main.DispatchHandler(MagicMock())
    url = MagicMock()
    url.Path = "test.command"
    url.Query = ""

    # Should not raise
    handler.dispatch(url, ())


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
