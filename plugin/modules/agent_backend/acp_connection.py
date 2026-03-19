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
"""Generic Agent Communication Protocol (ACP) adapter over stdio JSON-RPC.

Supports initial handshakes, prompt sessions, and streaming notifications
acting as an ACP client connected to a supporting agent binary backend.
"""

import json
import logging
import os
import subprocess
import threading

from plugin.framework.logging import debug_log

_LOG = "ABP"

_JSONRPC_VERSION = "2.0"
_ACP_PROTOCOL_VERSION = 1


class ACPConnection:
    """Manages a JSON-RPC stdio connection to an ACP subprocess."""

    def __init__(self, cmd_line, env=None, cwd=None):
        self._cmd_line = cmd_line
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
        """Spawn the ACP subprocess."""
        debug_log(f"Spawning: {' '.join(self._cmd_line)}", context=_LOG, level=logging.INFO)

        env = dict(os.environ)
        if self._env:
            env.update(self._env)

        self._proc = subprocess.Popen(
            self._cmd_line,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=self._cwd,
        )
        self._running = True
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="acp-reader"
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
            raise RuntimeError("ACP process is not running")

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
        debug_log(f"→ {method} (id={req_id})", context=_LOG, level=logging.DEBUG)

        try:
            self._proc.stdin.write(line.encode("utf-8"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            with self._lock:
                self._pending.pop(req_id, None)
            raise RuntimeError(f"Failed to write to ACP: {e}") from e

        if not event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            raise TimeoutError(f"ACP request {method} timed out after {timeout}s")

        with self._lock:
            entry = self._pending.pop(req_id, {})

        resp = entry.get("response")
        if resp and "error" in resp:
            err = resp["error"]
            msg_str = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"ACP error: {msg_str}")

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
        """Set a callback(method, params, msg_id) for incoming notifications."""
        self._notify_callback = callback

    def _reader_loop(self):
        """Read JSON-RPC messages from stdout and dispatch them."""
        debug_log("Reader loop started", context=_LOG, level=logging.INFO)
        while self._running and self._proc and self._proc.poll() is None:
            try:
                line = self._proc.stdout.readline()
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                idx = line.find('{')
                if idx >= 0:
                    line = line[idx:]

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    debug_log(f"Non-JSON output: {line[:200]}", context=_LOG, level=logging.DEBUG)
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
                        debug_log(f"Response for unknown id={req_id}", context=_LOG, level=logging.WARNING)
                else:
                    # Notification or Request from the agent
                    method = msg.get("method", "")
                    params = msg.get("params", {})
                    msg_id = msg.get("id")
                    if self._notify_callback:
                        try:
                            self._notify_callback(method, params, msg_id)
                        except Exception as e:
                            debug_log(f"Notification callback error: {e}", context=_LOG, level=logging.ERROR)

            except Exception as e:
                if self._running:
                    debug_log(f"Reader error: {e}", context=_LOG, level=logging.ERROR)
                break

        # Read stderr for debugging
        if self._proc and self._proc.stderr:
            try:
                stderr = self._proc.stderr.read()
                if stderr:
                    stderr_text = stderr.decode("utf-8", errors="replace")[:500]
                    debug_log(f"ACP stderr: {stderr_text}", context=_LOG, level=logging.WARNING)
            except Exception:
                pass

        debug_log("Reader loop ended", context=_LOG, level=logging.INFO)
