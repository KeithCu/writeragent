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
from typing import ClassVar, Tuple

from plugin.acp.acp_backend import ACPBackend


class GrokBackend(ACPBackend):
    """ACP-based xAI Grok Build backend. Official CLI: ``grok --no-auto-update agent stdio``."""

    backend_id: ClassVar[str] = "grok"
    default_extra_args: Tuple[str, ...] = ("--no-auto-update", "agent", "stdio")
    _extra_args: list[str]

    def get_binary_name(self) -> str:
        """Primary executable for PATH lookup (``grok agent stdio`` is the supported install)."""
        return "grok"

    def get_display_name(self) -> str:
        """Return display name for UI."""
        return "Grok Build"

    def get_agent_name(self) -> str:
        """Return ACP agent name."""
        return "grok"

    def _binary_matches(self, path: str) -> bool:
        """Official and versioned names (grok, grok-cli, grok.exe) share these args."""
        return os.path.basename(path).lower().startswith("grok")
