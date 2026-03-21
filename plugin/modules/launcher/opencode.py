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
"""OpenCode CLI launcher provider."""

import os
import json

from .base import BaseProvider


class OpenCodeProvider(BaseProvider):
    name = "opencode"
    label = "OpenCode CLI"
    binary_name = "opencode"
    install_url = "https://github.com/keithcu/opencode"

    def setup_env(self, cwd, mcp_url):
        env = super().setup_env(cwd, mcp_url)

        from plugin.framework.config import get_api_config
        from plugin.framework.uno_context import get_ctx
        try:
            api_config = get_api_config(get_ctx())
            endpoint = api_config.get("endpoint", "http://localhost:11434/v1")
            model = api_config.get("model", "qwen2.5-coder:7b")
        except Exception:
            endpoint = "http://localhost:11434/v1"
            model = "qwen2.5-coder:7b"

        opencode_config = {
            "api_key": "ollama",
            "base_url": endpoint,
            "model": model,
            "mcp_url": mcp_url
        }
        with open(os.path.join(cwd, "opencode.json"), "w") as f:
            json.dump(opencode_config, f, indent=2)

        return env
