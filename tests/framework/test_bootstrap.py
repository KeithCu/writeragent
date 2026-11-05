"""Unit tests for shared bootstrap_extension (pythonloader exec namespace)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


from plugin.bootstrap import BootstrapState, bootstrap_extension
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
    bootstrap._bootstrap_state = BootstrapState.UNINITIALIZED
    bootstrap._bootstrap_error = None

    finish_calls = 0

    def finish(unused_ctx):
        nonlocal finish_calls
        finish_calls += 1
        bootstrap._bootstrap_state = BootstrapState.INITIALIZED

    bootstrap_extension(
        None,
        extension_id="org.example.test",
        finish=finish,
    )
    assert bootstrap._bootstrap_state is BootstrapState.INITIALIZED
    assert finish_calls == 1

    with patch("plugin.framework.config.init_config") as mock_init_config:
        bootstrap_extension(
            None,
            extension_id="org.example.test",
            finish=finish,
        )
        mock_init_config.assert_not_called()
    assert finish_calls == 1
    bootstrap._bootstrap_state = BootstrapState.UNINITIALIZED
    bootstrap._bootstrap_error = None


def test_bind_writeragent_first_call_wins(monkeypatch):
    import plugin.app as app
    import plugin.bootstrap as bootstrap

    _patch_bootstrap_deps(monkeypatch)
    prev_finish = app._finish
    prev_services = app._services
    prev_tools = app._tools
    prev_modules = list(app._modules)
    prev_state = bootstrap._bootstrap_state
    prev_err = bootstrap._bootstrap_error
    bootstrap._bootstrap_state = BootstrapState.UNINITIALIZED
    bootstrap._bootstrap_error = None
    app._finish = None
    app._services = None
    app._tools = None
    app._modules.clear()

    first_calls = 0
    second_calls = 0

    def first_finish(_ctx):
        nonlocal first_calls
        first_calls += 1
        bootstrap._bootstrap_state = BootstrapState.INITIALIZED

    def second_finish(_ctx):
        nonlocal second_calls
        second_calls += 1

    try:
        app.bind_writeragent(finish=first_finish)
        app.bind_writeragent(finish=second_finish)
        app.bootstrap(None)
        assert first_calls == 1
        assert second_calls == 0
        assert bootstrap._bootstrap_state is BootstrapState.INITIALIZED

        app.bootstrap(None)
        assert first_calls == 1
        assert second_calls == 0
    finally:
        app._finish = prev_finish
        app._services = prev_services
        app._tools = prev_tools
        app._modules.clear()
        app._modules.extend(prev_modules)
        bootstrap._bootstrap_state = prev_state
        bootstrap._bootstrap_error = prev_err


def test_unbound_bootstrap_raises_and_does_not_import_main():
    repo = Path(__file__).resolve().parents[2]
    # Clean subprocess has no tests/conftest UNO mocks. CI release-verify .venv
    # ships a com.sun.star stub that raises unless uno was imported first, and
    # may lack pyuno entirely. Install the same minimal stubs conftest uses so
    # plugin.app can import; the assertion is that unbound bootstrap fails
    # without loading plugin.main.
    code = """
import sys
import types
from unittest.mock import MagicMock

sys.modules.setdefault("uno", MagicMock())
if "unohelper" not in sys.modules:
    _uh = types.ModuleType("unohelper")
    class _Base:
        pass
    _uh.Base = _Base
    sys.modules["unohelper"] = _uh

def _ensure_star(name: str):
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    mod.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = mod
    return mod

_ensure_star("com")
_ensure_star("com.sun")
star = _ensure_star("com.sun.star")
for sub in ("task", "frame", "lang"):
    _ensure_star(f"com.sun.star.{sub}")
# Interfaces used at import by bootstrap / main_shared.
for iface in ("XJob", "XJobExecutor"):
    setattr(sys.modules["com.sun.star.task"], iface, type(iface, (), {}))
for iface in ("XDispatch", "XDispatchProvider", "DispatchDescriptor"):
    setattr(sys.modules["com.sun.star.frame"], iface, type(iface, (), {}))
for iface in ("XInitialization", "XServiceInfo"):
    setattr(sys.modules["com.sun.star.lang"], iface, type(iface, (), {}))

import plugin.app as app
from plugin.bootstrap import BootstrapState
from plugin.framework.errors import WriterAgentException
app._finish = None
try:
    app.bootstrap(None)
except WriterAgentException as e:
    assert e.code == "BOOTSTRAP_FAILED"
else:
    raise SystemExit("expected WriterAgentException")
assert "plugin.main" not in sys.modules
from plugin import bootstrap as bootstrap_mod
assert bootstrap_mod._bootstrap_state is BootstrapState.UNINITIALIZED
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
