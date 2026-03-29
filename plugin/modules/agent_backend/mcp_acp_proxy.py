# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing
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
"""MCP ACP Proxy - Exposes WriterAgent MCP server as an ACP agent.

This backend allows external ACP clients (like Mistral Vibe) to connect
to WriterAgent's MCP server by acting as an ACP-to-MCP protocol bridge.
"""

import logging
import threading
import requests
from typing import Optional, Dict, Any, List

from plugin.modules.agent_backend.base import AgentBackend
from plugin.framework.errors import format_error_payload

log = logging.getLogger(__name__)

_LOG = "MCPACP"

# Default MCP server URL
_DEFAULT_MCP_URL = "http://localhost:8765/mcp"


class MCPACPProxy(AgentBackend):
    """ACP backend that bridges to WriterAgent's MCP server."""

    backend_id = "mcp_acp"
    display_name = "WriterAgent MCP (ACP)"

    def __init__(self, ctx=None):
        self._ctx = ctx
        self._mcp_url = _DEFAULT_MCP_URL
        self._session_id = None
        self._stop_requested = False
        self._prompt_done = threading.Event()
        self._tools_cache = None
        self._last_tools_fetch = 0
        self._tools_cache_ttl = 300  # 5 minutes
        self._load_config()

    def _load_config(self):
        """Read MCP server URL from WriterAgent config."""
        try:
            from plugin.framework.config import get_config
            path = str(get_config(self._ctx, "agent_backend.path") or "").strip()
            if path and path.startswith("http"):
                self._mcp_url = path
            else:
                # Try to get MCP URL from http config
                mcp_port = get_config(self._ctx, "http.mcp_port") or 8765
                self._mcp_url = f"http://localhost:{mcp_port}/mcp"
        except Exception as e:
            log.warning(f"Failed to load config: {e}")
            self._mcp_url = _DEFAULT_MCP_URL

    def _call_mcp(self, method: str, params: Optional[Dict] = None, 
                  document_url: Optional[str] = None) -> Dict:
        """Call MCP JSON-RPC method."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {}
        }
        
        headers = {"Content-Type": "application/json"}
        if document_url:
            headers["X-Document-URL"] = document_url
        
        try:
            response = requests.post(
                self._mcp_url,
                json=payload,
                headers=headers,
                timeout=30
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            log.error(f"MCP call failed: {e}")
            return {"error": {"code": -32000, "message": str(e)}}

    def _get_tools(self) -> List[Dict]:
        """Get cached tool list from MCP server."""
        import time
        current_time = time.time()
        
        if self._tools_cache and (current_time - self._last_tools_fetch) < self._tools_cache_ttl:
            return self._tools_cache
        
        result = self._call_mcp("tools/list")
        if "result" in result and "tools" in result["result"]:
            self._tools_cache = result["result"]["tools"]
            self._last_tools_fetch = current_time
            return self._tools_cache
        
        log.error(f"Failed to fetch tools: {result}")
        return []

    def initialize(self) -> Dict:
        """ACP initialize handshake."""
        log.info("ACP initialize called")
        
        # Get available tools
        tools = self._get_tools()
        
        return {
            "protocol_version": 1,
            "agent_name": "WriterAgent",
            "capabilities": {
                "tools": [
                    {
                        "name": tool["name"],
                        "description": tool["description"],
                        "input_schema": tool.get("inputSchema", {})
                    }
                    for tool in tools
                ]
            }
        }

    def new_session(self, cwd: str, mcp_servers: List[str]) -> str:
        """ACP new_session call."""
        log.info(f"ACP new_session: cwd={cwd}, mcp_servers={mcp_servers}")
        
        # Generate a session ID
        import uuid
        self._session_id = str(uuid.uuid4())
        
        return self._session_id

    def prompt(self, session_id: str, content_blocks: List[Dict]) -> Dict:
        """ACP prompt call - execute tools or process messages."""
        log.info(f"ACP prompt: session_id={session_id}, blocks={len(content_blocks)}")
        
        self._stop_requested = False
        self._prompt_done.clear()
        
        # Process content blocks to extract tool calls
        tool_calls = []
        for block in content_blocks:
            if block.get("type") == "tool_call":
                tool_calls.append(block)
        
        results = []
        for tool_call in tool_calls:
            if self._stop_requested:
                break
            
            tool_name = tool_call.get("tool_name")
            arguments = tool_call.get("arguments", {})
            
            # Call MCP tool
            result = self._call_mcp("tools/call", {
                "name": tool_name,
                "arguments": arguments
            })
            
            if "result" in result:
                tool_result = result["result"]
                results.append({
                    "type": "tool_result",
                    "tool_call_id": tool_call.get("tool_call_id"),
                    "content": tool_result.get("content", []) 
                })
            else:
                error_msg = result.get("error", {}).get("message", "Unknown error")
                results.append({
                    "type": "tool_result",
                    "tool_call_id": tool_call.get("tool_call_id"),
                    "content": [{"type": "text", "text": f"Error: {error_msg}"}]
                })
        
        self._prompt_done.set()
        
        return {
            "content_blocks": results
        }

    def stop(self):
        """Stop current operation."""
        log.info("ACP stop called")
        self._stop_requested = True
        self._prompt_done.set()

    def is_alive(self) -> bool:
        """Check if backend is available."""
        try:
            # Test MCP server connection
            result = self._call_mcp("tools/list")
            return "result" in result
        except Exception:
            return False

    def get_display_name(self) -> str:
        """Get display name for UI."""
        return self.display_name

    def get_agent_name(self) -> str:
        """Get agent name for ACP."""
        return "writeragent"

    def supports_tool_calling(self) -> bool:
        """This backend supports tool calling."""
        return True

    def supports_streaming(self) -> bool:
        """This backend supports streaming responses."""
        return False

    def get_tool_list(self) -> List[Dict]:
        """Get list of available tools."""
        return self._get_tools()
