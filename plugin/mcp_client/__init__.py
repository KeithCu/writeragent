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
"""External MCP servers: owns the ``mcp_client.*`` schema and Integrations editor.

The client and the ``mcp`` sub-agent domain live in ``client.py``. This module owns
the ``mcp_client.*`` configuration schema and the in-place server editor
(``settings_ui.py``) for Settings > Integrations.
"""

from __future__ import annotations

from plugin.framework.module_base import ModuleBase


class McpClientModule(ModuleBase):
    """Module manifest anchor for ``mcp_client.*`` settings.

    Remote servers are listed lazily on the first ``mcp`` domain delegation and
    cached in memory, so startup makes no network requests and a down server
    cannot delay or break LibreOffice.
    """
