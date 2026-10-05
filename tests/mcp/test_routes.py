# WriterAgent - HTTP route registry tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Route table reads stay consistent while register/unregister mutates it."""

from __future__ import annotations

import threading

from plugin.mcp.routes import HttpRouteRegistry

_MCP_ROUTES = (
    ("POST", "/mcp"),
    ("GET", "/mcp"),
    ("DELETE", "/mcp"),
    ("POST", "/sse"),
    ("POST", "/messages"),
    ("GET", "/sse"),
    ("GET", "/debug"),
    ("POST", "/debug"),
)


def _handler(body, headers, query):
    del body, headers, query
    return (200, {})


def test_list_routes_snapshot_ignores_later_adds() -> None:
    registry = HttpRouteRegistry()
    registry.add("GET", "/health", _handler)
    snapshot = registry.list_routes()
    registry.add("GET", "/", _handler)
    assert snapshot == [("GET", "/health")]
    assert ("GET", "/") in registry.list_routes()


def test_list_routes_waits_for_the_whole_register_batch() -> None:
    """A reader blocked on the batch lock must see every route added inside it."""
    registry = HttpRouteRegistry()
    started = threading.Event()
    result: dict[str, list[tuple[str, str]]] = {}

    def reader() -> None:
        started.set()
        result["routes"] = registry.list_routes()

    worker = threading.Thread(target=reader)
    try:
        with registry.batch():
            registry.add("GET", "/health", _handler)
            worker.start()
            assert started.wait(2)
            # started fires just before list_routes. With the lock held, the
            # reader cannot finish until this batch releases.
            worker.join(0.2)
            assert worker.is_alive()
            for method, path in _MCP_ROUTES:
                registry.add(method, path, _handler)
    finally:
        worker.join(2)
    assert not worker.is_alive()
    assert set(result["routes"]) == {("GET", "/health"), *_MCP_ROUTES}


def test_concurrent_get_during_toggle_does_not_raise() -> None:
    """GET / lists routes while toggle registers and unregisters the MCP set."""
    registry = HttpRouteRegistry()
    registry.add("GET", "/health", _handler)
    registry.add("GET", "/", _handler)
    base = {("GET", "/health"), ("GET", "/")}
    full = base | set(_MCP_ROUTES)
    stop = threading.Event()
    errors: list[BaseException] = []
    partial: list[set[tuple[str, str]]] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                snapshot = set(registry.list_routes())
                route = registry.match("GET", "/")
            except BaseException as exc:
                errors.append(exc)
                return
            if snapshot != base and snapshot != full:
                partial.append(snapshot)
                return
            if route is None:
                errors.append(RuntimeError("GET / missing during toggle"))
                return

    worker = threading.Thread(target=reader)
    worker.start()
    try:
        for _unused in range(40):
            with registry.batch():
                for method, path in _MCP_ROUTES:
                    registry.add(method, path, _handler)
            with registry.batch():
                for method, path in _MCP_ROUTES:
                    registry.remove(method, path)
    finally:
        stop.set()
        worker.join(2)
    assert not worker.is_alive()
    assert errors == []
    assert partial == []
    assert set(registry.list_routes()) == base


def test_mcp_module_toggle_keeps_route_snapshots_consistent(monkeypatch) -> None:
    """Register/unregister on McpModule is one locked batch, same as GET / sees."""
    from unittest.mock import MagicMock

    import plugin.mcp as mcp_mod
    from plugin.mcp import McpModule

    saved = (
        mcp_mod._primary_http_module,
        mcp_mod._shared_registry,
        mcp_mod._shared_http_server,
        mcp_mod._shared_tunnel,
    )
    with mcp_mod._http_peer_lock:
        mcp_mod._primary_http_module = None
        mcp_mod._shared_registry = None
        mcp_mod._shared_http_server = None
        mcp_mod._shared_tunnel = None

    services = MagicMock()
    services.config.proxy_for.return_value = {
        "mcp_enabled": False,
        "mcp_port": 18765,
        "host": "127.0.0.1",
        "use_ssl": False,
    }
    monkeypatch.setattr("plugin.mcp.reload_cors_policy_from_config", lambda *_a, **_k: None)

    mod = McpModule()
    mod.name = "mcp"
    try:
        mod.initialize(services)
        base = set(mod._registry.list_routes())
        mod._register_mcp_routes(services)
        full = set(mod._registry.list_routes())
        assert ("POST", "/mcp") in full
        assert ("GET", "/") in full

        stop = threading.Event()
        errors: list[BaseException] = []
        partial: list[set[tuple[str, str]]] = []

        def reader() -> None:
            while not stop.is_set():
                try:
                    snapshot = set(mod._registry.list_routes())
                except BaseException as exc:
                    errors.append(exc)
                    return
                if snapshot != base and snapshot != full:
                    partial.append(snapshot)
                    return

        worker = threading.Thread(target=reader)
        worker.start()
        try:
            for _unused in range(20):
                mod._unregister_mcp_routes(services)
                mod._register_mcp_routes(services)
        finally:
            stop.set()
            worker.join(2)
        assert not worker.is_alive()
        assert errors == []
        assert partial == []
        assert set(mod._registry.list_routes()) == full
    finally:
        try:
            mod.shutdown()
        except Exception:
            pass
        with mcp_mod._http_peer_lock:
            (
                mcp_mod._primary_http_module,
                mcp_mod._shared_registry,
                mcp_mod._shared_http_server,
                mcp_mod._shared_tunnel,
            ) = saved
