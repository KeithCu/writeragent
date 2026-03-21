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
"""Claude agent backend adapter using the Agent Client Protocol (ACP).

Communicates with Claude Code (or compatible adapters) via standard stdio JSON-RPC.
Requires an ACP adapter like `claude-code-acp` or `claude-code-acp-rs` installed.
"""

import logging
import json
import os
import shutil
import threading
import time

from plugin.framework.errors import format_error_payload
from plugin.modules.agent_backend.base import AgentBackend
from plugin.modules.agent_backend.acp_connection import ACPConnection
log = logging.getLogger(__name__)

_LOG = "ClaudeACP"

_ACP_PROTOCOL_VERSION = 1


def _find_claude_binary():
    """Find the claude-code-acp binary in PATH or common locations."""
    for name in ("claude-code-acp-rs", "claude-code-acp"):
        path = shutil.which(name)
        if path:
            return path
    # Check common install locations
    home = os.path.expanduser("~")
    for candidate in (
        os.path.join(home, ".local", "bin", "claude-code-acp-rs"),
        os.path.join(home, ".local", "bin", "claude-code-acp"),
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


class ClaudeBackend(AgentBackend):
    """ACP-based Claude backend via stdio JSON-RPC subprocess."""

    backend_id = "claude"
    display_name = "Claude Code (ACP)"

    def __init__(self, ctx=None):
        self._ctx = ctx
        self._conn = None
        self._session_id = None
        self._stop_requested = False
        self._claude_cmd = None
        self._claude_args = []
        self._prompt_done = threading.Event()

    def _load_config(self):
        """Read claude path and args from WriterAgent config."""
        try:
            from plugin.framework.config import get_config
            path = str(get_config(self._ctx, "agent_backend.path") or "").strip()
            if path and not path.startswith("http") and ("hermes" not in path.lower()):
                self._claude_cmd = path
            else:
                self._claude_cmd = _find_claude_binary()

            args_str = str(get_config(self._ctx, "agent_backend.args") or "").strip()
            # If there are arguments like URL or anything that doesn't fit claude directly,
            # you might need to filter. But simple space splitting generally works.
            self._claude_args = args_str.split() if args_str else []
        except Exception:
            self._claude_cmd = _find_claude_binary()

    def is_available(self, ctx):
        """Check if claude-code-acp is installed."""
        self._load_config()
        if self._claude_cmd and os.path.isfile(self._claude_cmd):
            log.info(f"Claude acp binary found: {self._claude_cmd}")
            return True
        cmd = _find_claude_binary()
        if cmd:
            self._claude_cmd = cmd
            log.info(f"Claude acp found via PATH: {cmd}")
            return True
        log.info("Claude acp binary not found")
        return False

    def _ensure_connection(self):
        """Start the ACP subprocess if not already running."""
        if self._conn and self._conn.is_alive:
            return
        if not self._claude_cmd:
            raise RuntimeError("Claude ACP binary not found. Install `@zed-industries/claude-code-acp` or `claude-code-acp-rs` and ensure it is in PATH.")

        cmd_line = [self._claude_cmd] + self._claude_args

        env = dict(os.environ)
        # Handle ANTHROPIC_API_KEY from general settings fallback as helper
        if "ANTHROPIC_API_KEY" not in env:
            try:
                from plugin.framework.config import get_api_key_for_endpoint, get_config
                endpoint = str(get_config(self._ctx, "ai.endpoint") or "")
                key = get_api_key_for_endpoint(self._ctx, endpoint)
                if key:
                    env["ANTHROPIC_API_KEY"] = key
                    log.warning("Using fallback ANTHROPIC_API_KEY from general settings")
            except Exception:
                pass

        self._conn = ACPConnection(cmd_line=cmd_line, env=env)
        self._conn.start()

        # Wait a moment for the process to start
        time.sleep(0.5)
        if not self._conn.is_alive:
            raise RuntimeError("Claude ACP process failed to start. Verify your API key and installation.")

        # Initialize handshake
        try:
            result = self._conn.send_request("initialize", {
                "protocolVersion": _ACP_PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"read_text_file": False, "write_text_file": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "WriterAgent", "version": "1.0"},
            }, timeout=15)
            log.info(f"Claude ACP initialized: {result}")
        except Exception as e:
            log.error(f"Claude ACP initialize failed: {e}")
            self._conn.stop()
            self._conn = None
            raise

    def _ensure_session(self, mcp_url=None, document_url=None):
        """Create a new ACP session if needed."""
        if self._session_id:
            return

        # mcp_servers is required by the ACP schema
        mcp_servers = []
        if mcp_url:
            mcp_servers.append({
                "url": mcp_url,
                "name": "writeragent",
                "type": "http",
                "headers": [],
            })

        params = {
            "cwd": os.getcwd(),
            "mcpServers": mcp_servers,
        }

        try:
            result = self._conn.send_request("session/new", params, timeout=30)
            self._session_id = result.get("sessionId", "") if result else ""
            log.debug(f"Claude ACP session created: {self._session_id}")
        except Exception as e:
            log.error(f"Claude ACP session creation failed: {e}")
            raise

    def send(
        self,
        queue,
        user_message,
        document_context,
        document_url,
        system_prompt=None,
        mcp_url=None,
        selection_text=None,
        stop_checker=None,
        **kwargs
    ):
        """Send a message to Claude via ACP stdio."""
        self._stop_requested = False
        self._prompt_done.clear()
        self._load_config()

        queue.put(("status", f"Starting {self.display_name}..."))

        try:
            self._ensure_connection()
        except Exception as e:
            queue.put(("error", format_error_payload(RuntimeError(
                f"Cannot start {self.display_name} ACP. "
                f"Is adapter installed? Error: {e}"
            ))))
            return

        try:
            self._ensure_session(mcp_url=mcp_url, document_url=document_url)
        except Exception as e:
            queue.put(("error", format_error_payload(RuntimeError(f"Session creation failed: {e}"))))
            return

        queue.put(("status", f"Sending to {self.display_name}..."))

        # Build prompt content blocks
        prompt_blocks = []
        if system_prompt:
            prompt_blocks.append({"type": "text", "text": system_prompt})
        if document_context:
            prompt_blocks.append({"type": "text", "text": f"[DOCUMENT CONTENT]\n{document_context}"})
        if selection_text:
            prompt_blocks.append({"type": "text", "text": f"[SELECTED TEXT]\n{selection_text}"})
        if document_url:
            prompt_blocks.append({"type": "text", "text": f"Document URL: {document_url}"})

        prompt_blocks.append({"type": "text", "text": user_message})

        def on_notification(method, params, msg_id=None):
            if self._stop_requested:
                return
            if method == "session/request_permission":
                description = params.get("description", "Claude requests permission")
                queue.put(("approval_required", description, "", {}, msg_id))
            else:
                self._handle_notification(method, params, queue)

        self._conn.set_notification_callback(on_notification)

        try:
            result = self._conn.send_request("session/prompt", {
                "sessionId": self._session_id,
                "prompt": prompt_blocks,
            }, timeout=600)

            if result:
                stop_reason = result.get("stopReason", result.get("stop_reason", ""))
                log.info(f"Claude prompt completed: stop_reason={stop_reason}")

            queue.put(("stream_done", None))

        except TimeoutError:
            queue.put(("error", format_error_payload(RuntimeError(f"{self.display_name} prompt timed out"))))
        except Exception as e:
            if self._stop_requested:
                queue.put(("stopped",))
            else:
                log.error(f"Claude prompt error: {e}")
                queue.put(("error", format_error_payload(e)))
        finally:
            self._conn.set_notification_callback(None)
            self._prompt_done.set()

    def _handle_notification(self, method, params, queue):
        """Handle ACP notifications during prompt execution."""
        if method in ("notifications/session", "session/update"):
            update = params.get("update", {})
            self._handle_session_update(update, queue)
        elif method in ("notifications/agent", "agent/update"):
            # Some agent architectures push directly via updates
            update = params.get("update", params)
            self._handle_agent_update(update, queue)
        else:
            log.debug(f"Unhandled notification: {method}")

    def _handle_session_update(self, update, queue):
        session_update = update.get("session_update", update.get("sessionUpdate", ""))

        if session_update in ("text", "agent_thought_chunk"):
            text = update.get("chunk", update.get("text", update.get("content", "")))
            if isinstance(text, dict):
                text = text.get("text", text.get("content", str(text)))
            if text:
                queue.put(("chunk", str(text)))

        elif session_update in ("tool_call", "tool_call_update"):
            status = update.get("status", "")
            title = update.get("title", "")
            if status == "in_progress":
                thinking = f"[Tool: {title}]"
                raw_input = update.get("raw_input", update.get("rawInput"))
                if raw_input:
                    thinking += f"\nInput: {json.dumps(raw_input, indent=2)}"
                queue.put(("thinking", thinking + "\n"))
            elif status == "completed":
                raw_output = update.get("raw_output", update.get("rawOutput"))
                if raw_output:
                    out_text = json.dumps(raw_output, indent=2) if not isinstance(raw_output, str) else raw_output
                    queue.put(("thinking", f"[Tool result: {title}]\n{out_text[:500]}\n"))
            elif status == "failed":
                queue.put(("thinking", f"[Tool failed: {title}]\n"))

        elif session_update == "plan":
            entries = update.get("entries", [])
            if entries:
                plan_text = "\n".join(f"  {'✓' if e.get('done') else '○'} {e.get('text', '')}" for e in entries)
                queue.put(("thinking", f"[Plan]\n{plan_text}\n"))

    def _handle_agent_update(self, update, queue):
        if "permission" in update or "approval" in update:
            description = json.dumps(update)
            queue.put(("approval_required", description, "", {}, self._session_id or ""))

    def stop(self):
        self._stop_requested = True
        if self._conn and self._conn.is_alive and self._session_id:
            try:
                self._conn.send_notification("session/cancel", {"sessionId": self._session_id})
                log.debug("Cancel notification sent")
            except Exception as e:
                log.error(f"Cancel failed: {e}")

    def submit_approval(self, request_id, approved):
        if not self._conn or not self._conn.is_alive or not request_id:
            return
        msg = {"jsonrpc": "2.0", "id": request_id, "result": {"approved": approved}}
        try:
            self._conn._proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            self._conn._proc.stdin.flush()
        except Exception:
            pass

    def cleanup(self):
        if self._conn:
            self._conn.stop()
            self._conn = None
        self._session_id = None