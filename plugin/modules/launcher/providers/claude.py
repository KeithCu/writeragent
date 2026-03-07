"""Claude Code launcher provider."""

import os
import json
import shutil

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

        # 3. Copy skills
        skills_src = os.path.join(os.path.dirname(__file__), "claude_skills")
        if os.path.isdir(skills_src):
            skills_dst = os.path.join(cwd, "skills")
            if os.path.exists(skills_dst):
                shutil.rmtree(skills_dst)
            shutil.copytree(skills_src, skills_dst)

        return env
