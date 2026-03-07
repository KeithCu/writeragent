# Adapted from Nelson MCP
"""Claude Code CLI provider for the launcher module."""

import json
import logging
import os
import shutil

from plugin.framework.module_base import ModuleBase

log = logging.getLogger("localwriter.launcher.claude")

_DEFAULT_CWD = os.path.join(
    os.path.expanduser("~"), ".local", "share", "localwriter", "cli", "claude")

# Directory containing prompt templates shipped with this module
_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


class ClaudeProvider:
    """Claude Code — Anthropic's AI coding CLI."""

    name = "claude"
    label = "Claude Code"
    binary_name = "claude"
    install_url = "https://docs.anthropic.com/en/docs/claude-code"
    default_cwd = _DEFAULT_CWD

    def get_args(self, mcp_url, config):
        return ["--resume"]

    def setup_env(self, mcp_url, env, cwd, config):
        """Write .mcp.json, CLAUDE.md, and skills into the working directory."""
        # 1. .mcp.json — MCP server config
        config_path = os.path.join(cwd, ".mcp.json")
        mcp_config = {
            "mcpServers": {
                "localwriter": {
                    "type": "http",
                    "url": mcp_url + "/",
                }
            }
        }
        try:
            with open(config_path, "w") as f:
                json.dump(mcp_config, f, indent=2)
            log.info("Wrote claude config: %s", config_path)
        except Exception:
            log.exception("Failed to write claude config")

        # 2. .claude/settings.json — auto-approve all localwriter MCP tools
        settings_dir = os.path.join(cwd, ".claude")
        os.makedirs(settings_dir, exist_ok=True)
        settings_path = os.path.join(settings_dir, "settings.json")
        settings = {
            "permissions": {
                "allow": ["mcp__localwriter__*"],
            }
        }
        try:
            with open(settings_path, "w") as f:
                json.dump(settings, f, indent=2)
            log.info("Wrote claude settings: %s", settings_path)
        except Exception:
            log.exception("Failed to write claude settings")

        # 3. CLAUDE.md — meta prompt
        self._copy_prompt_file("CLAUDE.md", cwd)

        # 4. Skills — .claude/skills/localwriter/SKILL.md
        skills_src = os.path.join(_PROMPTS_DIR, "skills")
        skills_dst = os.path.join(cwd, ".claude", "skills")
        if os.path.isdir(skills_src):
            for skill_name in os.listdir(skills_src):
                src_dir = os.path.join(skills_src, skill_name)
                if os.path.isdir(src_dir):
                    dst_dir = os.path.join(skills_dst, skill_name)
                    os.makedirs(dst_dir, exist_ok=True)
                    for fname in os.listdir(src_dir):
                        src_file = os.path.join(src_dir, fname)
                        dst_file = os.path.join(dst_dir, fname)
                        if os.path.isfile(src_file):
                            shutil.copy2(src_file, dst_file)
                            log.debug("Copied skill file: %s", dst_file)

    def _copy_prompt_file(self, filename, cwd):
        """Copy a prompt template file into the working directory."""
        src = os.path.join(_PROMPTS_DIR, filename)
        dst = os.path.join(cwd, filename)
        if os.path.isfile(src):
            try:
                shutil.copy2(src, dst)
                log.info("Copied %s to %s", filename, dst)
            except Exception:
                log.exception("Failed to copy %s", filename)


def get_default_cwd(services):
    """Return the default working directory for Claude Code."""
    return _DEFAULT_CWD


def on_install():
    """Callback for the Install button in Options."""
    from plugin.modules.launcher import run_install_for_provider
    run_install_for_provider("claude")


class ClaudeModule(ModuleBase):

    def initialize(self, services):
        if hasattr(services, "launcher_manager"):
            services.launcher_manager.register_provider(
                "claude", ClaudeProvider())
