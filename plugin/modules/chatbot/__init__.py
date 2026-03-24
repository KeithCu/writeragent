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

        from . import web_research
        # from .tools import memory, skills
        services.tools.auto_discover(web_research)
        # services.tools.auto_discover(memory)
        # services.tools.auto_discover(skills)        # Chat tool routing is now handled natively by main.py's get_tools() instead of ChatToolAdapter
        self._adapter = None

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
