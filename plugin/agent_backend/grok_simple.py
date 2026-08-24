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
"""Grok Build agent backend using the shared ACPBackend base class."""

import os
from typing import Tuple

from plugin.agent_backend.acp_backend import ACPBackend


class GrokBackend(ACPBackend):
    """ACP-based xAI Grok Build backend. Official CLI: ``grok --no-auto-update agent stdio``."""

    backend_id = "grok"
    default_extra_args: Tuple[str, ...] = ("--no-auto-update", "agent", "stdio")

    def get_binary_name(self) -> str:
        """Primary executable for PATH lookup (``grok agent stdio`` is the supported install)."""
        return "grok"

    def get_display_name(self) -> str:
        """Return display name for UI."""
        return "Grok Build (ACP)"

    def get_agent_name(self) -> str:
        """Return ACP agent name."""
        return "grok"

    def _apply_default_extra_args(self) -> None:
        """Official and versioned names (grok, grok-cli, …) all take the same ACP args."""
        if self._extra_args:
            return
        defaults = self.get_default_extra_args()
        if not defaults or not self._binary_path:
            return
        if os.path.basename(self._binary_path).lower().startswith("grok"):
            self._extra_args = list(defaults)
