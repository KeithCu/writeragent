"""Unit tests for shared bootstrap_extension (pythonloader exec namespace)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch


from plugin.bootstrap import bootstrap_extension
from plugin.framework.queue_executor import main as main_thread


def _patch_bootstrap_deps(monkeypatch):
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


def test_bootstrap_extension_pythonloader_namespace(monkeypatch):
    """pythonloader execs main.py as uno_component without sys.modules registration."""
    import plugin.bootstrap as bootstrap

    _patch_bootstrap_deps(monkeypatch)
    bootstrap.mark_uninitialized()
    bootstrap.clear_bootstrap_error()

    finish_calls = 0

    def finish(unused_ctx):
        nonlocal finish_calls
        finish_calls += 1
        bootstrap.mark_initialized()

    bootstrap_extension(
        None,
        extension_id="org.example.test",
        finish=finish,
    )
    assert bootstrap.bootstrap_initialized()
    assert finish_calls == 1

    with patch("plugin.framework.config.init_config") as mock_init_config:
        bootstrap_extension(
            None,
            extension_id="org.example.test",
            finish=finish,
        )
        mock_init_config.assert_not_called()
    assert finish_calls == 1
    bootstrap.mark_uninitialized()
    bootstrap.clear_bootstrap_error()


def test_bind_writeragent_first_call_wins(monkeypatch):
    import plugin.app as app
    import plugin.bootstrap as bootstrap

    _patch_bootstrap_deps(monkeypatch)
    prev_finish = app._finish
    prev_services = app._services
    prev_tools = app._tools
    prev_modules = list(app._modules)
    prev_initialized = bootstrap.bootstrap_initialized()
    prev_err = bootstrap.bootstrap_error()
    bootstrap.mark_uninitialized()
    bootstrap.clear_bootstrap_error()
    app._finish = None
    app._services = None
    app._tools = None
    app._modules.clear()

    first_calls = 0
    second_calls = 0

    def first_finish(_ctx):
        nonlocal first_calls
        first_calls += 1
        bootstrap.mark_initialized()

    def second_finish(_ctx):
        nonlocal second_calls
        second_calls += 1

    try:
        app.bind_writeragent(finish=first_finish)
        app.bind_writeragent(finish=second_finish)
        app.bootstrap(None)
        assert first_calls == 1
        assert second_calls == 0
        assert bootstrap.bootstrap_initialized()

        app.bootstrap(None)
        assert first_calls == 1
        assert second_calls == 0
    finally:
        app._finish = prev_finish
        app._services = prev_services
        app._tools = prev_tools
        app._modules.clear()
        app._modules.extend(prev_modules)
        if prev_initialized:
            bootstrap.mark_initialized()
        else:
            bootstrap.mark_uninitialized()
        if prev_err is not None:
            bootstrap.set_bootstrap_error(prev_err)
        else:
            bootstrap.clear_bootstrap_error()


def test_unbound_bootstrap_raises_and_does_not_import_main():
    repo = Path(__file__).resolve().parents[2]
    code = """
import plugin.app as app
from plugin.bootstrap import BootstrapState
from plugin.framework.errors import WriterAgentException
import sys
app._finish = None
try:
    app.bootstrap(None)
except WriterAgentException as e:
    assert e.code == "BOOTSTRAP_FAILED"
else:
    raise SystemExit("expected WriterAgentException")
assert "plugin.main" not in sys.modules
from plugin import bootstrap as bootstrap_mod
assert not bootstrap_mod.bootstrap_initialized()
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_app_py_does_not_import_plugin_main():
    app_path = Path(__file__).resolve().parents[2] / "plugin" / "app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"))

    def walk(node: ast.AST) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "plugin.main", f"import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "plugin.main", f"from {node.module}"
            if node.module and node.module.startswith("plugin.main"):
                raise AssertionError(f"from {node.module}")
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)


def test_is_dev_build_eq_one_only(monkeypatch):
    from plugin.framework import dev_build

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    assert dev_build.is_dev_build() is False
    monkeypatch.setenv("WRITERAGENT_TESTING", "0")
    assert dev_build.is_dev_build() is False
    monkeypatch.setenv("WRITERAGENT_TESTING", "true")
    assert dev_build.is_dev_build() is False
    monkeypatch.setenv("WRITERAGENT_TESTING", "")
    assert dev_build.is_dev_build() is False
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    assert dev_build.is_dev_build() is True


def test_register_action_handler_stores_handler_callable_with_frame():
    from plugin.framework.main_shared import _ACTION_HANDLERS, get_action_handler, register_action_handler

    calls: list[Any] = []

    def sample(frame=None):
        calls.append(frame)

    register_action_handler("test", "frame_arg", sample)
    try:
        handler = get_action_handler("test.frame_arg")
        assert handler is sample
        handler()
        frame = object()
        handler(frame)
        assert calls == [None, frame]
    finally:
        _ACTION_HANDLERS.pop("test.frame_arg", None)
