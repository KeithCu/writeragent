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
"""External MCP servers: owns the ``mcp_client.*`` settings on the Integrations tab.

The client and ``mcp`` sub-agent domain live in ``plugin/mcp/client.py`` next
to WriterAgent's own MCP server. This module owns only the configuration schema
for Settings > Integrations.
"""

from __future__ import annotations

from plugin.framework.module_base import ModuleBase


class McpClientModule(ModuleBase):
    """Module manifest anchor for ``mcp_client.*`` settings.

    Remote servers are listed lazily on the first ``mcp`` domain delegation and
    cached in memory, so startup makes no network requests and a down server
    cannot delay or break LibreOffice.
    """
