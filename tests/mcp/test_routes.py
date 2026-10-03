# WriterAgent - HTTP route registry tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Route-table races: list_routes vs add/remove, and disable stops first."""

import threading
from unittest.mock import MagicMock

from plugin.mcp.routes import HttpRouteRegistry


def _noop(*_args, **_kwargs):
    return (200, {})


def test_list_routes_overlaps_add_and_remove():
    """GET / snapshots keys. A concurrent add/remove must not raise RuntimeError."""
    reg = HttpRouteRegistry()
    reg.add("GET", "/", _noop)
    reg.add("GET", "/health", _noop)
    errors: list[BaseException] = []
    stop = threading.Event()

    def mutate() -> None:
        i = 0
        while not stop.is_set():
            path = "/t%d" % (i % 32)
            reg.add("POST", path, _noop)
            reg.remove("POST", path)
            i += 1

    def read() -> None:
        while not stop.is_set():
            try:
                routes = reg.list_routes()
                reg.match("GET", "/health")
                if not isinstance(routes, list) or reg.route_count < 1:
                    raise AssertionError("route snapshot missing built-in routes")
            except Exception as exc:
                errors.append(exc)
                stop.set()

    threads = [threading.Thread(target=mutate)]
    threads.extend(threading.Thread(target=read) for _idx in range(4))
    for thread in threads:
        thread.start()
    threading.Event().wait(0.25)
    stop.set()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert errors == []
    assert ("GET", "/") in reg.list_routes()
    assert ("GET", "/health") in reg.list_routes()


def test_disabling_mcp_stops_server_before_removing_routes(monkeypatch):
    """Unregister used to pop routes while serve_forever was still running."""
    import plugin.mcp as mcp_mod

    order: list[str] = []
    mod = mcp_mod.McpModule.__new__(mcp_mod.McpModule)
    mod.name = "mcp"
    mod._mcp_routes_registered = True
    mod._mcp_protocol = object()
    mod._srv_lock = threading.Lock()

    class _Registry:
        def remove(self, method: str, path: str) -> bool:
            order.append("remove")
            return True

    mod._registry = _Registry()

    server = MagicMock()
    server.is_running.return_value = True

    def stop() -> None:
        order.append("stop")
        server.is_running.return_value = False

    server.stop.side_effect = stop
    mod._server = server
    tunnel = MagicMock()
    mod._tunnel = tunnel

    monkeypatch.setattr(mcp_mod, "_shared_http_server", server)
    monkeypatch.setattr(mcp_mod, "_shared_tunnel", tunnel)
    monkeypatch.setattr(mcp_mod, "_primary_http_module", None)
    monkeypatch.setattr("plugin.mcp.reload_cors_policy_from_config", lambda *_a, **_k: None)

    proxy = MagicMock()
    proxy.get.side_effect = lambda key, default=None: {
        "mcp_enabled": False,
        "tunnel_enabled": False,
        "tunnel_provider": "cloudflare",
        "tunnel_provider_token": "",
        "mcp_port": 18765,
    }.get(key, default)
    services = MagicMock()
    services.config.proxy_for.return_value = proxy
    services.events = None
    mod._services = services

    mod._on_config_changed(key="mcp.mcp_enabled")

    assert "stop" in order
    assert "remove" in order
    assert order.index("stop") < order.index("remove")
    assert mod._mcp_routes_registered is False
    assert mod._mcp_protocol is None
