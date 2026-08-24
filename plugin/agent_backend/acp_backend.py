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
"""Base class for ACP (Agent Communication Protocol) backends.

Extracts common ACP logic: connection management, session handling,
notification processing, and prompt formatting.
"""

import logging
import os
import shutil
import threading
import time
from typing import Optional, Dict, List, Tuple

from plugin.agent_backend.base import AgentBackend
from plugin.agent_backend.acp_connection import ACPConnection
from plugin.framework.async_stream import StreamQueueKind
from plugin.framework.errors import format_error_payload

log = logging.getLogger(__name__)

# ACP protocol version (integer per SDK)
_ACP_PROTOCOL_VERSION = 1


class ACPBackend(AgentBackend):
    """Base class for ACP-based agent backends.

    Subclasses must implement:
    - get_binary_name(): return binary name (e.g., "hermes")
    - get_display_name(): return UI display name
    - get_agent_name(): return ACP agent name
    - get_env_vars(): return dict of environment variables to pass

    Optional class attr for CLIs that need a default subcommand when
    ``agent_backend.args`` is empty (Hermes/OpenCode ``acp``, Grok
    ``--no-auto-update agent stdio``):
    - default_extra_args: immutable tuple; ``get_default_extra_args()``
      copies it onto ``_extra_args`` when the resolved basename equals
      ``get_binary_name()``. Grok overrides ``_apply_default_extra_args``
      for prefix matching.
    """

    default_extra_args: Tuple[str, ...] = ()

    def __init__(self, ctx=None):
        self._ctx = ctx
        self._conn = None
        self._session_id = None
        self._stop_requested = False
        self._binary_path: Optional[str] = None
        self._extra_args: List[str] = []
        self._prompt_done = threading.Event()
        self._load_config()

    def _load_config(self):
        """Load configuration from WriterAgent settings."""
        try:
            from plugin.framework.config import get_config

            path = str(get_config("agent_backend.path") or "").strip()
            if path and os.path.isfile(path):
                self._binary_path = path
            else:
                self._binary_path = self._find_binary()

            args_str = str(get_config("agent_backend.args") or "").strip()
            self._extra_args = args_str.split() if args_str else []
        except Exception:
            self._binary_path = self._find_binary()
        self._apply_default_extra_args()

    def get_default_extra_args(self) -> List[str]:
        """CLI args used when settings args are empty and the binary matches this backend."""
        return list(self.default_extra_args)

    def _apply_default_extra_args(self) -> None:
        """Fill ``_extra_args`` from ``get_default_extra_args()`` when settings left them empty."""
        if self._extra_args:
            return
        defaults = self.get_default_extra_args()
        if not defaults or not self._binary_path:
            return
        if os.path.basename(self._binary_path).lower() == self.get_binary_name().lower():
            self._extra_args = list(defaults)

    def _find_binary(self):
        """Find the binary in PATH or common locations."""
        binary_name = self.get_binary_name()

        # Try the binary name directly
        path = shutil.which(binary_name)
        if path:
            return path

        # Check common install locations
        home = os.path.expanduser("~")
        for candidate in (os.path.join(home, ".local", "bin", binary_name), os.path.join(home, ".cargo", "bin", binary_name)):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

        return None

    def get_binary_name(self) -> str:
        """Return the binary name to search for (e.g., 'hermes')."""
        raise NotImplementedError

    def get_display_name(self) -> str:
        """Return display name for UI."""
        raise NotImplementedError

    def get_agent_name(self) -> str:
        """Return ACP agent name."""
        raise NotImplementedError

    def get_env_vars(self) -> Dict[str, str]:
        """Return environment variables to pass to subprocess."""
        return {}

    def is_available(self, ctx):
        """Check if binary is installed."""
        self._load_config()
        if self._binary_path and os.path.isfile(self._binary_path):
            log.info(f"{self.get_display_name()} binary found: {self._binary_path}")
            return True
        # Fallback: search PATH
        binary_name = self.get_binary_name()
        path = shutil.which(binary_name)
        if path:
            self._binary_path = path
            self._apply_default_extra_args()
            log.info(f"{self.get_display_name()} found via PATH: {path}")
            return True
        log.info(f"{self.get_display_name()} binary not found")
        return False

    def _ensure_connection(self):
        """Start the ACP subprocess if not already running."""
        if self._conn and self._conn.is_alive:
            return
        if not self._binary_path:
            raise RuntimeError(f"{self.get_display_name()} binary not found. Install {self.get_binary_name()} and ensure it's in PATH.")

        cmd_line = [self._binary_path]
        cmd_line.extend(self._extra_args)

        env = dict(os.environ)
        env.update(self.get_env_vars())

        self._conn = ACPConnection(cmd_line=cmd_line, env=env)
        self._conn.start()

        # Wait a moment for the process to start
        time.sleep(0.5)
        if not self._conn.is_alive:
            raise RuntimeError(f"{self.get_display_name()} ACP process failed to start.")

        # Initialize handshake
        try:
            result = self._conn.send_request("initialize", {"protocolVersion": _ACP_PROTOCOL_VERSION, "clientCapabilities": {"fs": {"read_text_file": False, "write_text_file": False}, "terminal": False}, "clientInfo": {"name": "WriterAgent", "version": "1.0"}}, timeout=15)
            log.info(f"ACP initialized: {result}")
        except Exception:
            log.exception("ACP initialize failed")
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
            mcp_servers.append({"url": mcp_url, "name": "writeragent", "type": "http", "headers": []})

        params = {"cwd": os.getcwd(), "mcpServers": mcp_servers}

        try:
            if self._conn:
                result = self._conn.send_request("session/new", params, timeout=30)
                self._session_id = result.get("sessionId", "") if result else ""
                log.debug(f"ACP session created: {self._session_id}")
        except Exception:
            log.exception("ACP session creation failed")
            raise

    def _build_prompt_blocks(self, user_message: str, document_context: Optional[str] = None, system_prompt: Optional[str] = None, selection_text: Optional[str] = None, document_url: Optional[str] = None) -> List[Dict]:
        """Build ACP prompt content blocks."""
        prompt_blocks = []
        is_slash_command = user_message.strip().startswith("/")

        if is_slash_command:
            # For slash commands, only send the command itself
            prompt_blocks.append({"type": "text", "text": user_message})
        else:
            # Add system prompt if provided
            if system_prompt:
                prompt_blocks.append({"type": "text", "text": system_prompt})
            # Add document context if provided
            if document_context:
                prompt_blocks.append({"type": "text", "text": f"[DOCUMENT CONTENT]\n{document_context}"})
            # Add selection text if provided
            if selection_text:
                prompt_blocks.append({"type": "text", "text": f"[SELECTED TEXT]\n{selection_text}"})
            # Add document URL if provided
            if document_url:
                prompt_blocks.append({"type": "text", "text": f"Document URL: {document_url}"})
            # Always add the user message last
            prompt_blocks.append({"type": "text", "text": user_message})

        return prompt_blocks

    def _handle_acp_update(self, update, queue):
        """Queue CHUNK / TOOL_CALL / TOOL_RESULT from session or agent update content.

        Session and agent notifications use the same shapes: ``content`` is either
        a list of blocks or a single block dict (``text`` / ``tool_call`` / ``tool_result``).
        """
        if not isinstance(update, dict) or "content" not in update:
            return
        content = update["content"]
        if isinstance(content, list):
            items = content
        elif isinstance(content, dict):
            items = [content]
        else:
            log.debug("ACP update content is neither list nor dict: %s", type(content))
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                queue.put((StreamQueueKind.CHUNK, item.get("text", "")))
            elif item_type == "tool_call":
                queue.put((StreamQueueKind.TOOL_CALL, item))
            elif item_type == "tool_result":
                queue.put((StreamQueueKind.TOOL_RESULT, item))

    def send(self, queue, user_message, document_context, document_url, system_prompt=None, mcp_url=None, selection_text=None, stop_checker=None, **kwargs):
        """Send a message via ACP stdio."""
        self._stop_requested = False
        self._prompt_done.clear()

        queue.put((StreamQueueKind.STATUS, f"Starting {self.get_display_name()}..."))

        try:
            self._ensure_connection()
        except Exception as e:
            queue.put((StreamQueueKind.ERROR, format_error_payload(RuntimeError(f"Cannot start {self.get_display_name()} ACP. Is {self.get_binary_name()} installed? Error: {e}"))))
            return

        try:
            self._ensure_session(mcp_url=mcp_url, document_url=document_url)
        except Exception as e:
            queue.put((StreamQueueKind.ERROR, format_error_payload(RuntimeError(f"Session creation failed: {e}"))))
            return

        queue.put((StreamQueueKind.STATUS, f"Sending to {self.get_display_name()}..."))

        # Build prompt content blocks
        prompt_blocks = self._build_prompt_blocks(user_message=user_message, document_context=document_context, system_prompt=system_prompt, selection_text=selection_text, document_url=document_url)

        # Set up notification handler for streaming updates
        def on_notification(method, params, msg_id=None):
            if self._stop_requested:
                return
            if method == "session/request_permission":
                description = params.get("description", "Agent requests permission")
                tool_call = params.get("toolCall", {})
                tool_name = tool_call.get("name", "") if isinstance(tool_call, dict) else ""
                queue.put((StreamQueueKind.APPROVAL_REQUIRED, description, tool_name, tool_call, msg_id))
            elif method in ("notifications/session", "session/update"):
                self._handle_acp_update(params.get("update", {}), queue)
            elif method in ("notifications/agent", "agent/update"):
                self._handle_acp_update(params.get("update", params), queue)

        if self._conn:
            self._conn.set_notification_callback(on_notification)

        # Send prompt
        try:
            if self._conn:
                result = self._conn.send_request("session/prompt", {"sessionId": self._session_id, "prompt": prompt_blocks}, timeout=600)

                # Process the final response
                if result:
                    stop_reason = result.get("stopReason", result.get("stop_reason", ""))
                    log.info(f"Prompt completed: stop_reason={stop_reason}")
                    # Some ACP agents (Vibe) put final text in the prompt result.
                    # Same block types as session updates; drain if present.
                    content_blocks = result.get("contentBlocks") or []
                    if content_blocks:
                        self._handle_acp_update({"content": content_blocks}, queue)

            queue.put((StreamQueueKind.STREAM_DONE, None))

        except TimeoutError:
            queue.put((StreamQueueKind.ERROR, format_error_payload(RuntimeError(f"{self.get_display_name()} prompt timed out"))))
        except Exception as e:
            if self._stop_requested:
                queue.put((StreamQueueKind.STOPPED,))
            else:
                log.exception("Prompt execution failed")
                queue.put((StreamQueueKind.ERROR, format_error_payload(e)))
        finally:
            if self._conn:
                self._conn.set_notification_callback(None)
            self._prompt_done.set()

    def stop(self):
        """Stop current operation."""
        self._stop_requested = True
        if self._conn:
            try:
                # Send interrupt notification if supported
                self._conn.send_notification("session/interrupt", {"sessionId": self._session_id})
            except Exception:
                pass
        self._prompt_done.set()

    def submit_approval(self, request_id, approved):
        """Submit HITL approval response back to ACP process."""
        if not self._conn or not self._conn.is_alive:
            log.warning("Cannot submit approval, ACP connection is dead")
            return

        try:
            self._conn.send_response(request_id, result={"approved": approved})
        except Exception:
            log.exception("Failed to submit approval")

    def shutdown(self):
        """Clean up resources."""
        if self._conn:
            self._conn.stop()
            self._conn = None
        self._session_id = None
