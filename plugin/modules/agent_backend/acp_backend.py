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
from typing import Optional, Dict, List

from plugin.modules.agent_backend.base import AgentBackend
from plugin.modules.agent_backend.acp_connection import ACPConnection
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
    """

    def __init__(self, ctx=None):
        self._ctx = ctx
        self._conn = None
        self._session_id = None
        self._stop_requested = False
        self._binary_path = None
        self._extra_args = []
        self._prompt_done = threading.Event()
        self._load_config()

    def _load_config(self):
        """Load configuration from WriterAgent settings."""
        try:
            from plugin.framework.config import get_config
            path = str(get_config(self._ctx, "agent_backend.path") or "").strip()
            if path and os.path.isfile(path):
                self._binary_path = path
            else:
                self._binary_path = self._find_binary()

            args_str = str(get_config(self._ctx, "agent_backend.args") or "").strip()
            self._extra_args = args_str.split() if args_str else []
        except Exception:
            self._binary_path = self._find_binary()

    def _find_binary(self):
        """Find the binary in PATH or common locations."""
        binary_name = self.get_binary_name()
        
        # Try the binary name directly
        path = shutil.which(binary_name)
        if path:
            return path
        
        # Check common install locations
        home = os.path.expanduser("~")
        for candidate in (
            os.path.join(home, ".local", "bin", binary_name),
            os.path.join(home, ".cargo", "bin", binary_name),
        ):
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
            log.info(f"{self.get_display_name()} found via PATH: {path}")
            return True
        log.info(f"{self.get_display_name()} binary not found")
        return False

    def _ensure_connection(self):
        """Start the ACP subprocess if not already running."""
        if self._conn and self._conn.is_alive:
            return
        if not self._binary_path:
            raise RuntimeError(
                f"{self.get_display_name()} binary not found. "
                f"Install {self.get_binary_name()} and ensure it's in PATH."
            )

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
            result = self._conn.send_request("initialize", {
                "protocolVersion": _ACP_PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"read_text_file": False, "write_text_file": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "WriterAgent", "version": "1.0"},
            }, timeout=15)
            log.info(f"ACP initialized: {result}")
        except Exception as e:
            log.error(f"ACP initialize failed: {e}")
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
            if self._conn:
                result = self._conn.send_request("session/new", params, timeout=30)
                self._session_id = result.get("sessionId", "") if result else ""
                log.debug(f"ACP session created: {self._session_id}")
        except Exception as e:
            log.error(f"ACP session creation failed: {e}")
            raise

    def _build_prompt_blocks(
        self,
        user_message: str,
        document_context: Optional[str] = None,
        system_prompt: Optional[str] = None,
        selection_text: Optional[str] = None,
        document_url: Optional[str] = None
    ) -> List[Dict]:
        """Build ACP prompt content blocks."""
        prompt_blocks = []
        is_slash_command = user_message.strip().startswith("/")

        if is_slash_command:
            # For slash commands, only send the command itself
            prompt_blocks.append({
                "type": "text",
                "text": user_message
            })
        else:
            # Add system prompt if provided
            if system_prompt:
                prompt_blocks.append({
                    "type": "text",
                    "text": system_prompt,
                })
            # Add document context if provided
            if document_context:
                prompt_blocks.append({
                    "type": "text",
                    "text": f"[DOCUMENT CONTENT]\n{document_context}",
                })
            # Add selection text if provided
            if selection_text:
                prompt_blocks.append({
                    "type": "text",
                    "text": f"[SELECTED TEXT]\n{selection_text}",
                })
            # Add document URL if provided
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

        return prompt_blocks

    def _handle_session_update(self, update, queue):
        """Handle ACP session update notifications."""
        log.info(f"Session update received: {update}")  # DEBUG: Log full update
        if isinstance(update, dict):
            if "content" in update:
                content = update["content"]
                log.info(f"Found content: {content}")  # DEBUG: Log content
                
                # Handle both list format and direct dict format
                if isinstance(content, list):
                    # List format: [{"type": "text", "text": "..."}, ...]
                    for item in content:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                text = item.get("text", "")
                                log.info(f"Queueing text from session update (list): {text[:50]}...")  # DEBUG
                                queue.put((StreamQueueKind.CHUNK, text))
                            elif item.get("type") == "tool_call":
                                log.info(f"Queueing tool call from session update (list): {item}")  # DEBUG
                                queue.put((StreamQueueKind.TOOL_CALL, item))
                            elif item.get("type") == "tool_result":
                                log.info(f"Queueing tool result from session update (list): {item}")  # DEBUG
                                queue.put((StreamQueueKind.TOOL_RESULT, item))
                elif isinstance(content, dict):
                    # Direct dict format: {"type": "text", "text": "..."}
                    if content.get("type") == "text":
                        text = content.get("text", "")
                        log.info(f"Queueing text from session update (dict): {text[:50]}...")  # DEBUG
                        queue.put((StreamQueueKind.CHUNK, text))
                    elif content.get("type") == "tool_call":
                        log.info(f"Queueing tool call from session update (dict): {content}")  # DEBUG
                        queue.put((StreamQueueKind.TOOL_CALL, content))
                    elif content.get("type") == "tool_result":
                        log.info(f"Queueing tool result from session update (dict): {content}")  # DEBUG
                        queue.put((StreamQueueKind.TOOL_RESULT, content))
                else:
                    log.warning(f"Content is neither list nor dict: {type(content)}")  # DEBUG
            else:
                log.info(f"Update has no 'content' field. Keys: {list(update.keys())}")  # DEBUG

    def _handle_agent_update(self, update, queue):
        """Handle ACP agent update notifications."""
        log.info(f"Agent update received: {update}")  # DEBUG: Log full update
        if isinstance(update, dict):
            if "content" in update:
                content = update["content"]
                log.info(f"Found content in agent update: {content}")  # DEBUG: Log content
                
                # Handle both list format and direct dict format
                if isinstance(content, list):
                    # List format: [{"type": "text", "text": "..."}, ...]
                    for item in content:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                text = item.get("text", "")
                                log.info(f"Queueing text from agent update (list): {text[:50]}...")  # DEBUG
                                queue.put((StreamQueueKind.CHUNK, text))
                            elif item.get("type") == "tool_call":
                                log.info(f"Queueing tool call from agent update (list): {item}")  # DEBUG
                                queue.put((StreamQueueKind.TOOL_CALL, item))
                            elif item.get("type") == "tool_result":
                                log.info(f"Queueing tool result from agent update (list): {item}")  # DEBUG
                                queue.put((StreamQueueKind.TOOL_RESULT, item))
                elif isinstance(content, dict):
                    # Direct dict format: {"type": "text", "text": "..."}
                    if content.get("type") == "text":
                        text = content.get("text", "")
                        log.info(f"Queueing text from agent update (dict): {text[:50]}...")  # DEBUG
                        queue.put((StreamQueueKind.CHUNK, text))
                    elif content.get("type") == "tool_call":
                        log.info(f"Queueing tool call from agent update (dict): {content}")  # DEBUG
                        queue.put((StreamQueueKind.TOOL_CALL, content))
                    elif content.get("type") == "tool_result":
                        log.info(f"Queueing tool result from agent update (dict): {content}")  # DEBUG
                        queue.put((StreamQueueKind.TOOL_RESULT, content))
                else:
                    log.warning(f"Content is neither list nor dict: {type(content)}")  # DEBUG
            else:
                log.info(f"Agent update has no 'content' field. Keys: {list(update.keys())}")  # DEBUG

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
        """Send a message via ACP stdio."""
        self._stop_requested = False
        self._prompt_done.clear()

        queue.put((StreamQueueKind.STATUS, f"Starting {self.get_display_name()}..."))

        try:
            self._ensure_connection()
        except Exception as e:
            queue.put((StreamQueueKind.ERROR, format_error_payload(RuntimeError(
                f"Cannot start {self.get_display_name()} ACP. "
                f"Is {self.get_binary_name()} installed? Error: {e}"
            ))))
            return

        try:
            self._ensure_session(mcp_url=mcp_url, document_url=document_url)
        except Exception as e:
            queue.put((StreamQueueKind.ERROR, format_error_payload(RuntimeError(f"Session creation failed: {e}"))))
            return

        queue.put((StreamQueueKind.STATUS, f"Sending to {self.get_display_name()}..."))

        # Build prompt content blocks
        prompt_blocks = self._build_prompt_blocks(
            user_message=user_message,
            document_context=document_context,
            system_prompt=system_prompt,
            selection_text=selection_text,
            document_url=document_url
        )

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
                update = params.get("update", {})
                self._handle_session_update(update, queue)
            elif method in ("notifications/agent", "agent/update"):
                update = params.get("update", params)
                self._handle_agent_update(update, queue)

        if self._conn:
            self._conn.set_notification_callback(on_notification)

        # Send prompt
        try:
            if self._conn:
                result = self._conn.send_request("session/prompt", {
                    "sessionId": self._session_id,
                    "prompt": prompt_blocks,
                }, timeout=600)

                # Process the final response
                if result:
                    stop_reason = result.get("stopReason", result.get("stop_reason", ""))
                    log.info(f"Prompt completed: stop_reason={stop_reason}")

            queue.put((StreamQueueKind.STREAM_DONE, None))

        except TimeoutError:
            queue.put((StreamQueueKind.ERROR, format_error_payload(RuntimeError(f"{self.get_display_name()} prompt timed out"))))
        except Exception as e:
            if self._stop_requested:
                queue.put((StreamQueueKind.STOPPED,))
            else:
                log.error(f"Prompt error: {e}")
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
                self._conn.send_notification("session/interrupt", {
                    "sessionId": self._session_id
                })
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
        except Exception as e:
            log.error(f"Failed to submit approval: {e}")

    def shutdown(self):
        """Clean up resources."""
        if self._conn:
            self._conn.stop()
            self._conn = None
        self._session_id = None
