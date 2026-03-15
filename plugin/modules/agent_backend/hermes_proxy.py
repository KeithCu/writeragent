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

from plugin.modules.agent_backend.base import AgentBackend
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


class HermesACPConnection:
    """Manages a JSON-RPC stdio connection to a hermes acp subprocess."""

    def __init__(self, cmd, args=None, env=None, cwd=None):
        self._cmd = cmd
        self._args = args or []
        self._env = env
        self._cwd = cwd
        self._proc = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._pending = {}         # id -> threading.Event, response dict
        self._reader_thread = None
        self._running = False
        self._notifications = []   # queue of notification dicts
        self._notify_callback = None

    def start(self):
        """Spawn the hermes acp subprocess."""
        cmd_line = [self._cmd] + self._args
        # If the command is 'hermes' (not hermes-acp), add 'acp' subcommand
        if os.path.basename(self._cmd) == "hermes":
            cmd_line = [self._cmd, "acp"] + self._args

        debug_log(f"Spawning: {' '.join(cmd_line)}", context=_LOG)

        env = dict(os.environ)
        if self._env:
            env.update(self._env)
        # Ensure hermes knows where its home is
        if "HERMES_HOME" not in env:
            hermes_home = os.path.join(os.path.expanduser("~"), ".hermes")
            if os.path.isdir(hermes_home):
                env["HERMES_HOME"] = hermes_home

        self._proc = subprocess.Popen(
            cmd_line,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=self._cwd,
        )
        self._running = True
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="hermes-acp-reader"
        )
        self._reader_thread.start()

    def stop(self):
        """Terminate the subprocess."""
        self._running = False
        if self._proc:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    @property
    def is_alive(self):
        return self._proc is not None and self._proc.poll() is None

    def _next_id(self):
        with self._lock:
            self._request_id += 1
            return self._request_id

    def send_request(self, method, params=None, timeout=120):
        """Send a JSON-RPC request and wait for the response."""
        if not self.is_alive:
            raise RuntimeError("Hermes ACP process is not running")

        req_id = self._next_id()
        msg = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": req_id,
            "method": method,
            "params": params or {},
        }

        event = threading.Event()
        with self._lock:
            self._pending[req_id] = {"event": event, "response": None}

        line = json.dumps(msg) + "\n"
        debug_log(f"→ {method} (id={req_id})", context=_LOG)

        try:
            self._proc.stdin.write(line.encode("utf-8"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            with self._lock:
                self._pending.pop(req_id, None)
            raise RuntimeError(f"Failed to write to Hermes ACP: {e}") from e

        if not event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            raise TimeoutError(f"Hermes ACP request {method} timed out after {timeout}s")

        with self._lock:
            entry = self._pending.pop(req_id, {})

        resp = entry.get("response")
        if resp and "error" in resp:
            err = resp["error"]
            msg_str = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"Hermes ACP error: {msg_str}")

        return resp.get("result") if resp else None

    def send_notification(self, method, params=None):
        """Send a JSON-RPC notification (no response expected)."""
        if not self.is_alive:
            return
        msg = {
            "jsonrpc": _JSONRPC_VERSION,
            "method": method,
            "params": params or {},
        }
        line = json.dumps(msg) + "\n"
        try:
            self._proc.stdin.write(line.encode("utf-8"))
            self._proc.stdin.flush()
        except Exception:
            pass

    def set_notification_callback(self, callback):
        """Set a callback(method, params) for incoming notifications."""
        self._notify_callback = callback

    def _reader_loop(self):
        """Read JSON-RPC messages from stdout and dispatch them."""
        debug_log("Reader loop started", context=_LOG)
        while self._running and self._proc and self._proc.poll() is None:
            try:
                line = self._proc.stdout.readline()
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    debug_log(f"Non-JSON output: {line[:200]}", context=_LOG)
                    continue

                if "id" in msg and msg["id"] is not None and "method" not in msg:
                    # Response to our request
                    req_id = msg["id"]
                    with self._lock:
                        entry = self._pending.get(req_id)
                    if entry:
                        entry["response"] = msg
                        entry["event"].set()
                    else:
                        debug_log(f"Response for unknown id={req_id}", context=_LOG)
                else:
                    # Notification or Request from the agent
                    method = msg.get("method", "")
                    params = msg.get("params", {})
                    msg_id = msg.get("id")
                    if self._notify_callback:
                        try:
                            self._notify_callback(method, params, msg_id)
                        except Exception as e:
                            debug_log(f"Notification callback error: {e}", context=_LOG)

            except Exception as e:
                if self._running:
                    debug_log(f"Reader error: {e}", context=_LOG)
                break

        # Read stderr for debugging
        if self._proc and self._proc.stderr:
            try:
                stderr = self._proc.stderr.read()
                if stderr:
                    stderr_text = stderr.decode("utf-8", errors="replace")[:500]
                    debug_log(f"Hermes stderr: {stderr_text}", context=_LOG)
            except Exception:
                pass

        debug_log("Reader loop ended", context=_LOG)


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
            path = str(get_config(self._ctx, "agent_backend.path") or "").strip()
            if path and not path.startswith("http"):
                self._hermes_cmd = path
            else:
                cmd, _ = _find_hermes_binary()
                self._hermes_cmd = cmd

            args_str = str(get_config(self._ctx, "agent_backend.args") or "").strip()
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

        self._conn = HermesACPConnection(
            cmd=self._hermes_cmd,
            args=self._hermes_args,
        )
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
        if method == "notifications/session":
            update = params.get("update", {})
            self._handle_session_update(update, queue)
        elif method == "notifications/agent":
            update = params.get("update", params)
            self._handle_agent_update(update, queue)
        else:
            debug_log(f"Unhandled notification: {method}", context=_LOG)

    def _handle_session_update(self, update, queue):
        """Handle a session update notification."""
        session_update = update.get("session_update", update.get("sessionUpdate", ""))

        if session_update == "text":
            # Streaming text from the agent
            text = update.get("text", "")
            if text:
                queue.put(("chunk", text))

        elif session_update == "tool_call":
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
