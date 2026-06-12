# Tests for scripts/prune_vendored_websockets.py

from __future__ import annotations

import os
import shutil
import sys

import pytest

from scripts.prune_vendored_websockets import prune_vendored_websockets


@pytest.fixture
def websockets_src():
    root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "vendor",
        "websockets",
    )
    if not os.path.isdir(root):
        pytest.skip("vendor/websockets not present — run make vendor")
    return root


@pytest.fixture
def pruned_websockets(tmp_path, websockets_src):
    dst = tmp_path / "websockets"
    shutil.copytree(websockets_src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    prune_vendored_websockets(str(dst))
    return dst


def test_prune_websockets_removes_dead_paths(pruned_websockets):
    root = pruned_websockets
    assert not (root / "legacy").is_dir()
    assert not (root / "sync").is_dir()
    assert not (root / "asyncio" / "server.py").is_file()
    assert not (root / "server.py").is_file()
    assert not (root / "cli.py").is_file()
    # Client stack must remain.
    assert (root / "asyncio" / "client.py").is_file()
    assert (root / "client.py").is_file()


def test_pruned_websockets_import_closure(pruned_websockets):
    lib_dir = str(pruned_websockets.parent)
    # Isolate from project/dev installs so we exercise the pruned tree only.
    env_path = [lib_dir]
    old_path = list(sys.path)
    old_modules = {
        k: v for k, v in sys.modules.items() if k == "websockets" or k.startswith("websockets.")
    }
    try:
        sys.path = env_path + [p for p in old_path if p not in env_path]
        for key in list(sys.modules):
            if key == "websockets" or key.startswith("websockets."):
                del sys.modules[key]

        import websockets
        from websockets.asyncio.client import ClientConnection
        from websockets.exceptions import WebSocketException

        _ = websockets.connect
        _ = ClientConnection
        _ = WebSocketException

        loaded = {k for k in sys.modules if k.startswith("websockets")}
        assert "websockets.asyncio.client" in loaded
        assert not any(k.startswith("websockets.legacy") for k in loaded)
        assert not any(k.startswith("websockets.sync") for k in loaded)
    finally:
        sys.path = old_path
        for key in list(sys.modules):
            if (key == "websockets" or key.startswith("websockets.")) and key not in old_modules:
                del sys.modules[key]
        sys.modules.update(old_modules)
