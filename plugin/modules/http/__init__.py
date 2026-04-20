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
"""HTTP server module — owns the HTTP server lifecycle."""

import logging
import threading
from typing import Any, cast

from plugin.framework.module_base import ModuleBase

log = logging.getLogger("writeragent.http")

# LibreOffice may call bootstrap() more than once (e.g. sidebar vs menu UNO contexts). Each run
# constructs a new HttpModule(), which would otherwise create a second registry and try to
# bind the same port. The first instance is canonical; later instances reuse its registry/server.
_primary_http_module: "HttpModule | None" = None
_shared_registry: Any = None
_shared_http_server: Any = None
_http_peer_lock = threading.Lock()


class HttpModule(ModuleBase):
    """Manages the shared HTTP server and route registry.

    Other modules (chatbot, doc) register routes via the
    ``http_routes`` service during their initialize() phase.
    This module also handles the MCP (Model Context Protocol) 
    JSON-RPC routes if enabled.
    This module starts the server in start_background() (phase 2b).
    """

    def initialize(self, services):
        global _primary_http_module, _shared_registry, _shared_http_server

        from plugin.modules.http.routes import HttpRouteRegistry

        with _http_peer_lock:
            if _primary_http_module is not None:
                # Second (or later) bootstrap in this process: share registry and server state.
                prim = _primary_http_module
                self._registry = _shared_registry
                self._server = _shared_http_server
                self._services = services
                self._mcp_protocol = prim._mcp_protocol
                self._mcp_routes_registered = prim._mcp_routes_registered
                self._srv_lock = prim._srv_lock
                services.register("http_routes", self._registry)
                log.info(
                    "HttpModule initialize: reusing primary HTTP/MCP (mcp_enabled=%s, server=%s)",
                    services.config.proxy_for(self.name).get("mcp_enabled"),
                    "running" if (_shared_http_server and _shared_http_server.is_running()) else "stopped",
                )
                return

            self._registry = HttpRouteRegistry()
            _shared_registry = self._registry
            services.register("http_routes", self._registry)
            self._server = None
            self._services = services
            self._mcp_protocol = None
            self._mcp_routes_registered = False
            self._srv_lock = threading.Lock()

            # Built-in endpoints
            self._registry.add("GET", "/health", self._handle_health)
            self._registry.add("GET", "/", self._handle_info)
            self._registry.add("GET", "/api/config", self._handle_config_get)
            self._registry.add("POST", "/api/config", self._handle_config_set)

            # MCP endpoints
            mcp_enabled = services.config.proxy_for(self.name).get("mcp_enabled")
            log.info("HttpModule initialize: mcp_enabled=%s", mcp_enabled)
            if mcp_enabled:
                self._register_mcp_routes(services)

            if hasattr(services, "events"):
                services.events.subscribe("config:changed", self._on_config_changed)

            _primary_http_module = self

    def _bound_http_server(self):
        """Server instance for this process: shared copy after primary starts, else this instance."""
        global _shared_http_server
        if _shared_http_server is not None:
            return _shared_http_server
        return self._server

    def start_background(self, services):
        # We start automatically if MCP is enabled.
        if services.config.proxy_for(self.name).get("mcp_enabled"):
            self._start_server(services)

    def _on_config_changed(self, **data):
        key = data.get("key", "")
        # Non-http keys: ignore. Per-key http.* below. Empty key = bulk save (e.g. Settings OK).
        if key and not key.startswith("http."):
            return
        # MCP lifecycle: explicit toggle, or bulk apply (Settings dialog does not pass key).
        if key and key not in ("http.mcp_enabled",) and key != "":
            return

        cfg = self._services.config.proxy_for(self.name)
        enabled = cfg.get("mcp_enabled")
        log.info("HTTP/MCP config sync (key=%r): mcp_enabled=%s", key or "(bulk)", enabled)
        if enabled and not self._mcp_routes_registered:
            self._register_mcp_routes(self._services)
        elif not enabled and self._mcp_routes_registered:
            self._unregister_mcp_routes(self._services)

        bound = self._bound_http_server()
        if enabled and not (bound and bound.is_running()):
            self._start_server(self._services)
        elif not enabled and bound:
            self._stop_server()

    def _start_server(self, services):
        global _shared_http_server
        from plugin.modules.http.server import HttpServer

        with self._srv_lock:
            bound = self._bound_http_server()
            if bound is not None and bound.is_running():
                return

            cfg = services.config.proxy_for(self.name)
            event_bus = getattr(services, "events", None)

            srv = HttpServer(
                route_registry=self._registry,
                port=cfg.get("port") or cfg.get("mcp_port") or 8765,
                host=cfg.get("host") or "localhost",
                use_ssl=cfg.get("use_ssl") or False,
                ssl_cert=cfg.get("ssl_cert") or "",
                ssl_key=cfg.get("ssl_key") or "",
            )
            try:
                srv.start()
                if event_bus:
                    status = srv.get_status()
                    event_bus.emit("http:server_started",
                                   port=status["port"], host=status["host"],
                                   url=status["url"])
                if event_bus:
                    event_bus.emit("menu:update")
                self._server = srv
                _shared_http_server = srv
            except Exception:
                log.exception("Failed to start HTTP server")
                try:
                    srv.stop()
                except Exception:
                    log.debug("HttpServer.stop after failed start", exc_info=True)

    def _stop_server(self):
        global _shared_http_server
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

    def shutdown(self):
        self._stop_server()
        if self._mcp_routes_registered:
            self._unregister_mcp_routes(self._services)

    def _register_mcp_routes(self, services):
        log.info("Registering MCP routes (SSE, /mcp, /debug)...")
        from plugin.modules.http.mcp_protocol import MCPProtocolHandler

        self._mcp_protocol = MCPProtocolHandler(services)
        p = self._mcp_protocol

        # MCP streamable-http (raw — JSON-RPC + custom headers + SSE)
        self._registry.add("POST", "/mcp", p.handle_mcp_post, raw=True)
        self._registry.add("GET", "/mcp", p.handle_mcp_sse, raw=True)
        self._registry.add("DELETE", "/mcp", p.handle_mcp_delete, raw=True)

        # Legacy SSE transport (raw — streaming)
        self._registry.add("POST", "/sse", p.handle_sse_post, raw=True)
        self._registry.add("POST", "/messages", p.handle_sse_post, raw=True)
        self._registry.add("GET", "/sse", p.handle_sse_stream, raw=True)

        # Debug (simple — returns dict, server handles JSON)
        self._registry.add("GET", "/debug", p.handle_debug_info)
        # Debug POST (raw — complex response handling)
        self._registry.add("POST", "/debug", p.handle_debug_post, raw=True)

        self._mcp_routes_registered = True
        log.info("MCP routes registered on HTTP server")

    def _unregister_mcp_routes(self, services):
        for method, path in [
            ("POST", "/mcp"), ("GET", "/mcp"), ("DELETE", "/mcp"),
            ("POST", "/sse"), ("POST", "/messages"), ("GET", "/sse"),
            ("GET", "/debug"), ("POST", "/debug"),
        ]:
            try:
                self._registry.remove(method, path)
            except Exception:
                pass
        self._mcp_routes_registered = False
        self._mcp_protocol = None
        log.info("MCP routes unregistered from HTTP server")

    # ── Action dispatch ──────────────────────────────────────────────

    def on_action(self, action):
        if action == "toggle_server":
            self._action_toggle_server()
        elif action == "server_status":
            self._action_server_status()
        else:
            super().on_action(action)

    def get_menu_text(self, action):
        from plugin.framework.i18n import _
        if action == "toggle_server":
            b = self._bound_http_server()
            if b and b.is_running():
                return _("Stop MCP Server")
            return _("Start MCP Server")
        return None

    def get_menu_icon(self, action):
        b = self._bound_http_server()
        running = b and b.is_running()
        if action == "toggle_server":
            # Show target state: "start" icon when stopped, "stop" icon when running
            return "stopped" if running else "running"
        if action == "server_status":
            return "running" if running else "stopped"
        return None

    def _action_toggle_server(self):
        from plugin.framework.dialogs import msgbox
        from plugin.framework.uno_context import get_ctx
        from plugin.framework.i18n import _

        ctx = get_ctx()
        b = self._bound_http_server()
        if b and b.is_running():
            log.info("Stopping MCP server via toggle")
            self._stop_server()
            msgbox(ctx, "WriterAgent", _("MCP server stopped"))
        else:
            log.info("Starting MCP server via toggle")
            self._start_server(self._services)
            b2 = self._bound_http_server()
            if b2 and b2.is_running():
                status = b2.get_status()
                msgbox(ctx, "WriterAgent",
                       _("MCP server started") + "\n{0}".format(status.get("url", "")))
            else:
                msgbox(ctx, "WriterAgent",
                       _("MCP server failed to start") + "\n" + _("Check ~/localwriter.log"))

    def _action_server_status(self):
        from plugin.framework.dialogs import msgbox, add_dialog_label, add_dialog_edit, add_dialog_button
        from plugin.framework.uno_context import get_ctx
        from plugin.framework.i18n import _

        ctx = get_ctx()
        b = self._bound_http_server()
        if not b:
            msgbox(ctx, "WriterAgent", _("MCP server is not running"))
            return

        status = b.get_status()
        running = status.get("running", False)
        if not running:
            msgbox(ctx, "WriterAgent", _("MCP server is not running"))
            return

        url = status.get("url", "?")
        routes = status.get("routes", 0)
        msg = _("MCP server running") + "\n" + _("Routes: {0}").format(routes)

        try:
            assert ctx is not None
            ctx_any = cast("Any", ctx)
            smgr = getattr(ctx_any, "ServiceManager", getattr(ctx_any, "getServiceManager", lambda: None)())
            assert smgr is not None
            sm_any = cast("Any", smgr)

            dlg_model = sm_any.createInstanceWithContext(
                "com.sun.star.awt.UnoControlDialogModel", ctx_any)
            dlg_model.Title = _("Server Status")
            dlg_model.Width = 230
            dlg_model.Height = 80

            add_dialog_label(dlg_model, "Msg", msg, 10, 6, 210, 24)

            # Read-only textfield for the URL — user can select + Ctrl+C
            add_dialog_edit(dlg_model, "UrlField", url, 10, 34, 210, 14, readonly=True)

            add_dialog_button(dlg_model, "OKBtn", _("OK"), 170, 58, 50, 14, push_button_type=1)

            dlg = sm_any.createInstanceWithContext(
                "com.sun.star.awt.UnoControlDialog", ctx_any)
            dlg.setModel(dlg_model)
            toolkit = sm_any.createInstanceWithContext(
                "com.sun.star.awt.Toolkit", ctx_any)
            dlg.createPeer(toolkit, None)
            dlg.execute()
            dlg.dispose()
        except Exception:
            log.exception("Status dialog error")
            msgbox(ctx, "WriterAgent", msg + "\n" + _("URL: {0}").format(url))

    # ---- Built-in route handlers ----

    def _handle_health(self, body, headers, query):
        from plugin.version import EXTENSION_VERSION
        return (200, {
            "status": "healthy",
            "server": "WriterAgent",
            "version": EXTENSION_VERSION,
        })

    def _handle_info(self, body, headers, query):
        log.info("Request: GET / (info) from %s", headers.get("User-Agent"))
        from plugin.version import EXTENSION_VERSION
        routes = self._registry.list_routes()
        return (200, {
            "name": "WriterAgent",
            "version": EXTENSION_VERSION,
            "description": "WriterAgent HTTP server",
            "routes": ["%s %s" % (m, p) for m, p in sorted(routes)],
        })

    def _handle_config_get(self, body, headers, query):
        """GET /api/config — read config values.

        Query params:
          ?key=ai_ollama.instances   → single key
          ?prefix=ai_ollama          → all keys with prefix
          (none)                     → all config
        """
        cfg = self._services.config

        key = (query.get("key") or [None])[0]
        if key:
            val = cfg.get(key)
            return (200, {"key": key, "value": val})

        module = (query.get("module") or [None])[0]
        prefix = (query.get("prefix") or [None])[0]
        all_config = cfg.get_dict()

        if module:
            p = module if module.endswith(".") else module + "."
            filtered = {k: v for k, v in all_config.items()
                        if k.startswith(p)}
            return (200, {"config": filtered})

        if prefix:
            filtered = {k: v for k, v in all_config.items()
                        if k.startswith(prefix)}
            return (200, {"config": filtered})

        return (200, {"config": all_config})

    def _handle_config_set(self, body, headers, query):
        """POST /api/config — write config values.

        Body: {"key": "value", ...}
        """
        if not body or not isinstance(body, dict):
            return (400, {"error": "Body must be a JSON object of key-value pairs"})

        cfg = self._services.config
        errors = []
        written = []
        for key, value in body.items():
            try:
                cfg.set(key, value)
                written.append(key)
            except Exception as e:
                errors.append({"key": key, "error": str(e)})

        result = {"written": written}
        if errors:
            result["errors"] = errors
            return (207, result)
        return (200, result)
