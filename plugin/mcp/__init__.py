# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""HTTP server and MCP module — owns the HTTP server lifecycle and MCP routes."""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any

from plugin.framework.http_server import format_bind_failure
from plugin.framework.module_base import ModuleBase

if TYPE_CHECKING:
    from com.sun.star.awt import ActionEvent
from plugin.mcp.cors import reload_cors_policy_from_config
from plugin.mcp.server import is_port_in_use_error, mcp_endpoint_url
from plugin.mcp.tunnel import DEFAULT_PROVIDER, TunnelManager, provider_label, tunnel_status_line

log = logging.getLogger("writeragent.http")

# LibreOffice may call bootstrap() more than once (e.g. sidebar vs menu UNO contexts). Each run
# constructs a new McpModule(), which would otherwise create a second registry and try to
# bind the same port. The first instance is canonical; later instances reuse its registry/server.
_primary_http_module: "McpModule | None" = None
_shared_registry: Any = None
_shared_http_server: Any = None
_shared_tunnel: TunnelManager | None = None
_http_peer_lock = threading.Lock()

# Last failed start — module-level so peer McpModule instances (second bootstrap) see the same
# reason when Toggle/Status run on a non-primary instance. Cleared on successful start.
_last_start_error: BaseException | None = None
_last_start_host: str = "localhost"
_last_start_port: Any = None


def is_mcp_server_running() -> bool:
    """Return True if the shared HTTP/MCP server is actively running."""
    server = _shared_http_server
    return bool(server and getattr(server, "is_running", lambda: False)())


_DEBUG_TOKEN_FILENAME = "writeragent_mcp_debug_token"

MCP_ROUTE_TABLE: tuple[tuple[str, str, str, bool], ...] = (
    ("POST", "/mcp", "handle_mcp_post", True),
    ("GET", "/mcp", "handle_mcp_sse", True),
    ("DELETE", "/mcp", "handle_mcp_delete", True),
    ("POST", "/sse", "handle_sse_post", True),
    ("POST", "/messages", "handle_sse_post", True),
    ("GET", "/sse", "handle_sse_stream", True),
    ("GET", "/debug", "handle_debug_info", False),
    ("POST", "/debug", "handle_debug_post", True),
)


def _debug_token_path() -> str | None:
    try:
        from plugin.framework.config import get_config_file_path

        cfg_path = get_config_file_path()
        if cfg_path:
            return os.path.join(os.path.dirname(str(cfg_path)), _DEBUG_TOKEN_FILENAME)
    except Exception:
        log.debug("debug token path lookup failed", exc_info=True)
    return None


def _write_debug_token_file(token: str) -> None:
    if os.environ.get("WRITERAGENT_TESTING"):
        return
    token_path = _debug_token_path()
    if not token_path:
        return
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(token_path, flags, 0o600)
        with open(fd, "w", encoding="utf-8") as f:
            f.write(token)
    except OSError:
        log.exception("Failed to write debug token file")


def _remove_debug_token_file() -> None:
    token_path = _debug_token_path()
    if not token_path:
        return
    try:
        if os.path.exists(token_path):
            os.remove(token_path)
    except OSError:
        pass


def format_mcp_start_failure(host: str, port: int | str, exc: BaseException) -> str:
    """Short user-facing body for MCP/HTTP start failures (no full traceback).

    Always includes host:port and the exception line. Port conflicts get the same
    guidance as the bind log so the dialog is actionable without opening the debug log.
    """
    return format_bind_failure(host, port, exc, service_name="MCP")


class McpModule(ModuleBase):
    """Manages the shared HTTP server and route registry.

    Route registrations are managed by McpModule (the MCP JSON-RPC routes,
    SSE streams, and debug endpoints). This module starts the server in
    start_background().
    """

    # Declared here so mypy can type the second-bootstrap reuse path
    # (prim._tunnel) after initialize() gained annotations.
    _registry: Any
    _server: Any
    _tunnel: TunnelManager | None = None
    _services: Any
    _mcp_routes_registered: bool
    _srv_lock: threading.Lock

    def initialize(self, services: Any) -> None:
        global _primary_http_module, _shared_registry, _shared_http_server, _shared_tunnel

        from plugin.mcp.routes import HttpRouteRegistry

        with _http_peer_lock:
            if _primary_http_module is not None:
                # Second (or later) bootstrap in this process: share registry and server state.
                prim = _primary_http_module
                self._registry = _shared_registry
                self._server = _shared_http_server
                self._tunnel = _shared_tunnel or prim._tunnel
                self._services = services
                self._mcp_routes_registered = prim._mcp_routes_registered
                self._srv_lock = prim._srv_lock
                services.register("http_routes", self._registry)
                log.info("McpModule initialize: reusing primary HTTP/MCP (mcp_enabled=%s, server=%s)", services.config.proxy_for(self.name).get("mcp_enabled"), "running" if (_shared_http_server and _shared_http_server.is_running()) else "stopped")
                return

            self._registry = HttpRouteRegistry()
            _shared_registry = self._registry
            services.register("http_routes", self._registry)
            self._server = None
            self._tunnel = TunnelManager()
            _shared_tunnel = self._tunnel
            self._services = services
            self._mcp_routes_registered = False
            self._srv_lock = threading.Lock()

            # Built-in endpoints
            self._registry.add("GET", "/health", self._handle_health)
            self._registry.add("GET", "/", self._handle_info)

            # MCP endpoints
            mcp_enabled = services.config.proxy_for(self.name).get("mcp_enabled")
            log.info("McpModule initialize: mcp_enabled=%s", mcp_enabled)
            if mcp_enabled:
                self._register_mcp_routes(services)

            reload_cors_policy_from_config(services)

            if hasattr(services, "events"):
                services.events.subscribe("config:changed", self._on_config_changed)

            _primary_http_module = self

    def _bound_http_server(self) -> Any:
        """Server instance for this process: shared copy after primary starts, else this instance."""
        global _shared_http_server
        if _shared_http_server is not None:
            return _shared_http_server
        return self._server

    def start_background(self, services: Any) -> None:
        # We start automatically if MCP is enabled.
        if services.config.proxy_for(self.name).get("mcp_enabled"):
            self._start_server(services)

    def _on_config_changed(self, **data: Any) -> None:
        key = data.get("key", "")
        prefix = f"{self.name}."
        # Ignore keys owned by other modules; empty key = bulk save (e.g. Settings OK).
        if key and not key.startswith(prefix):
            return
        toggle_key = f"{prefix}mcp_enabled"
        tunnel_key = f"{prefix}tunnel_enabled"
        tunnel_provider_key = f"{prefix}tunnel_provider"
        tunnel_provider_token_key = f"{prefix}tunnel_provider_token"
        cors_list_key = f"{prefix}cors_allowed_origins"
        cors_private_key = f"{prefix}cors_allow_private_origins"
        port_key = f"{prefix}mcp_port"
        # MCP lifecycle: toggle, port, tunnel, CORS policy keys, or bulk apply (Settings OK).
        # mcp.mcp_port is a lifecycle key. A port-only Settings save emits
        # key="mcp.mcp_port" (ConfigStore uses "" only when more than one key
        # changes). Omitting it left HttpServer on the old port. Rebind when
        # the port differs.
        if key and key not in (toggle_key, port_key, tunnel_key, tunnel_provider_key, tunnel_provider_token_key, cors_list_key, cors_private_key):
            return

        reload_cors_policy_from_config(self._services)

        cfg = self._services.config.proxy_for(self.name)
        enabled = cfg.get("mcp_enabled")
        log.info("HTTP/MCP config sync (key=%r): mcp_enabled=%s tunnel_enabled=%s tunnel_provider=%s", key or "(bulk)", enabled, cfg.get("tunnel_enabled"), cfg.get("tunnel_provider") or DEFAULT_PROVIDER)
        if enabled and not self._mcp_routes_registered:
            self._register_mcp_routes(self._services)
        elif not enabled and self._mcp_routes_registered:
            self._unregister_mcp_routes(self._services)

        bound = self._bound_http_server()
        running = bool(bound and bound.is_running())
        cfg_port = cfg.get("mcp_port")
        running_port = getattr(bound, "port", None) if bound is not None else None
        # Bulk saves (key="") already reach this handler, but a running listener
        # used to be left in place. Rebind whenever the live port disagrees.
        port_changed = running and isinstance(cfg_port, int) and not isinstance(cfg_port, bool) and running_port != cfg_port
        if enabled and port_changed:
            log.info("MCP port changed from %s to %s; rebinding HTTP server", running_port, cfg_port)
            self._stop_server()
            ok = self._start_server(self._services)
            # Port-only saves are user-initiated (key is mcp.mcp_port, not "").
            # Toggle uses mcp_enabled and shows its own dialog — skip that key.
            if not ok and (not key or key == port_key):
                self._show_start_failure_dialog(data.get("ctx"))
            return

        if enabled and not running:
            ok = self._start_server(self._services)
            # Settings OK emits bulk config:changed with an empty key. Show the failure there
            # (user-initiated). Toggle uses mcp_enabled key then shows its own dialog — skip
            # that key here to avoid a double msgbox. Bootstrap never goes through this path.
            if not ok and not key:
                self._show_start_failure_dialog(data.get("ctx"))
        elif not enabled and bound:
            self._stop_server()

        # Tunnel-only toggles (or bulk save) while the server is already up.
        self._sync_tunnel()

    def _clear_start_failure(self) -> None:
        global _last_start_error, _last_start_host, _last_start_port
        _last_start_error = None
        _last_start_host = "localhost"
        _last_start_port = None

    def _record_start_failure(self, host: str, port: Any, exc: BaseException) -> None:
        global _last_start_error, _last_start_host, _last_start_port
        _last_start_error = exc
        _last_start_host = host or "localhost"
        _last_start_port = port

    def _formatted_start_failure(self) -> str:
        if _last_start_error is None:
            return ""
        port = _last_start_port if _last_start_port is not None else "?"
        return format_mcp_start_failure(_last_start_host, port, _last_start_error)

    def _start_failure_reportable(self) -> bool:
        # Port conflicts are local config, not product bugs — don't nudge a GitHub report.
        if _last_start_error is None:
            return True
        return not is_port_in_use_error(_last_start_error)

    def _show_start_failure_dialog(self, ctx: Any | None = None) -> None:
        from plugin.chatbot.dialogs import msgbox_with_report
        from plugin.framework.i18n import _
        from plugin.framework.uno_context import get_ctx

        if ctx is None:
            ctx = get_ctx()
        detail = self._formatted_start_failure()
        if detail:
            message = _("MCP server failed to start") + "\n" + detail
        else:
            message = _("MCP server failed to start") + "\n" + _("Check writeragent_debug.log in your LibreOffice user config folder")
        msgbox_with_report(ctx, "WriterAgent", message, box_type=3, reportable=self._start_failure_reportable(), report_title="MCP server failed to start", report_extra=detail)

    def _start_server(self, services: Any) -> bool:
        import os

        if os.environ.get("WRITERAGENT_TESTING"):
            return True

        global _shared_http_server
        from plugin.mcp.server import HttpServer

        reload_cors_policy_from_config(services)

        started_ok = False
        with self._srv_lock:
            bound = self._bound_http_server()
            if bound is not None and bound.is_running():
                self._clear_start_failure()
                started_ok = True
            else:
                cfg = services.config.proxy_for(self.name)
                event_bus = getattr(services, "events", None)
                host = cfg.get("host") or "localhost"
                port = cfg.get("mcp_port")
                # Mock UNO/config (e.g. generate_tool_proxies) yields MagicMock host/port.
                if not isinstance(host, str) or not isinstance(port, int):
                    log.debug("MCP start skipped: host/port not usable (%r, %r)", host, port)
                    return False

                # Schema default is mcp/module.yaml mcp_port; ConfigService supplies it when unset.
                srv = HttpServer(route_registry=self._registry, port=port, host=host, use_ssl=cfg.get("use_ssl") or False, ssl_cert=cfg.get("ssl_cert") or "", ssl_key=cfg.get("ssl_key") or "")
                try:
                    srv.start()
                    if event_bus:
                        status = srv.get_status()
                        event_bus.emit("http:server_started", port=status["port"], host=status["host"], url=status["url"])
                    if event_bus:
                        event_bus.emit("menu:update")
                    self._server = srv
                    _shared_http_server = srv
                    self._clear_start_failure()
                    started_ok = True
                except Exception as e:
                    # Stash for Toggle/Status/Settings UI — previously only log.exception left a trail
                    # and the dialog said "check the debug log" with no host/port or bind reason (#379).
                    log.exception("Failed to start HTTP server")
                    self._record_start_failure(host, port, e)
                    try:
                        srv.stop()
                    except Exception:
                        log.debug("HttpServer.stop after failed start", exc_info=True)
                    return False

        # Outside _srv_lock — cloudflared spawn must not hold the HTTP start lock.
        if started_ok:
            self._sync_tunnel()
            return True
        return False

    def _stop_server(self) -> None:
        global _shared_http_server
        # Stop tunnel first so we do not keep advertising a dead local port.
        self._stop_tunnel()
        with self._srv_lock:
            srv = self._bound_http_server()
            if not srv:
                return
            srv.stop()
            self._server = None
            _shared_http_server = None
            if _primary_http_module is not None:
                _primary_http_module._server = None
        event_bus = getattr(self._services, "events", None)
        if event_bus:
            event_bus.emit("http:server_stopped", reason="shutdown")
            event_bus.emit("menu:update")

    def _bound_tunnel(self) -> TunnelManager | None:
        global _shared_tunnel
        if _shared_tunnel is not None:
            return _shared_tunnel
        return self._tunnel

    def _sync_tunnel(self) -> None:
        """Start or stop the public tunnel to match MCP settings."""
        tunnel = self._bound_tunnel()
        if tunnel is None:
            return
        cfg = self._services.config.proxy_for(self.name)
        bound = self._bound_http_server()
        want = bool(cfg.get("mcp_enabled") and cfg.get("tunnel_enabled") and bound and bound.is_running())
        if not want:
            tunnel.stop()
            return
        port = cfg.get("mcp_port")
        if port is None and bound is not None:
            port = getattr(bound, "port", None)
        if port is None:
            log.warning("tunnel_enabled but no MCP port available")
            return
        provider = cfg.get("tunnel_provider") or DEFAULT_PROVIDER
        provider_token = str(cfg.get("tunnel_provider_token") or "")
        port_i = int(port)
        # Armed before the thread is queued. stop() clears it, so a disable
        # that lands while this start is still waiting to run cannot lose.
        start_token = object()
        tunnel.note_pending_start(start_token)

        def _start(port: int = port_i, provider: str = str(provider), provider_token: str = provider_token, start_token: object = start_token) -> None:
            before = tunnel.last_error
            ok = tunnel.start(port, provider, provider_token=provider_token, start_token=start_token)
            # A superseded start (stop, or a newer arm) returns False without
            # a new last_error. Only a probe/spawn failure is worth logging.
            if not ok and tunnel.last_error and tunnel.last_error != before:
                log.error("Failed to start MCP public tunnel via %s (is the provider binary installed?)", provider)

        # config:changed runs this on the UI thread, and start() calls
        # binary_available() (subprocess, timeout 10s). A hung provider
        # --version would freeze Settings for that whole wait. Leave the UI
        # thread first. The probe also runs outside TunnelManager._lock so
        # stop() is not stuck behind it. dedicated so the wait does not
        # occupy the shared background pool.
        from plugin.framework.thread_guard import on_main_thread

        if on_main_thread():
            from plugin.framework.worker_pool import run_in_background

            run_in_background(_start, name="mcp-tunnel-sync", dedicated=True)
            return
        _start()

    def _stop_tunnel(self) -> None:
        tunnel = self._bound_tunnel()
        if tunnel is not None:
            tunnel.stop()

    def shutdown(self) -> None:
        self._stop_server()
        if self._mcp_routes_registered:
            self._unregister_mcp_routes(self._services)
        _remove_debug_token_file()

    def _register_mcp_routes(self, services: Any) -> None:
        log.info("Registering MCP routes (SSE, /mcp, /debug)...")
        from plugin.mcp.mcp_protocol import MCPProtocolHandler

        p = MCPProtocolHandler(services)
        if getattr(p, "debug_token", None):
            _write_debug_token_file(p.debug_token)

        # One lock across the whole set. GET / snapshots routes under the same
        # lock, so a toggle cannot expose a half-registered table or raise
        # RuntimeError while iterating it.
        with self._registry.batch():
            for method, path, handler_name, raw in MCP_ROUTE_TABLE:
                self._registry.add(method, path, getattr(p, handler_name), raw=raw)
            self._mcp_routes_registered = True
        log.info("MCP routes registered on HTTP server")

    def _unregister_mcp_routes(self, services: Any) -> None:
        with self._registry.batch():
            for method, path, _handler_name, _raw in MCP_ROUTE_TABLE:
                self._registry.remove(method, path)
            self._mcp_routes_registered = False
        _remove_debug_token_file()
        log.info("MCP routes unregistered from HTTP server")

    # ── Action dispatch ──────────────────────────────────────────────

    def on_action(self, action: str) -> None:
        if action == "toggle_server":
            self._action_toggle_server()
        elif action == "server_status":
            self._action_server_status()
        else:
            super().on_action(action)

    def get_menu_text(self, action: str) -> str | None:
        from plugin.framework.i18n import _

        if action == "toggle_server":
            b = self._bound_http_server()
            if b and b.is_running():
                return _("Stop MCP Server")
            return _("Start MCP Server")
        return None

    def get_menu_icon(self, action: str) -> str | None:
        if action != "server_status":
            return None
        b = self._bound_http_server()
        running = b and b.is_running()
        return "running" if running else "stopped"

    def _action_toggle_server(self) -> None:
        # No msgbox on success: modal boxes block the sidebar; the panel status line shows it.
        from plugin.framework.uno_context import get_ctx

        ctx = get_ctx()
        b = self._bound_http_server()
        if b and b.is_running():
            log.info("Stopping MCP server via toggle")
            self._stop_server()
        else:
            log.info("Starting MCP server via toggle")
            cfg = self._services.config.proxy_for(self.name)
            if not cfg.get("mcp_enabled"):
                cfg.set("mcp_enabled", True)
            elif not self._mcp_routes_registered:
                self._register_mcp_routes(self._services)
                self._start_server(self._services)
            else:
                self._start_server(self._services)
            b2 = self._bound_http_server()
            if not (b2 and b2.is_running()):
                self._show_start_failure_dialog(ctx)

    def _not_running_status_message(self) -> str:
        from plugin.framework.i18n import _

        msg = _("MCP server is not running")
        detail = self._formatted_start_failure()
        if detail:
            # One short reason: first line is host:port + exception; enough for Status.
            first = detail.split("\n", 1)[0]
            msg = msg + "\n" + first
        return msg

    def _action_server_status(self) -> None:
        from plugin.chatbot.dialogs import CopyListener, load_writeragent_dialog, msgbox
        from plugin.framework.uno_listeners import BaseActionListener
        from plugin.framework.uno_context import get_ctx
        from plugin.framework.i18n import _

        ctx = get_ctx()
        b = self._bound_http_server()
        if not b:
            msgbox(ctx, "WriterAgent", self._not_running_status_message())
            return

        status = b.get_status()
        running = status.get("running", False)
        if not running:
            msgbox(ctx, "WriterAgent", self._not_running_status_message())
            return

        local_url = status.get("mcp_url", status.get("url", "?"))
        routes = status.get("routes", 0)
        tunnel = self._bound_tunnel()
        public_url = tunnel.mcp_public_url() if tunnel else None
        cfg = self._services.config.proxy_for(self.name)
        provider = cfg.get("tunnel_provider") or DEFAULT_PROVIDER
        pname = provider_label(provider)
        tunnel_enabled = bool(cfg.get("tunnel_enabled"))

        # Primary copy URL is the public tunnel URL when active, else local URL
        active_url = public_url or local_url
        msg = _("MCP server running") + "\n" + _("Routes: {0}").format(routes)
        tunnel_line = tunnel_status_line(pname, tunnel, public_url, tunnel_enabled)
        fallback_msg = msg + "\n" + _("Local URL: {0}").format(local_url)
        if tunnel_line:
            fallback_msg = fallback_msg + "\n" + tunnel_line
        if public_url:
            fallback_msg = fallback_msg + "\n" + _("Public URL: {0}").format(public_url)

        try:
            assert ctx is not None
            dlg = load_writeragent_dialog("ServerStatusDialog", ctx)
            if dlg is None:
                msgbox(ctx, "WriterAgent", fallback_msg)
                return

            msg_ctrl = dlg.getControl("Msg")
            if msg_ctrl is not None:
                msg_ctrl.getModel().Label = msg

            local_lbl = dlg.getControl("LocalUrlLabel")
            if local_lbl is not None:
                local_lbl.getModel().Label = _("Local Endpoint:")

            url_ctrl = dlg.getControl("UrlField")
            if url_ctrl is not None:
                url_ctrl.setText(local_url)

            tunnel_lbl = dlg.getControl("TunnelLabel")
            tunnel_url_ctrl = dlg.getControl("TunnelUrlField")
            tunnel_status_ctrl = dlg.getControl("TunnelStatusMsg")

            if tunnel_enabled:
                if tunnel_lbl is not None:
                    tunnel_lbl.getModel().Label = _("Public Tunnel ({0}):").format(pname)
                    tunnel_lbl.getModel().Visible = True

                if tunnel_url_ctrl is not None:
                    tunnel_url_ctrl.setText(public_url or "")
                    tunnel_url_ctrl.getModel().Visible = True

                if tunnel_status_ctrl is not None:
                    if public_url:
                        tstatus = _("Status: Active (connected)")
                    elif tunnel and getattr(tunnel, "is_reconnecting", False):
                        tstatus = _("Status: Reconnecting — {0}").format(tunnel.last_error or _("retrying…"))
                    elif tunnel and tunnel.is_running:
                        tstatus = _("Status: Starting tunnel…")
                    elif tunnel and tunnel.last_error:
                        tstatus = _("Status: Failed — {0}").format(tunnel.last_error)
                    else:
                        tstatus = _("Status: Not running (is {0} binary installed?)").format(pname)
                    tunnel_status_ctrl.getModel().Label = tstatus
                    tunnel_status_ctrl.getModel().Visible = True
            else:
                for ctrl in (tunnel_lbl, tunnel_url_ctrl, tunnel_status_ctrl):
                    if ctrl is not None:
                        ctrl.getModel().Visible = False

            copy_btn = dlg.getControl("CopyBtn")
            if copy_btn is not None:
                copy_btn.addActionListener(CopyListener(dlg, ctx, active_url))

            class _OkListener(BaseActionListener):
                def actionPerformed(self, rEvent: ActionEvent) -> None:
                    dlg.endDialog(1)

            ok_btn = dlg.getControl("OKBtn")
            if ok_btn is not None:
                ok_btn.addActionListener(_OkListener())

            dlg.execute()
            dlg.dispose()
        except Exception:
            log.exception("Status dialog error")
            msgbox(ctx, "WriterAgent", fallback_msg)

    # ---- Built-in route handlers ----

    def _handle_health(self, body: Any, headers: Any, query: Any) -> tuple[int, dict[str, Any]]:
        from plugin.version import EXTENSION_VERSION

        return (200, {"status": "healthy", "server": "WriterAgent", "version": EXTENSION_VERSION})

    def _mcp_endpoint_from_config(self) -> str:
        cfg = self._services.config.proxy_for(self.name)
        return mcp_endpoint_url(cfg.get("host") or "localhost", cfg.get("mcp_port"), bool(cfg.get("use_ssl")))

    def _handle_info(self, body: Any, headers: Any, query: Any) -> tuple[int, dict[str, Any]]:
        log.info("Request: GET / (info) from %s", headers.get("User-Agent"))
        from plugin.version import EXTENSION_VERSION

        routes = self._registry.list_routes()
        info = {"name": "WriterAgent", "version": EXTENSION_VERSION, "description": "WriterAgent HTTP server", "routes": ["%s %s" % (m, p) for m, p in sorted(routes)]}
        if self._mcp_routes_registered:
            info["mcp_endpoint"] = self._mcp_endpoint_from_config()
        return (200, info)
