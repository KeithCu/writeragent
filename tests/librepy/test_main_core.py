"""LibrePy bootstrap registers Settings and Run Python Script handlers."""

from unittest.mock import MagicMock


from plugin.framework.main_shared import get_action_handler


def test_bootstrap_registers_settings_and_run_python(monkeypatch) -> None:
    import plugin.main_core as main_core

    main_core._initialized = False
    monkeypatch.setattr("plugin.framework.config.init_config", lambda _ctx: None)
    monkeypatch.setattr("plugin.framework.i18n.init_i18n", lambda _ctx: None)
    monkeypatch.setattr(
        "plugin.scripting.native_binaries.ensure_downloaded_audio_on_path",
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


def test_execute_inits_logging_before_bootstrap(monkeypatch) -> None:
    import plugin.main_core as main_core

    order: list[str] = []
    monkeypatch.setattr(main_core, "init_logging", lambda _ctx: order.append("log"))
    monkeypatch.setattr(main_core, "bootstrap", lambda _ctx: order.append("boot"))
    monkeypatch.setattr(main_core, "_schedule_update_check", lambda _ctx: order.append("upd"))
    job = main_core.MainBootstrapJob(MagicMock())
    job.execute(())
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
