# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTP/MCP bind: single attempt, clear failure — no LibreOffice required."""
import socket

import pytest

from plugin.mcp.server import (
    HttpServer,
    _PORT_IN_USE_GUIDANCE,
    format_mcp_start_failure,
    is_port_in_use_error,
)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _EmptyRoutes:
    route_count = 0


def test_start_binds_on_free_port():
    last_error = None
    for attempt in range(3):
        port = _free_port()
        srv = HttpServer(route_registry=_EmptyRoutes(), port=port, host="127.0.0.1")
        try:
            srv.start()
            assert srv.is_running()
            return
        except OSError as exc:
            if not is_port_in_use_error(exc):
                raise
            last_error = exc
        finally:
            srv.stop()
    pytest.fail(
        f"Failed to start HttpServer after {attempt + 1} attempts due to port in use: {last_error}"
    )


def test_start_raises_immediately_when_port_busy(monkeypatch):
    # Persistent holder — must not sleep/retry (used to block LO bootstrap ~4s).
    occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupier.bind(("127.0.0.1", 0))
    occupier.listen(1)
    port = occupier.getsockname()[1]

    sleeps = {"n": 0}
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: sleeps.__setitem__("n", sleeps["n"] + 1))

    try:
        srv = HttpServer(route_registry=_EmptyRoutes(), port=port, host="127.0.0.1")
        with pytest.raises(OSError):
            srv.start()
        assert sleeps["n"] == 0
    finally:
        occupier.close()

def test_is_port_in_use_error_by_errno():
    assert is_port_in_use_error(OSError(98, "Address already in use"))
    assert is_port_in_use_error(OSError(48, "Address already in use"))
    err = OSError("busy")
    err.winerror = 10048
    assert is_port_in_use_error(err)
    assert not is_port_in_use_error(OSError(13, "Permission denied"))
    assert not is_port_in_use_error(RuntimeError("boom"))


def test_format_mcp_start_failure_port_in_use():
    msg = format_mcp_start_failure("localhost", 18765, OSError(98, "Address already in use"))
    assert "localhost:18765" in msg
    assert "OSError" in msg
    assert _PORT_IN_USE_GUIDANCE in msg
    assert "mcp.mcp_port" in msg


def test_format_mcp_start_failure_other_oserror():
    msg = format_mcp_start_failure("127.0.0.1", 9000, OSError(13, "Permission denied"))
    assert "127.0.0.1:9000" in msg
    assert "Permission denied" in msg
    assert _PORT_IN_USE_GUIDANCE not in msg


def test_start_server_stashes_last_start_error(monkeypatch):
    """Failed HttpServer.start must leave a reason for Toggle/Status (#379)."""
    import threading
    from unittest.mock import MagicMock

    import plugin.mcp as mcp_mod

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)

    services = MagicMock()
    proxy = MagicMock()
    proxy.get.side_effect = lambda k, d=None: {
        "mcp_port": 18765,
        "host": "localhost",
        "use_ssl": False,
        "ssl_cert": "",
        "ssl_key": "",
    }.get(k, d)
    services.config.proxy_for.return_value = proxy
    services.events = None

    mod = mcp_mod.McpModule.__new__(mcp_mod.McpModule)
    mod._registry = MagicMock()
    mod._srv_lock = threading.Lock()
    mod._server = None
    mod.name = "mcp"

    boom = OSError(98, "Address already in use")

    class _FailingServer:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise boom

        def stop(self):
            pass

    monkeypatch.setattr(mcp_mod, "_shared_http_server", None)
    monkeypatch.setattr(mcp_mod, "_last_start_error", None)
    monkeypatch.setattr("plugin.mcp.server.HttpServer", _FailingServer)
    monkeypatch.setattr("plugin.mcp.reload_cors_policy_from_config", lambda *_a, **_k: None)

    assert mod._start_server(services) is False
    assert mcp_mod._last_start_error is boom
    detail = mod._formatted_start_failure()
    assert "localhost:18765" in detail
    assert _PORT_IN_USE_GUIDANCE in detail
    assert mod._start_failure_reportable() is False



def test_mcp_module_does_not_register_api_config(monkeypatch):
    """GET/POST /api/config was removed — must not appear on the shared registry."""
    from unittest.mock import MagicMock

    import plugin.mcp as mcp_mod
    from plugin.mcp.routes import HttpRouteRegistry

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
    services.events = MagicMock()
    services.get.side_effect = lambda name: getattr(services, name, None)

    monkeypatch.setattr("plugin.mcp.reload_cors_policy_from_config", lambda *_a, **_k: None)

    mod = mcp_mod.McpModule()
    mod.name = "mcp"
    mod.initialize(services)

    assert not hasattr(mod, "_handle_config_get")
    assert not hasattr(mod, "_handle_config_set")
    routes = set(mod._registry.list_routes())
    assert ("GET", "/api/config") not in routes
    assert ("POST", "/api/config") not in routes
    assert ("GET", "/health") in routes
    assert isinstance(mod._registry, HttpRouteRegistry)

    mod.shutdown()


class _Cfg:
    def __init__(self, values: dict):
        self.values = dict(values)

    def get(self, key, default=None):
        return self.values.get(key, default)


class _Listener:
    """Stand-in for HttpServer: records the port it was asked to bind."""

    def __init__(self, port: int, fail: BaseException | None = None):
        self.port = port
        self._fail = fail
        self.running = False
        self.stop_count = 0

    def start(self) -> None:
        if self._fail is not None:
            raise self._fail
        self.running = True

    def stop(self) -> None:
        self.stop_count += 1
        self.running = False

    def is_running(self) -> bool:
        return self.running

    def get_status(self) -> dict:
        return {"port": self.port, "host": "127.0.0.1", "url": "http://127.0.0.1:%s" % self.port}


def _module_with_listener(monkeypatch, listener, cfg_values, *, fail_start: BaseException | None = None):
    """McpModule wired to a fake listener and config, without a live socket."""
    import threading
    from unittest.mock import MagicMock

    import plugin.mcp as mcp_mod

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    created: list[_Listener] = []

    def _factory(**kwargs):
        srv = _Listener(kwargs["port"], fail=fail_start)
        created.append(srv)
        return srv

    services = MagicMock()
    cfg = _Cfg(cfg_values)
    services.config.proxy_for.return_value = cfg
    services.events = MagicMock()

    mod = mcp_mod.McpModule.__new__(mcp_mod.McpModule)
    mod.name = "mcp"
    mod._services = services
    mod._registry = MagicMock()
    mod._srv_lock = threading.Lock()
    mod._server = listener
    mod._tunnel = None
    mod._mcp_routes_registered = True
    mod._mcp_protocol = None
    dialogs: list = []
    mod._show_start_failure_dialog = lambda ctx=None: dialogs.append(ctx)

    monkeypatch.setattr(mcp_mod, "_shared_http_server", listener)
    monkeypatch.setattr(mcp_mod, "_shared_tunnel", None)
    monkeypatch.setattr(mcp_mod, "_primary_http_module", mod)
    monkeypatch.setattr(mcp_mod, "_last_start_error", None)
    monkeypatch.setattr("plugin.mcp.server.HttpServer", _factory)
    monkeypatch.setattr("plugin.mcp.reload_cors_policy_from_config", lambda *_a, **_k: None)
    return mod, cfg, created, dialogs


def test_mcp_port_only_change_rebinds_running_server(monkeypatch):
    """Settings port-only save emits key=mcp.mcp_port, not bulk \"\"."""
    old = _Listener(18765)
    old.running = True
    mod, cfg, created, dialogs = _module_with_listener(
        monkeypatch,
        old,
        {"mcp_enabled": True, "mcp_port": 19000, "host": "127.0.0.1", "use_ssl": False, "tunnel_enabled": False},
    )
    mod._on_config_changed(key="mcp.mcp_port", value=19000)
    assert old.stop_count == 1
    assert old.is_running() is False
    assert len(created) == 1
    assert created[0].port == 19000
    assert created[0].is_running()
    assert mod._bound_http_server() is created[0]
    assert dialogs == []
    assert cfg.get("mcp_port") == 19000


def test_mcp_port_change_in_bulk_save_rebinds_running_server(monkeypatch):
    """Settings OK that changes the port plus other keys emits key=\"\"."""
    old = _Listener(18765)
    old.running = True
    mod, _cfg, created, dialogs = _module_with_listener(
        monkeypatch,
        old,
        {
            "mcp_enabled": True,
            "mcp_port": 20000,
            "host": "127.0.0.1",
            "use_ssl": False,
            "tunnel_enabled": True,
            "tunnel_provider": "cloudflare",
        },
    )
    mod._on_config_changed(key="", keys=("mcp.mcp_port", "mcp.tunnel_provider"))
    assert old.stop_count == 1
    assert len(created) == 1
    assert created[0].port == 20000
    assert created[0].is_running()
    assert dialogs == []


def test_mcp_port_unchanged_does_not_restart_listener(monkeypatch):
    old = _Listener(18765)
    old.running = True
    mod, _cfg, created, _dialogs = _module_with_listener(
        monkeypatch,
        old,
        {"mcp_enabled": True, "mcp_port": 18765, "host": "127.0.0.1", "use_ssl": False, "tunnel_enabled": False},
    )
    mod._on_config_changed(key="mcp.mcp_port", value=18765)
    assert old.stop_count == 0
    assert old.is_running()
    assert created == []


def test_unrelated_config_key_does_not_rebind_mcp_port(monkeypatch):
    old = _Listener(18765)
    old.running = True
    mod, _cfg, created, _dialogs = _module_with_listener(
        monkeypatch,
        old,
        {"mcp_enabled": True, "mcp_port": 19000, "host": "127.0.0.1", "use_ssl": False, "tunnel_enabled": False},
    )
    # Port in config already differs, but this key is not a lifecycle key.
    mod._on_config_changed(key="mcp.host", value="127.0.0.1")
    mod._on_config_changed(key="chatbot.max_tool_rounds", value=15)
    assert old.stop_count == 0
    assert old.is_running()
    assert created == []


def test_mcp_port_change_while_disabled_does_not_start(monkeypatch):
    mod, _cfg, created, dialogs = _module_with_listener(
        monkeypatch,
        None,
        {"mcp_enabled": False, "mcp_port": 19000, "host": "127.0.0.1", "use_ssl": False, "tunnel_enabled": False},
    )
    mod._on_config_changed(key="mcp.mcp_port", value=19000)
    assert created == []
    assert dialogs == []
    assert mod._bound_http_server() is None


def test_mcp_port_change_starts_when_enabled_but_stopped(monkeypatch):
    mod, _cfg, created, dialogs = _module_with_listener(
        monkeypatch,
        None,
        {"mcp_enabled": True, "mcp_port": 19000, "host": "127.0.0.1", "use_ssl": False, "tunnel_enabled": False},
    )
    mod._on_config_changed(key="mcp.mcp_port", value=19000)
    assert len(created) == 1
    assert created[0].port == 19000
    assert created[0].is_running()
    assert dialogs == []


def test_mcp_port_rebind_failure_stops_old_listener_and_reports(monkeypatch):
    old = _Listener(18765)
    old.running = True
    boom = OSError(98, "Address already in use")
    mod, _cfg, created, dialogs = _module_with_listener(
        monkeypatch,
        old,
        {"mcp_enabled": True, "mcp_port": 19000, "host": "127.0.0.1", "use_ssl": False, "tunnel_enabled": False},
        fail_start=boom,
    )
    mod._on_config_changed(key="mcp.mcp_port", value=19000, ctx="settings-ctx")
    assert old.stop_count == 1
    assert old.is_running() is False
    assert len(created) == 1
    assert created[0].is_running() is False
    assert mod._bound_http_server() is None
    assert dialogs == ["settings-ctx"]
    import plugin.mcp as mcp_mod

    assert mcp_mod._last_start_error is boom
