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
"""Mistral Vibe agent backend using the shared ACPBackend base class."""

import logging
from typing import Dict

from plugin.agent_backend.acp_backend import ACPBackend
from plugin.framework.config import get_api_key_for_endpoint, get_current_endpoint

log = logging.getLogger(__name__)


class VibeBackend(ACPBackend):
    """ACP-based Mistral Vibe backend.

    ``contentBlocks`` on the prompt result is drained by base ``send()`` —
    the same block types as session/agent updates.
    """

    backend_id = "vibe"

    def get_binary_name(self) -> str:
        """Return the binary name to search for."""
        return "vibe-acp"

    def get_display_name(self) -> str:
        """Return display name for UI."""
        return "Mistral Vibe (ACP)"

    def get_agent_name(self) -> str:
        """Return ACP agent name."""
        return "vibe"

    def get_env_vars(self) -> Dict[str, str]:
        """Return environment variables to pass to subprocess."""
        env = {}
        try:
            # Forward API key to Vibe if available
            endpoint = str(get_current_endpoint() or "")
            key = get_api_key_for_endpoint(endpoint)
            if key:
                env["MISTRAL_API_KEY"] = key
                log.info("Using MISTRAL_API_KEY from general settings")
        except Exception:
            pass
        return env
