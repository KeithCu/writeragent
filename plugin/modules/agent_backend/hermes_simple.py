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
"""Hermes agent backend using the shared ACPBackend base class."""

import logging
import os
from typing import Dict

from plugin.modules.agent_backend.acp_backend import ACPBackend
from plugin.framework.config import get_config, get_api_key_for_endpoint

log = logging.getLogger(__name__)


class HermesBackend(ACPBackend):
    """ACP-based Hermes backend."""

    backend_id = "hermes"

    def get_binary_name(self) -> str:
        """Return the binary name to search for."""
        return "hermes-acp"

    def get_display_name(self) -> str:
        """Return display name for UI."""
        return "Hermes"

    def get_agent_name(self) -> str:
        """Return ACP agent name."""
        return "hermes"

    def get_env_vars(self) -> Dict[str, str]:
        """Return environment variables to pass to subprocess."""
        env = {}
        try:
            # Forward API key to Hermes if available
            endpoint = str(get_config(self._ctx, "ai.endpoint") or "")
            key = get_api_key_for_endpoint(self._ctx, endpoint)
            if key:
                env["OPENROUTER_API_KEY"] = key
                env["OPENAI_API_KEY"] = key
                log.info("Using OPENROUTER_API_KEY from general settings")
        except Exception:
            pass
        return env
