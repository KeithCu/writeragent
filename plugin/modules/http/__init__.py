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

from plugin.framework.module_base import ModuleBase

log = logging.getLogger("writeragent.http")


class HttpModule(ModuleBase):
    """Manages the shared HTTP server and route registry.

    Other modules (chatbot, doc) register routes via the
    ``http_routes`` service during their initialize() phase.
    This module also handles the MCP (Model Context Protocol) 
    JSON-RPC routes if enabled.
    This module starts the server in start_background() (phase 2b).
    """

    def initialize(self, services):
        from plugin.modules.http.routes import HttpRouteRegistry

        self._registry = HttpRouteRegistry()
        services.register("http_routes", self._registry)
        self._server = None
        self._services = services
        self._mcp_protocol = None
        self._mcp_routes_registered = False

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

    def start_background(self, services):
        if services.config.proxy_for(self.name).get("enabled"):
            self._start_server(services)

    def _on_config_changed(self, **data):
        key = data.get("key", "")
        if not key.startswith("http."):
            return
        cfg = self._services.config.proxy_for(self.name)

        if key == "http.enabled":
            enabled = cfg.get("enabled")
            if enabled and not self._server:
                self._start_server(self._services)
            elif not enabled and self._server:
                self._stop_server()
        elif key == "http.mcp_enabled":
            enabled = cfg.get("mcp_enabled")
            log.info("Config changed: http.mcp_enabled=%s", enabled)
            if enabled and not self._mcp_routes_registered:
                self._register_mcp_routes(self._services)
            elif not enabled and self._mcp_routes_registered:
                self._unregister_mcp_routes(self._services)

    def _start_server(self, services):
        from plugin.modules.http.server import HttpServer

        cfg = services.config.proxy_for(self.name)
        event_bus = getattr(services, "events", None)

        self._server = HttpServer(
            route_registry=self._registry,
            port=cfg.get("port") or cfg.get("mcp_port") or 8765,
            host=cfg.get("host") or "localhost",
            use_ssl=cfg.get("use_ssl") or False,
            ssl_cert=cfg.get("ssl_cert") or "",
            ssl_key=cfg.get("ssl_key") or "",
        )
        try:
            self._server.start()
            if event_bus:
                status = self._server.get_status()
                event_bus.emit("http:server_started",
                               port=status["port"], host=status["host"],
                               url=status["url"])
            if event_bus:
                event_bus.emit("menu:update")
        except Exception:
            log.exception("Failed to start HTTP server")
            self._server = None

    def _stop_server(self):
        if self._server:
            self._server.stop()
            self._server = None
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
        if action == "toggle_server":
            if self._server and self._server.is_running():
                return "Stop HTTP Server"
            return "Start HTTP Server"
        return None

    def get_menu_icon(self, action):
        running = self._server and self._server.is_running()
        if action == "toggle_server":
            # Show target state: "start" icon when stopped, "stop" icon when running
            return "stopped" if running else "running"
        if action == "server_status":
            return "running" if running else "stopped"
        return None

    def _action_toggle_server(self):
        from plugin.framework.dialogs import msgbox
        from plugin.framework.uno_context import get_ctx

        ctx = get_ctx()
        if self._server and self._server.is_running():
            log.info("Stopping HTTP server via toggle")
            self._stop_server()
            msgbox(ctx, "WriterAgent", "HTTP server stopped")
        else:
            log.info("Starting HTTP server via toggle")
            self._start_server(self._services)
            if self._server and self._server.is_running():
                status = self._server.get_status()
                msgbox(ctx, "WriterAgent",
                       "HTTP server started\n%s" % status.get("url", ""))
            else:
                msgbox(ctx, "WriterAgent",
                       "HTTP server failed to start\nCheck ~/localwriter.log")

    def _action_server_status(self):
        from plugin.framework.dialogs import msgbox, add_dialog_label, add_dialog_edit, add_dialog_button
        from plugin.framework.uno_context import get_ctx

        ctx = get_ctx()
        if not self._server:
            msgbox(ctx, "WriterAgent", "HTTP server is not running")
            return

        status = self._server.get_status()
        running = status.get("running", False)
        if not running:
            msgbox(ctx, "WriterAgent", "HTTP server not running")
            return

        url = status.get("url", "?")
        routes = status.get("routes", 0)
        msg = "HTTP server running\nRoutes: %d" % routes

        try:
            smgr = ctx.ServiceManager

            dlg_model = smgr.createInstanceWithContext(
                "com.sun.star.awt.UnoControlDialogModel", ctx)
            dlg_model.Title = "Server Status"
            dlg_model.Width = 230
            dlg_model.Height = 80

            add_dialog_label(dlg_model, "Msg", msg, 10, 6, 210, 24)

            # Read-only textfield for the URL — user can select + Ctrl+C
            add_dialog_edit(dlg_model, "UrlField", url, 10, 34, 210, 14, readonly=True)

            add_dialog_button(dlg_model, "OKBtn", "OK", 170, 58, 50, 14, push_button_type=1)

            dlg = smgr.createInstanceWithContext(
                "com.sun.star.awt.UnoControlDialog", ctx)
            dlg.setModel(dlg_model)
            toolkit = smgr.createInstanceWithContext(
                "com.sun.star.awt.Toolkit", ctx)
            dlg.createPeer(toolkit, None)
            dlg.execute()
            dlg.dispose()
        except Exception:
            log.exception("Status dialog error")
            msgbox(ctx, "WriterAgent", "%s\nURL: %s" % (msg, url))

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
