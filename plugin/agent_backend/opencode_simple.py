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
"""OpenCode agent backend using the shared ACPBackend base class."""

from typing import Tuple

from plugin.agent_backend.acp_backend import ACPBackend


class OpenCodeBackend(ACPBackend):
    """ACP-based OpenCode backend (``opencode acp``)."""

    backend_id = "opencode"
    default_extra_args: Tuple[str, ...] = ("acp",)

    def get_binary_name(self) -> str:
        """Primary executable for PATH lookup (``opencode acp`` is the supported install)."""
        return "opencode"

    def get_display_name(self) -> str:
        """Return display name for UI."""
        return "OpenCode (ACP)"

    def get_agent_name(self) -> str:
        """Return ACP agent name."""
        return "opencode"
