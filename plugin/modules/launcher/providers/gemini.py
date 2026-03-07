"""Gemini CLI launcher provider."""

import os
import json

from .base import BaseProvider


class GeminiProvider(BaseProvider):
    name = "gemini"
    label = "Gemini CLI"
    binary_name = "gemini"
    install_url = "https://github.com/google-gemini/gemini-cli"

    def setup_env(self, cwd, mcp_url):
        env = super().setup_env(cwd, mcp_url)
        settings = {"mcp_url": mcp_url}
        with open(os.path.join(cwd, "settings.json"), "w") as f:
            json.dump(settings, f, indent=2)
        return env
