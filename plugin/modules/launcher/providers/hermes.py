"""Hermes Agent launcher provider."""

import os

from .base import BaseProvider


class HermesProvider(BaseProvider):
    name = "hermes"
    label = "Hermes Agent"
    binary_name = "hermes-agent"
    install_url = "https://github.com/project-hermes/hermes-agent"

    def setup_env(self, cwd, mcp_url):
        env = super().setup_env(cwd, mcp_url)
        hermes_home = self._find_hermes_home(cwd)
        if hermes_home:
            env["HERMES_HOME"] = hermes_home
            self._update_hermes_config(hermes_home, mcp_url)
        return env

    def _find_hermes_home(self, cwd):
        search_dirs = [cwd, os.path.dirname(cwd), os.path.expanduser("~/.hermes")]
        for d in search_dirs:
            if os.path.isdir(os.path.join(d, "config")):
                return d
        return None

    def _update_hermes_config(self, home, mcp_url):
        # Simplified: user manages main config; we just provide env.
        pass
