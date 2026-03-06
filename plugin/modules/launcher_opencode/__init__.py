# Adapted from Nelson MCP
"""OpenCode CLI provider for the launcher module."""

import json
import logging
import os
import shutil

from plugin.framework.module_base import ModuleBase

log = logging.getLogger("localwriter.launcher.opencode")

_DEFAULT_CWD = os.path.join(
    os.path.expanduser("~"), ".local", "share", "localwriter", "cli", "opencode")

# Directory containing prompt templates shipped with this module
_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


class OpenCodeProvider:
    """OpenCode CLI — AI coding assistant."""

    name = "opencode"
    label = "OpenCode"
    binary_name = "opencode"
    install_url = "https://opencode.ai/docs/cli/"
    default_cwd = _DEFAULT_CWD

    def get_args(self, mcp_url, config):
        return [
            "--prompt", "You are helping the user work with a LibreOffice "
            "document through LocalWriter MCP. Start by calling get_document_info "
            "to see the current document. If no document is open, use "
            "list_open_documents or get_recent_documents to find one, "
            "then open_document to open it.",
        ]

    def setup_env(self, mcp_url, env, cwd, config):
        """Write opencode.json and AGENTS.md into the working directory."""
        handle_config = config.get("handle_config", True)

        # 1. opencode.json — always inject MCP, optionally inject Ollama
        config_path = os.path.join(cwd, "opencode.json")
        oc_config = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "localwriter": {
                    "type": "remote",
                    "url": mcp_url + "/sse",
                }
            }
        }

        if handle_config:
            ollama_url = config.get("ollama_url") or "http://localhost:11434/v1"
            ollama_model = config.get("ollama_model") or "qwen3:8b"

            oc_config["provider"] = {
                "ollama": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Ollama (local)",
                    "options": {
                        "baseURL": ollama_url,
                    },
                    "models": {
                        ollama_model: {
                            "name": ollama_model,
                        }
                    }
                }
            }
            oc_config["model"] = "ollama/%s" % ollama_model

        try:
            with open(config_path, "w") as f:
                json.dump(oc_config, f, indent=2)
            log.info("Wrote opencode config: %s", config_path)
        except Exception:
            log.exception("Failed to write opencode config")


def get_default_cwd(services):
    """Return the default working directory for OpenCode."""
    return _DEFAULT_CWD


def get_ollama_models(services):
    """Query Ollama API for installed models. Returns options list."""
    import urllib.request
    import json as _json

    # Read ollama_url from config, fallback to default
    url = "http://localhost:11434"
    try:
        if services:
            cfg = services.config.proxy_for("launcher.opencode")
            raw = cfg.get("ollama_url") or ""
            if raw:
                # Strip /v1 suffix if present
                url = raw.replace("/v1", "").rstrip("/")
    except Exception:
        pass

    try:
        req = urllib.request.urlopen(url + "/api/tags", timeout=3)
        data = _json.loads(req.read())
        models = data.get("models", [])
        return [
            {"value": m["name"], "label": m["name"]}
            for m in models
        ]
    except Exception:
        log.debug("Could not query Ollama models at %s", url)
        return []


def on_install():
    """Callback for the Install button in Options."""
    from plugin.modules.launcher import run_install_for_provider
    run_install_for_provider("opencode")


class OpenCodeModule(ModuleBase):

    def initialize(self, services):
        if hasattr(services, "launcher_manager"):
            services.launcher_manager.register_provider(
                "opencode", OpenCodeProvider())
