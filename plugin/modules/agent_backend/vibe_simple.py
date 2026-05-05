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
"""Mistral Vibe agent backend using the shared ACPBackend base class."""

import logging
from typing import Dict

from plugin.modules.agent_backend.acp_backend import ACPBackend
from plugin.framework.async_stream import StreamQueueKind
from plugin.framework.config import get_config, get_api_key_for_endpoint
from plugin.framework.errors import format_error_payload

log = logging.getLogger(__name__)


class VibeBackend(ACPBackend):
    """ACP-based Mistral Vibe backend."""

    backend_id = "vibe"

    def get_binary_name(self) -> str:
        """Return the binary name to search for."""
        return "vibe-acp"

    def get_display_name(self) -> str:
        """Return display name for UI."""
        return "Mistral Vibe (ACP)"

    def get_agent_name(self) -> str:
        """Return ACP agent name."""
        return "vibe"

    def get_env_vars(self) -> Dict[str, str]:
        """Return environment variables to pass to subprocess."""
        env = {}
        try:
            # Forward API key to Vibe if available
            endpoint = str(get_config(self._ctx, "ai.endpoint") or "")
            key = get_api_key_for_endpoint(self._ctx, endpoint)
            if key:
                env["MISTRAL_API_KEY"] = key
                log.info("Using MISTRAL_API_KEY from general settings")
        except Exception:
            pass
        return env

    def send(self, queue, user_message, document_context, document_url, system_prompt=None, mcp_url=None, selection_text=None, stop_checker=None, **kwargs):
        """Send a message via ACP stdio - Vibe-specific implementation."""
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
            log.info(f"Notification received: method={method}, params keys={list(params.keys()) if params else []}")  # DEBUG
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
                result = self._conn.send_request("session/prompt", {"sessionId": self._session_id, "prompt": prompt_blocks}, timeout=600)

                # Process the final response - Vibe returns content in result
                log.info(f"Vibe prompt result: {result}")  # DEBUG: Log full result

                if result:
                    stop_reason = result.get("stopReason", result.get("stop_reason", ""))
                    log.info(f"Prompt completed: stop_reason={stop_reason}")

                    # Check if Vibe returned content in the result
                    content_blocks = result.get("contentBlocks", [])
                    log.info(f"Found {len(content_blocks)} content blocks")  # DEBUG: Log block count

                    if content_blocks:
                        for i, block in enumerate(content_blocks):
                            log.info(f"Processing block {i}: {block}")  # DEBUG: Log each block
                            if isinstance(block, dict):
                                if block.get("type") == "text":
                                    text = block.get("text", "")
                                    log.info(f"Queueing text chunk: {text[:50]}...")  # DEBUG: Log text
                                    queue.put((StreamQueueKind.CHUNK, text))
                                elif block.get("type") == "tool_call":
                                    log.info(f"Queueing tool call: {block}")  # DEBUG: Log tool call
                                    queue.put((StreamQueueKind.TOOL_CALL, block))
                                elif block.get("type") == "tool_result":
                                    log.info(f"Queueing tool result: {block}")  # DEBUG: Log tool result
                                    queue.put((StreamQueueKind.TOOL_RESULT, block))
                    else:
                        log.warning("No contentBlocks found in Vibe response")
                        # Check for other possible response formats
                        if "content" in result:
                            log.info(f"Found 'content' field: {result['content']}")
                        if "output" in result:
                            log.info(f"Found 'output' field: {result['output']}")
                        if "response" in result:
                            log.info(f"Found 'response' field: {result['response']}")

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
