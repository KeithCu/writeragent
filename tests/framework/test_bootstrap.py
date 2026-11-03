"""Unit tests for shared bootstrap owner resolution (pythonloader path)."""

from __future__ import annotations

import sys

from plugin.bootstrap import BootstrapState, resolve_bootstrap_owner


def test_resolve_bootstrap_owner_pythonloader_namespace():
    """pythonloader execs main.py as uno_component without sys.modules registration."""
    saved_main = sys.modules.pop("plugin.main", None)
    saved_uno = sys.modules.pop("uno_component", None)
    try:
        ns: dict = {
            "__name__": "uno_component",
            "__file__": "/tmp/ext/plugin/main.py",
            "_bootstrap_state": BootstrapState.UNINITIALIZED,
        }
        owner = resolve_bootstrap_owner(ns)

        assert owner._bootstrap_state is BootstrapState.UNINITIALIZED
        owner._bootstrap_state = BootstrapState.BOOTSTRAPPING
        assert ns["_bootstrap_state"] is BootstrapState.BOOTSTRAPPING
        assert sys.modules["uno_component"] is owner
        assert sys.modules["plugin.main"] is owner
    finally:
        sys.modules.pop("uno_component", None)
        if saved_main is not None:
            sys.modules["plugin.main"] = saved_main
        else:
            sys.modules.pop("plugin.main", None)
        if saved_uno is not None:
            sys.modules["uno_component"] = saved_uno


def test_resolve_bootstrap_owner_reuses_existing_module():
    """Normal import path: __name__ already in sys.modules."""
    existing = sys.modules["plugin.bootstrap"]
    owner = resolve_bootstrap_owner(
        {
            "__name__": "plugin.bootstrap",
            "__file__": existing.__file__,
            "_bootstrap_state": BootstrapState.UNINITIALIZED,
        }
    )
    assert owner is existing


def test_resolve_bootstrap_owner_main_core_canonical():
    saved_core = sys.modules.pop("plugin.main_core", None)
    saved_uno = sys.modules.pop("uno_component", None)
    try:
        ns: dict = {
            "__name__": "uno_component",
            "__file__": "/tmp/ext/plugin/main_core.py",
            "_bootstrap_state": BootstrapState.INITIALIZED,
        }
        owner = resolve_bootstrap_owner(ns)
        assert sys.modules["plugin.main_core"] is owner
        assert owner._bootstrap_state is BootstrapState.INITIALIZED
    finally:
        sys.modules.pop("uno_component", None)
        if saved_core is not None:
            sys.modules["plugin.main_core"] = saved_core
        else:
            sys.modules.pop("plugin.main_core", None)
        if saved_uno is not None:
            sys.modules["uno_component"] = saved_uno
