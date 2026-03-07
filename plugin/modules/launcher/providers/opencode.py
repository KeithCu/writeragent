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

        ai = self.services.get("ai")
        inst = ai.get_instance("text")

        endpoint = "http://localhost:11434/v1"
        model = "qwen2.5-coder:7b"

        if inst:
            try:
                p_cfg = getattr(inst.provider, "_config", {})
                endpoint = p_cfg.get("endpoint", endpoint)
                model = p_cfg.get("model", model)
            except Exception:
                pass

        opencode_config = {
            "api_key": "ollama",
            "base_url": endpoint,
            "model": model,
            "mcp_url": mcp_url
        }
        with open(os.path.join(cwd, "opencode.json"), "w") as f:
            json.dump(opencode_config, f, indent=2)

        return env
