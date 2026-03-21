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
"""AI chat sidebar module."""

import logging

from plugin.framework.module_base import ModuleBase

log = logging.getLogger("writeragent.chatbot")


class ChatbotModule(ModuleBase):
    """Registers the chatbot sidebar and its tool adapter."""

    def initialize(self, services):
        self._services = services
        self._routes_registered = False
        self._api_handler = None

        from . import web_research
        services.tools.auto_discover(web_research)

        # Chat tool routing is now handled natively by main.py's get_tools() instead of ChatToolAdapter
        self._adapter = None

        # Always register API routes (legacy Chat API) when http_routes is available.
        # The old chatbot.api_enabled toggle was removed from the manifest, so the
        # routes are now unconditionally enabled for the HTTP server.
        self._register_routes(services)

    def _register_routes(self, services):
        routes = services.get("http_routes")
        if not routes:
            log.warning("http_routes service not available")
            return

        try:
            from plugin.modules.chatbot.handler import ChatApiHandler
            self._api_handler = ChatApiHandler(services)
            routes.add("POST", "/api/chat",
                       self._api_handler.handle_chat, raw=True)
            routes.add("GET", "/api/chat",
                       self._api_handler.handle_history)
            routes.add("DELETE", "/api/chat",
                       self._api_handler.handle_reset)
            routes.add("GET", "/api/providers",
                       self._api_handler.handle_providers)
            self._routes_registered = True
            log.info("Chat API routes registered")
        except Exception as exc:  # ImportError, AttributeError, or route add failure
            log.info(
                "Chat API handler not available; skipping /api/chat routes: %s",
                exc,
            )
            self._api_handler = None

    def _unregister_routes(self, services):
        routes = services.get("http_routes")
        if routes:
            for method, path in [
                ("POST", "/api/chat"),
                ("GET", "/api/chat"),
                ("DELETE", "/api/chat"),
                ("GET", "/api/providers"),
            ]:
                try:
                    routes.remove(method, path)
                except Exception:
                    pass
        self._routes_registered = False
        log.info("Chat API routes unregistered")

    def get_adapter(self):
        """Return the ChatToolAdapter for use by the panel factory."""
        return self._adapter

    # ── Action dispatch ──────────────────────────────────────────────

    def on_action(self, action):
        if action == "extend_selection":
            from plugin.modules.chatbot.selection import action_extend_selection
            action_extend_selection(self._services)
        elif action == "edit_selection":
            from plugin.modules.chatbot.selection import action_edit_selection
            action_edit_selection(self._services)
        else:
            super().on_action(action)
