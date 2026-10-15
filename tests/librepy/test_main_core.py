"""LibrePy bootstrap registers Settings and Run Python Script handlers."""

from unittest.mock import MagicMock


from plugin.framework.main_shared import get_action_handler


def test_bootstrap_registers_settings_and_run_python(monkeypatch) -> None:
    import plugin.main_core as main_core

    main_core._initialized = False
    main_core._bootstrapping = False
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
        "plugin.calc.excel_py_convert.auto_open.install_excel_py_auto_convert",
        lambda _ctx: None,
    )
    try:
        main_core.bootstrap(MagicMock())
        assert get_action_handler("main.settings") is not None
        assert get_action_handler("scripting.run_python_dialog") is not None
    finally:
        main_core._initialized = False
        main_core._bootstrapping = False


def test_bootstrap_reentry_returns_without_lock(monkeypatch) -> None:
    import plugin.main_core as main_core

    main_core._initialized = False
    main_core._bootstrapping = False

    calls = 0

    def fake_init_config(ctx):
        nonlocal calls
        calls += 1
        if calls == 1:
            main_core.bootstrap(ctx)
        return ""

    monkeypatch.setattr("plugin.framework.config.init_config", fake_init_config)
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
        "plugin.calc.excel_py_convert.auto_open.install_excel_py_auto_convert",
        lambda _ctx: None,
    )
    try:
        main_core.bootstrap(MagicMock())
        assert calls == 1
        assert main_core._initialized is True
        assert main_core._bootstrapping is False
        assert not hasattr(main_core, "_init_lock")
        assert not hasattr(main_core, "_DISPATCH_PROTOCOL")
    finally:
        main_core._initialized = False
        main_core._bootstrapping = False


def test_execute_inits_logging_before_bootstrap(monkeypatch) -> None:
    import plugin.main_core as main_core

    order: list[str] = []
    monkeypatch.setattr(main_core, "init_logging", lambda _ctx: order.append("log"))
    monkeypatch.setattr(main_core, "bootstrap", lambda _ctx: order.append("boot"))
    monkeypatch.setattr(main_core, "_schedule_update_check", lambda _ctx: order.append("upd"))
    job = main_core.MainBootstrapJob(MagicMock())
    job.execute(())
    assert order == ["log", "boot", "upd"]


def test_execute_bootstraps_when_init_logging_fails(monkeypatch) -> None:
    import plugin.main_core as main_core

    order: list[str] = []

    def fail_logging(_ctx):
        order.append("log")
        raise RuntimeError("logging boom")

    monkeypatch.setattr(main_core, "init_logging", fail_logging)
    monkeypatch.setattr(main_core, "bootstrap", lambda _ctx: order.append("boot"))
    monkeypatch.setattr(main_core, "_schedule_update_check", lambda _ctx: order.append("upd"))
    job = main_core.MainBootstrapJob(MagicMock())
    assert job.execute(()) == ()
    assert order == ["log", "boot", "upd"]


def test_trigger_wraps_bootstrap_errors(monkeypatch) -> None:
    import plugin.main_core as main_core

    def boom(_ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(main_core, "init_logging", lambda _ctx: None)
    monkeypatch.setattr(main_core, "bootstrap", boom)
    monkeypatch.setattr(main_core, "_schedule_update_check", lambda _ctx: None)
    job = main_core.MainBootstrapJob(MagicMock())
    job.trigger("")


def test_dispatch_command_reports_handler_failure(monkeypatch) -> None:
    import plugin.main_core as main_core
    from plugin.framework.main_shared import register_action_handler, _ACTION_HANDLERS

    reported = []

    def fake_report(cmd, exc):
        reported.append((cmd, exc))

    monkeypatch.setattr("plugin.framework.main_shared.report_action_failure", fake_report)
    monkeypatch.setattr(main_core, "bootstrap", lambda _ctx: None)

    def bad_handler():
        raise RuntimeError("boom")

    register_action_handler("test", "action", bad_handler)
    try:
        main_core._dispatch_command("test.action")
        assert len(reported) == 1
        assert reported[0][0] == "test.action"
        assert isinstance(reported[0][1], RuntimeError)
    finally:
        _ACTION_HANDLERS.pop("test.action", None)


def test_dispatch_guards_msgbox_failure(monkeypatch) -> None:
    import plugin.main_core as main_core

    ctx = MagicMock()
    handler = main_core.DispatchHandler(ctx)
    url = MagicMock()
    url.Protocol = "org.extension.librepy:"
    url.Path = "test.action"
    url.Complete = "org.extension.librepy:test.action"

    monkeypatch.setattr(main_core, "init_logging", lambda _ctx: None)
    monkeypatch.setattr(main_core, "bootstrap", lambda _ctx: None)

    def boom(_cmd, _ctx):
        raise RuntimeError("dispatch error")

    monkeypatch.setattr(main_core, "_dispatch_command", boom)

    def msgbox_boom(*args, **kwargs):
        raise RuntimeError("msgbox failed")

    monkeypatch.setattr("plugin.chatbot.dialogs.msgbox", msgbox_boom)
    # Must not raise
    handler.dispatch(url, ())

