# Adapted from Nelson MCP
"""Gemini CLI provider for the launcher module."""

import json
import logging
import os

from plugin.framework.module_base import ModuleBase

log = logging.getLogger("localwriter.launcher.gemini")

_DEFAULT_CWD = os.path.join(
    os.path.expanduser("~"), ".local", "share", "localwriter", "cli", "gemini")


class GeminiProvider:
    """Gemini CLI — Google's AI coding CLI."""

    name = "gemini"
    label = "Gemini CLI"
    binary_name = "gemini"
    install_url = "https://github.com/google-gemini/gemini-cli"
    default_cwd = _DEFAULT_CWD

    def get_args(self, mcp_url, config):
        return []

    def setup_env(self, mcp_url, env, cwd, config):
        """Write settings.json into the working directory."""
        config_path = os.path.join(cwd, "settings.json")

        gemini_config = {
            "mcpServers": {
                "localwriter": {
                    "uri": mcp_url + "/sse",
                }
            }
        }

        try:
            with open(config_path, "w") as f:
                json.dump(gemini_config, f, indent=2)
            log.info("Wrote gemini config: %s", config_path)
        except Exception:
            log.exception("Failed to write gemini config")


def get_default_cwd(services):
    """Return the default working directory for Gemini CLI."""
    return _DEFAULT_CWD


def on_install():
    """Callback for the Install button in Options."""
    from plugin.modules.launcher import run_install_for_provider
    run_install_for_provider("gemini")


class GeminiModule(ModuleBase):

    def initialize(self, services):
        if hasattr(services, "launcher_manager"):
            services.launcher_manager.register_provider(
                "gemini", GeminiProvider())
