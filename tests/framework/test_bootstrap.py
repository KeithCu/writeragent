"""Unit tests for shared bootstrap_extension (pythonloader exec namespace)."""

from __future__ import annotations

from unittest.mock import patch

from plugin.bootstrap import BootstrapState, bootstrap_extension
from plugin.framework.queue_executor import main as main_thread


def test_bootstrap_extension_pythonloader_namespace(monkeypatch):
    """pythonloader execs main.py as uno_component without sys.modules registration."""
    monkeypatch.setattr(main_thread, "is_current", lambda: True)
    monkeypatch.setattr(
        "plugin.framework.queue_executor.MainThread.on_python_main_thread",
        lambda: True,
    )
    monkeypatch.setattr("plugin.bootstrap.get_ctx", lambda: None)
    monkeypatch.setattr("plugin.framework.config.init_config", lambda ctx=None: None)
    monkeypatch.setattr("plugin.framework.i18n.init_i18n", lambda ctx: None)
    monkeypatch.setattr("plugin.bootstrap.set_package_extension_id", lambda extension_id: None)
    monkeypatch.setattr("plugin.bootstrap_ui_hooks.register_framework_ui_hooks", lambda: None)
    monkeypatch.setattr(
        "plugin.scripting.native_binaries.ensure_native_binaries_on_path",
        lambda: None,
    )

    ns: dict = {
        "__name__": "uno_component",
        "_bootstrap_state": BootstrapState.UNINITIALIZED,
    }

    def finish(unused_ctx):
        ns["_bootstrap_state"] = BootstrapState.INITIALIZED

    bootstrap_extension(
        None,
        extension_id="org.example.test",
        finish=finish,
        state=ns,
    )
    assert ns["_bootstrap_state"] is BootstrapState.INITIALIZED

    with patch("plugin.framework.config.init_config") as mock_init_config:
        bootstrap_extension(
            None,
            extension_id="org.example.test",
            finish=finish,
            state=ns,
        )
        mock_init_config.assert_not_called()
