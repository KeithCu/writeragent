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
"""Hermes agent backend adapter using the Agent Communication Protocol (ACP).

Communicates with Hermes via the ACP **stdio JSON-RPC** transport: we spawn
``hermes acp`` (or ``hermes-acp``) as a subprocess and exchange newline-
delimited JSON-RPC messages over stdin/stdout.

Protocol flow:
  1. Initialize handshake
  2. NewSession(cwd, mcp_servers) → session_id
  3. Prompt(session_id, content_blocks) → streaming notifications → PromptResponse

ACP spec: https://agentcommunicationprotocol.dev
Hermes docs: https://github.com/NousResearch/hermes-agent
"""

import json
import os
import shutil
import subprocess
import threading
import time

from plugin.modules.acp.base import AgentBackend
from plugin.modules.acp.acp_connection import ACPConnection
from plugin.framework.logging import debug_log

_LOG = "HermesACP"

# Default hermes binary name (auto-discovered via PATH)
_DEFAULT_HERMES_CMD = "hermes"

# JSON-RPC protocol version
_JSONRPC_VERSION = "2.0"

# ACP protocol version (integer per SDK)
_ACP_PROTOCOL_VERSION = 1


def _find_hermes_binary():
    """Find the hermes binary in PATH or common locations."""
    # Try hermes-acp first (dedicated ACP binary)
    for name in ("hermes-acp", "hermes"):
        path = shutil.which(name)
        if path:
            return path, name
    # Check common install locations
    home = os.path.expanduser("~")
    for candidate in (
        os.path.join(home, ".local", "bin", "hermes-acp"),
        os.path.join(home, ".local", "bin", "hermes"),
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate, os.path.basename(candidate)
    return None, None




class HermesBackend(AgentBackend):
    """ACP-based Hermes backend via stdio JSON-RPC subprocess."""

    backend_id = "hermes"
    display_name = "Hermes"

    def __init__(self, ctx=None):
        self._ctx = ctx
        self._conn = None
        self._session_id = None
        self._stop_requested = False
        self._hermes_cmd = None
        self._hermes_args = []
        self._prompt_done = threading.Event()

    def _load_config(self):
        """Read hermes path and args from WriterAgent config."""
        try:
            from plugin.framework.config import get_config
            path = str(get_config(self._ctx, "acp.path") or "").strip()
            if path and not path.startswith("http"):
                self._hermes_cmd = path
            else:
                cmd, _ = _find_hermes_binary()
                self._hermes_cmd = cmd

            args_str = str(get_config(self._ctx, "acp.args") or "").strip()
            self._hermes_args = args_str.split() if args_str else []
        except Exception:
            cmd, _ = _find_hermes_binary()
            self._hermes_cmd = cmd

    def is_available(self, ctx):
        """Check if hermes is installed (binary found in PATH)."""
        self._load_config()
        if self._hermes_cmd and os.path.isfile(self._hermes_cmd):
            debug_log(f"Hermes binary found: {self._hermes_cmd}", context=_LOG)
            return True
        # Fallback: search PATH
        cmd, name = _find_hermes_binary()
        if cmd:
            self._hermes_cmd = cmd
            debug_log(f"Hermes found via PATH: {cmd}", context=_LOG)
            return True
        debug_log("Hermes binary not found", context=_LOG)
        return False

    def _ensure_connection(self):
        """Start the ACP subprocess if not already running."""
        if self._conn and self._conn.is_alive:
            return
        if not self._hermes_cmd:
            raise RuntimeError("Hermes binary not found. Install hermes-agent and ensure 'hermes' is in PATH.")

        cmd_line = [self._hermes_cmd]
        if os.path.basename(self._hermes_cmd) == "hermes":
            cmd_line.append("acp")
        cmd_line.extend(self._hermes_args)

        env = dict(os.environ)
        # Ensure hermes knows where its home is
        if "HERMES_HOME" not in env:
            hermes_home = os.path.join(os.path.expanduser("~"), ".hermes")
            if os.path.isdir(hermes_home):
                env["HERMES_HOME"] = hermes_home

        # Forward API key to Hermes
        if "OPENROUTER_API_KEY" not in env:
            try:
                from plugin.framework.config import get_api_key_for_endpoint, get_config
                endpoint = str(get_config(self._ctx, "ai.endpoint") or "")
                key = get_api_key_for_endpoint(self._ctx, endpoint)
                if key:
                    env["OPENROUTER_API_KEY"] = key
                    env["OPENAI_API_KEY"] = key
                    debug_log("Using fallback OPENROUTER_API_KEY from general settings", context=_LOG)
            except Exception:
                pass

        self._conn = ACPConnection(cmd_line=cmd_line, env=env)
        self._conn.start()

        # Wait a moment for the process to start
        time.sleep(0.5)
        if not self._conn.is_alive:
            raise RuntimeError("Hermes ACP process failed to start. Check hermes installation.")

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
            debug_log(f"ACP initialized: {result}", context=_LOG)
        except Exception as e:
            debug_log(f"ACP initialize failed: {e}", context=_LOG)
            self._conn.stop()
            self._conn = None
            raise

    def _ensure_session(self, mcp_url=None, document_url=None):
        """Create a new ACP session if needed."""
        if self._session_id:
            return

        # mcp_servers is required by the ACP schema (even if empty)
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
            debug_log(f"ACP session created: {self._session_id}", context=_LOG)
        except Exception as e:
            debug_log(f"ACP session creation failed: {e}", context=_LOG)
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
        """Send a message to Hermes via ACP stdio."""
        self._stop_requested = False
        self._prompt_done.clear()
        self._load_config()

        queue.put(("status", f"Starting {self.display_name}..."))

        try:
            self._ensure_connection()
        except Exception as e:
            queue.put(("error", RuntimeError(
                f"Cannot start {self.display_name} ACP. "
                f"Is hermes installed? Error: {e}"
            )))
            return

        try:
            self._ensure_session(mcp_url=mcp_url, document_url=document_url)
        except Exception as e:
            queue.put(("error", RuntimeError(f"Session creation failed: {e}")))
            return

        queue.put(("status", f"Sending to {self.display_name}..."))

        # Build prompt content blocks
        prompt_blocks = []
        is_slash_command = user_message.strip().startswith("/")

        if is_slash_command:
            # For slash commands, only send the command itself so that Hermes'
            # ACP adapter (`user_text.startswith("/")`) can intercept it cleanly,
            # and we avoid polluting the command with document context.
            prompt_blocks.append({
                "type": "text",
                "text": user_message,
            })
        else:
            if system_prompt:
                prompt_blocks.append({
                    "type": "text",
                    "text": system_prompt,
                })
            if document_context:
                prompt_blocks.append({
                    "type": "text",
                    "text": f"[DOCUMENT CONTENT]\n{document_context}",
                })
            if selection_text:
                prompt_blocks.append({
                    "type": "text",
                    "text": f"[SELECTED TEXT]\n{selection_text}",
                })
            if document_url:
                prompt_blocks.append({
                    "type": "text",
                    "text": f"Document URL: {document_url}",
                })
            # Always add the user message last
            prompt_blocks.append({
                "type": "text",
                "text": user_message,
            })

        # Set up notification handler for streaming updates
        def on_notification(method, params, msg_id=None):
            if self._stop_requested:
                return
            if method == "session/request_permission":
                description = params.get("description", "Agent requests permission")
                queue.put(("approval_required", description, "", {}, msg_id))
            else:
                self._handle_notification(method, params, queue)

        self._conn.set_notification_callback(on_notification)

        # Send the prompt request
        try:
            result = self._conn.send_request("session/prompt", {
                "sessionId": self._session_id,
                "prompt": prompt_blocks,
            }, timeout=600)

            # Process the final response
            if result:
                stop_reason = result.get("stopReason", result.get("stop_reason", ""))
                debug_log(f"Prompt completed: stop_reason={stop_reason}", context=_LOG)

            queue.put(("stream_done", None))

        except TimeoutError:
            queue.put(("error", RuntimeError(f"{self.display_name} prompt timed out")))
        except Exception as e:
            if self._stop_requested:
                queue.put(("stopped",))
            else:
                debug_log(f"Prompt error: {e}", context=_LOG)
                queue.put(("error", e))
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
            debug_log(f"Unhandled notification: {method}", context=_LOG)

    def _handle_session_update(self, update, queue):
        """Handle a session update notification."""
        session_update = update.get("session_update", update.get("sessionUpdate", ""))
        debug_log(f"session_update: {session_update}, payload: {update}", context=_LOG)

        if session_update in ("text", "agent_thought_chunk", "agent_message_chunk"):
            # Streaming text or thought from the agent
            text = update.get("chunk", update.get("text", update.get("content", "")))
            if isinstance(text, dict):
                text = text.get("text", text.get("content", str(text)))
            if text:
                queue.put(("chunk", str(text)))

        elif session_update in ("tool_call", "tool_call_update"):
            # Tool call update
            status = update.get("status", "")
            title = update.get("title", "")
            tool_id = update.get("tool_call_id", update.get("toolCallId", ""))

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
            # Agent plan update
            entries = update.get("entries", [])
            if entries:
                plan_text = "\n".join(
                    f"  {'✓' if e.get('done') else '○'} {e.get('text', '')}"
                    for e in entries
                )
                queue.put(("thinking", f"[Plan]\n{plan_text}\n"))

        elif session_update == "usage":
            cost = update.get("cost")
            if cost:
                debug_log(f"Usage: cost={cost}", context=_LOG)

        elif session_update == "info":
            title = update.get("title", "")
            if title:
                debug_log(f"Session info: {title}", context=_LOG)

        else:
            debug_log(f"Unhandled session_update type: {session_update}", context=_LOG)

    def _handle_agent_update(self, update, queue):
        """Handle an agent-level update."""
        # Agent approval / permission request
        if "permission" in update or "approval" in update:
            description = json.dumps(update)
            queue.put(("approval_required", description, "", {}, self._session_id or ""))

    def stop(self):
        """Cancel the current prompt."""
        self._stop_requested = True
        if self._conn and self._conn.is_alive and self._session_id:
            try:
                self._conn.send_notification("session/cancel", {
                    "sessionId": self._session_id,
                })
                debug_log("Cancel notification sent", context=_LOG)
            except Exception as e:
                debug_log(f"Cancel failed: {e}", context=_LOG)

    def submit_approval(self, request_id, approved):
        """Submit approval for a permission request."""
        if not self._conn or not self._conn.is_alive or not request_id:
            return
        
        # Send a JSON-RPC response back to the agent's permission request
        msg = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"approved": approved}
        }
        line = json.dumps(msg) + "\n"
        try:
            self._conn._proc.stdin.write(line.encode("utf-8"))
            self._conn._proc.stdin.flush()
            debug_log(f"Approval responded (id={request_id}): approved={approved}", context=_LOG)
        except Exception as e:
            debug_log(f"Approval response failed: {e}", context=_LOG)

    def cleanup(self):
        """Shutdown the ACP subprocess."""
        if self._conn:
            self._conn.stop()
            self._conn = None
        self._session_id = None
