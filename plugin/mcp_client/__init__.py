# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
# Copyright (c) 2026 Anthony Scarola (MCP client)
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
"""External MCP servers: owns the mcp_client.* settings and registers the external_mcp chat tool.

The client itself lives in ``plugin/mcp/client.py`` next to WriterAgent's own
MCP server. This module owns the settings, which ``config_inline: mcp`` shows
as an "MCP Client" section on the Settings dialog's MCP tab.
"""

from __future__ import annotations

from typing import Any

from plugin.framework.module_base import ModuleBase


class McpClientModule(ModuleBase):
    """Registers the external_mcp tool. It reads mcp_client.* on each call and
    connects lazily, so startup makes no network requests and a server that is
    down cannot delay or break LibreOffice."""

    def initialize(self, services: Any) -> None:
        from plugin.mcp import client as mcp_client

        services.tools.auto_discover(mcp_client)
