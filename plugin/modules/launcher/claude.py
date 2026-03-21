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
"""Claude Code launcher provider."""

import os
import json

from .base import BaseProvider


class ClaudeProvider(BaseProvider):
    name = "claude"
    label = "Claude Code"
    binary_name = "claude"
    install_url = "https://claude.ai/code"

    def setup_env(self, cwd, mcp_url):
        env = super().setup_env(cwd, mcp_url)

        # 1. Write .mcp.json (Claude Code specific)
        mcp_config = {
            "mcpServers": {
                "localwriter": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-http", mcp_url]
                }
            }
        }
        with open(os.path.join(cwd, ".mcp.json"), "w") as f:
            json.dump(mcp_config, f, indent=2)

        # 2. Setup .claude/settings.json
        dot_claude = os.path.join(cwd, ".claude")
        os.makedirs(dot_claude, exist_ok=True)
        with open(os.path.join(dot_claude, "settings.json"), "w") as f:
            json.dump({"mcp": {"enabled": True}}, f, indent=2)

        return env
