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
"""AI chat sidebar module.

Also hosts shared LibreOffice UNO UI helpers (`dialogs`, `dialog_views`, `settings_dialog`) used by bootstrap, MCP, and other modules—not only the sidebar deck.
"""

from importlib import import_module
import logging
from typing import Any

from plugin.framework.module_base import ModuleBase

log = logging.getLogger(__name__)

_TOOL_MODULES = (
    "web_research",
    "memory",
    "librarian",
    "brainstorming",
    "writing",
    "deep_research_session",
    "ppt_master",
)


class ChatbotModule(ModuleBase):
    """Registers the chatbot sidebar tools and selection actions."""

    def initialize(self, services: Any) -> None:
        self._register_selection_actions(services)
        for name in _TOOL_MODULES:
            try:
                services.tools.auto_discover(import_module(f"{__name__}.{name}"))
            except Exception:
                log.exception("chatbot tool import failed: %s", name)

    def _register_selection_actions(self, services: Any) -> None:
        """Hamburger passes the sidebar frame. The menubar calls these with none."""
        from plugin.chatbot.selection import action_edit_selection, action_extend_selection
        from plugin.framework.main_shared import register_action_handler

        def _extend(frame: Any = None) -> None:
            action_extend_selection(services, frame=frame)

        def _edit(frame: Any = None) -> None:
            action_edit_selection(services, frame=frame)

        register_action_handler("chatbot", "extend_selection", _extend)
        register_action_handler("chatbot", "edit_selection", _edit)
