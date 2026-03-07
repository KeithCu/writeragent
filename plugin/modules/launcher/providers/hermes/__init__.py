"""Hermes Agent CLI provider for the launcher module."""

import json
import logging
import os
import shutil
import yaml

from plugin.framework.module_base import ModuleBase

log = logging.getLogger("localwriter.launcher.hermes")

# Directory containing prompt templates shipped with this module
_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")

def find_hermes_agent_root():
    """Locate the hermes-agent directory in the workspace."""
    # Common locations relative to the workspace root
    # Since we are running inside LibreOffice, we might need to find the workspace
    # But for now, we can try to find it relative to the plugin directory
    # or look for the 'hermes-agent' directory in the project root.
    plugin_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    potential_root = os.path.join(plugin_dir, "hermes-agent")
    if os.path.isdir(potential_root):
        return potential_root
    
    # Fallback: check current directory if it's the project root
    if os.path.isdir("hermes-agent"):
        return os.path.abspath("hermes-agent")
        
    return None

class HermesProvider:
    """Hermes Agent — The self-improving AI agent."""

    name = "hermes"
    label = "Hermes Agent"
    install_url = "https://hermes-agent.nousresearch.com/docs/getting-started/quickstart"
    
    @property
    def binary_name(self):
        root = find_hermes_agent_root()
        if root:
            local_bin = os.path.join(root, "hermes")
            if os.path.isfile(local_bin):
                return local_bin
        return "hermes"
    
    @property
    def default_cwd(self):
        return os.path.join(os.path.expanduser("~"), ".local", "share", "localwriter", "cli", "hermes")

    def get_args(self, mcp_url, config):
        # By default, just launch 'hermes'
        return []

    def setup_env(self, mcp_url, env, cwd, config):
        """Configure Hermes Agent to use LocalWriter MCP."""
        os.makedirs(cwd, exist_ok=True)
        
        # We use a custom HERMES_HOME for this instance to avoid messing with user's global config
        # and to ensure it always points to our LocalWriter MCP.
        hermes_home = os.path.join(cwd, ".hermes")
        os.makedirs(hermes_home, exist_ok=True)
        
        # Set HERMES_HOME in the environment so the 'hermes' command uses our config
        env["HERMES_HOME"] = hermes_home

        # Add local hermes-agent root to PATH so the 'hermes' command can be found
        hermes_root = find_hermes_agent_root()
        if hermes_root:
            env["PATH"] = hermes_root + os.pathsep + env.get("PATH", "")
            # Ensure the python path also includes it so the wrapper works
            env["PYTHONPATH"] = hermes_root + os.pathsep + env.get("PYTHONPATH", "")
        
        # 1. config.yaml
        config_path = os.path.join(hermes_home, "config.yaml")
        
        # Load existing config if it exists, otherwise use a baseline
        mcp_config = {
            "mcp_servers": {
                "localwriter": {
                    "url": mcp_url + "/sse",
                    "timeout": 300
                }
            }
        }
        
        current_config = {}
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    current_config = yaml.safe_load(f) or {}
            except Exception:
                log.warning("Failed to load existing Hermes config at %s", config_path)
        
        # Merge MCP settings
        if "mcp_servers" not in current_config:
            current_config["mcp_servers"] = {}
        current_config["mcp_servers"]["localwriter"] = mcp_config["mcp_servers"]["localwriter"]
        
        # Ensure we have some default toolsets if none are set
        if "toolsets" not in current_config:
            current_config["toolsets"] = ["hermes-cli"]

        try:
            with open(config_path, "w") as f:
                yaml.dump(current_config, f, default_flow_style=False)
            log.info("Wrote Hermes config: %s", config_path)
        except Exception:
            log.exception("Failed to write Hermes config")

        # 2. HERMES.md — meta prompt
        self._copy_prompt_file("HERMES.md", cwd)

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

class HermesModule(ModuleBase):

    def initialize(self, services):
        if hasattr(services, "launcher_manager"):
            services.launcher_manager.register_provider(
                "hermes", HermesProvider())
