"""LibrePy bootstrap registers Settings and Run Python Script handlers."""

import sys
import threading
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

import plugin.bootstrap as bootstrap
from plugin.framework.errors import WriterAgentException
from plugin.framework.main_shared import get_action_handler


@pytest.fixture
def _bootstrap_stubs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    bootstrap.mark_uninitialized()
    bootstrap.clear_bootstrap_error()
    monkeypatch.setattr("plugin.framework.config.init_config", lambda _ctx: None)
    monkeypatch.setattr("plugin.framework.i18n.init_i18n", lambda _ctx: None)
    monkeypatch.setattr(
        "plugin.scripting.native_binaries.ensure_native_binaries_on_path",
        lambda: None,
    )
    monkeypatch.setattr(
        "plugin.calc.python.editor_context_menu.install_calc_cell_context_menu",
        lambda _ctx: None,
    )
    monkeypatch.setattr(
        "plugin.calc.python.workbook_lifecycle.install_global_document_event_listener",
        lambda _ctx: None,
    )
    try:
        yield
    finally:
        bootstrap.mark_uninitialized()
        bootstrap.clear_bootstrap_error()


@pytest.mark.parametrize("defer_settings", [False, True], ids=["normal", "defers_settings_import"])
def test_bootstrap_registers_settings_and_run_python(
    monkeypatch: pytest.MonkeyPatch, _bootstrap_stubs: None, defer_settings: bool
) -> None:
    import plugin.main_core as main_core

    if defer_settings:
        monkeypatch.setitem(sys.modules, "plugin.librepy.settings", None)
    main_core.bootstrap(MagicMock())
    assert get_action_handler("main.settings") is not None
    assert get_action_handler("scripting.run_python_dialog") is not None


def test_bootstrap_reentry_returns_without_lock(
    monkeypatch: pytest.MonkeyPatch, _bootstrap_stubs: None
) -> None:
    import plugin.main_core as main_core

    calls = 0

    def fake_init_config(ctx):
        nonlocal calls
        calls += 1
        if calls == 1:
            main_core.bootstrap(ctx)
        return ""

    monkeypatch.setattr("plugin.framework.config.init_config", fake_init_config)
    main_core.bootstrap(MagicMock())
    assert calls == 1
    assert bootstrap.bootstrap_initialized()


def test_execute_inits_logging_before_bootstrap(monkeypatch) -> None:
    import plugin.main_core as main_core

    order: list[str] = []
    monkeypatch.setattr(main_core, "init_logging", lambda _ctx: order.append("log"))
    monkeypatch.setattr(main_core, "bootstrap", lambda _ctx: order.append("boot"))
    monkeypatch.setattr(
        "plugin.chatbot.extension_update_check.schedule_extension_update_check_once",
        lambda _ctx, _ext: order.append("upd"),
    )
    job = main_core.MainBootstrapJob(MagicMock())
    job.execute(())
    assert order == ["log", "boot", "upd"]


@pytest.mark.parametrize("method", ["execute", "trigger"])
def test_bootstraps_when_init_logging_fails(monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    import plugin.main_core as main_core

    order: list[str] = []

    def fail_logging(_ctx):
        order.append("log")
        raise RuntimeError("logging boom")

    monkeypatch.setattr(main_core, "init_logging", fail_logging)
    monkeypatch.setattr(main_core, "bootstrap", lambda _ctx: order.append("boot"))
    monkeypatch.setattr(
        "plugin.chatbot.extension_update_check.schedule_extension_update_check_once",
        lambda _ctx, _ext: order.append("upd"),
    )
    job = main_core.MainBootstrapJob(MagicMock())
    if method == "execute":
        assert job.execute(()) == ()
    else:
        job.trigger("")
    assert order == ["log", "boot", "upd"]


def test_trigger_wraps_bootstrap_errors(monkeypatch) -> None:
    import plugin.main_core as main_core

    def boom(_ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(main_core, "init_logging", lambda _ctx: None)
    monkeypatch.setattr(main_core, "bootstrap", boom)
    job = main_core.MainBootstrapJob(MagicMock())
    job.trigger("")


def test_dispatch_command_reports_handler_failure(monkeypatch) -> None:
    import plugin.main_core as main_core
    from plugin.framework.main_shared import register_action_handler, _ACTION_HANDLERS

    reported = []

    def fake_report(cmd, exc):
        reported.append((cmd, exc))

    monkeypatch.setattr("plugin.main_core.report_action_failure", fake_report)
    monkeypatch.setattr(main_core, "bootstrap", lambda _ctx: None)

    def bad_handler(_frame=None):
        raise RuntimeError("boom")

    register_action_handler("test", "action", bad_handler)
    try:
        main_core._dispatch_command("test.action")
        assert len(reported) == 1
        assert reported[0][0] == "test.action"
        assert isinstance(reported[0][1], RuntimeError)
    finally:
        _ACTION_HANDLERS.pop("test.action", None)


def test_dispatch_reports_failure_once(monkeypatch) -> None:
    import plugin.main_core as main_core

    ctx = MagicMock()
    handler = main_core.DispatchHandler(ctx)
    url = MagicMock()
    url.Protocol = "org.extension.librepy:"
    url.Path = "test.action"
    url.Complete = "org.extension.librepy:test.action"

    reported: list[tuple[str, Exception]] = []
    monkeypatch.setattr(
        "plugin.main_core.report_action_failure",
        lambda cmd, exc: reported.append((cmd, exc)),
    )
    monkeypatch.setattr(main_core, "init_logging", lambda _ctx: None)
    monkeypatch.setattr(main_core, "bootstrap", lambda _ctx: None)

    def boom(_cmd, _ctx):
        raise RuntimeError("dispatch error")

    monkeypatch.setattr(main_core, "_dispatch_command", boom)
    handler.dispatch(url, ())
    assert len(reported) == 1
    assert reported[0][0] == "test.action"
    assert isinstance(reported[0][1], RuntimeError)


def test_dispatch_survives_report_failure(monkeypatch) -> None:
    import plugin.main_core as main_core

    ctx = MagicMock()
    handler = main_core.DispatchHandler(ctx)
    url = MagicMock()
    url.Protocol = "org.extension.librepy:"
    url.Path = "test.action"
    url.Complete = "org.extension.librepy:test.action"

    def report_boom(_cmd, _exc):
        raise RuntimeError("report failed")

    monkeypatch.setattr(
        "plugin.main_core.report_action_failure",
        report_boom,
    )
    monkeypatch.setattr(main_core, "init_logging", lambda _ctx: None)
    monkeypatch.setattr(main_core, "bootstrap", lambda _ctx: None)

    def boom(_cmd, _ctx):
        raise RuntimeError("dispatch error")

    monkeypatch.setattr(main_core, "_dispatch_command", boom)
    # Must not raise
    handler.dispatch(url, ())


def test_calc_menu_failure_does_not_abort_handlers_or_notebook_wiring(
    monkeypatch: pytest.MonkeyPatch, _bootstrap_stubs: None
) -> None:
    import plugin.main_core as main_core

    def raising_calc_menu(_ctx):
        raise RuntimeError("calc menu exploded")

    monkeypatch.setattr(
        "plugin.calc.python.editor_context_menu.install_calc_cell_context_menu",
        raising_calc_menu,
    )
    notebook_wired = []
    monkeypatch.setattr(
        "plugin.notebook.notebook_controls.install_notebook_run_button_wiring",
        lambda _ctx: notebook_wired.append(True),
    )
    main_core.bootstrap(MagicMock())
    assert get_action_handler("scripting.run_python_dialog") is not None
    assert notebook_wired == [True]


def test_bootstrap_off_main_thread_raises():
    """Calling bootstrap off-thread when uninitialized raises BOOTSTRAP_OFF_THREAD."""
    import plugin.main_core as main_core

    bootstrap.mark_uninitialized()
    caught: list[Exception] = []

    def run_boot():
        try:
            main_core.bootstrap(MagicMock())
        except Exception as exc:
            caught.append(exc)

    t = threading.Thread(target=run_boot)
    t.start()
    t.join()

    assert len(caught) == 1
    assert isinstance(caught[0], WriterAgentException)
    assert caught[0].code == "BOOTSTRAP_OFF_THREAD"
    assert not bootstrap.bootstrap_initialized()


def test_librepy_bootstrap_finish_arms_webview_probe():
    """_librepy_bootstrap_finish arms exactly one webview probe on background pool."""
    import plugin.main_core as main_core
    from plugin.scripting import editor_host

    editor_host.reset_webview_probe_state_for_tests()
    ctx = MagicMock()
    try:
        with (
            patch("plugin.scripting.editor_host.get_config", return_value=False),
            patch("plugin.scripting.editor_host.resolve_editor_python", return_value=("/fake/python", "")),
            patch("plugin.scripting.editor_host.run_in_background") as mock_bg,
        ):
            main_core._librepy_bootstrap_finish(ctx)
            probe_calls = [c for c in mock_bg.call_args_list if c[1].get("name") == "webview-probe"]
            assert len(probe_calls) == 1
            assert probe_calls[0][0][0] == editor_host._webview_probe_worker
    finally:
        editor_host.reset_webview_probe_state_for_tests()


def test_librepy_bootstrap_finish_skips_webview_probe_under_eval_harness(monkeypatch):
    """_librepy_bootstrap_finish does not arm webview probe under Policy.eval_harness."""
    import plugin.main_core as main_core
    from plugin.framework.queue_executor import MainThread, Policy
    from plugin.scripting import editor_host

    editor_host.reset_webview_probe_state_for_tests()
    ctx = MagicMock()
    monkeypatch.setattr(MainThread, "_policy", Policy.eval_harness)
    try:
        with (
            patch("plugin.scripting.editor_host.get_config", return_value=False),
            patch("plugin.scripting.editor_host.resolve_editor_python", return_value=("/fake/python", "")),
            patch("plugin.scripting.editor_host.run_in_background") as mock_bg,
        ):
            main_core._librepy_bootstrap_finish(ctx)
            mock_bg.assert_not_called()
    finally:
        editor_host.reset_webview_probe_state_for_tests()



