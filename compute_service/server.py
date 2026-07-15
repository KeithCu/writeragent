# WriterAgent - Python Compute Service Server
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lightweight stdlib HTTP server for sandboxed Python execution (no FastAPI)."""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Ensure repo root is on sys.path to resolve plugin.* / compute_service imports
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from compute_service.executor import execute_code, timeout_ms_to_sec

# Reject absurd bodies early (bytes). Kit should not send multi-GB grids.
_MAX_BODY_BYTES = 32 * 1024 * 1024


class ComputeHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        # Quieter default for ThreadingHTTPServer under tests; still prints.
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        print(f"recv GET {path} from {self.address_string()}")
        if path == "/health":
            self._send_json(200, {"status": "healthy"})
            return
        self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        print(f"recv POST {path} from {self.address_string()}")
        if path != "/v1/execute":
            self.send_error(404, "Not Found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            self._send_json(400, {"status": "error", "error": "Missing Content-Length"})
            return
        if content_length > _MAX_BODY_BYTES:
            self._send_json(413, {"status": "error", "error": "Request body too large"})
            return

        body = self.rfile.read(content_length)
        try:
            req_data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            print("POST /v1/execute rejected: invalid JSON")
            self._send_json(400, {"status": "error", "error": "Invalid JSON"})
            return

        if not isinstance(req_data, dict):
            print("POST /v1/execute rejected: body not an object")
            self._send_json(400, {"status": "error", "error": "JSON body must be an object"})
            return

        code = req_data.get("code")
        if not code or not isinstance(code, str):
            print("POST /v1/execute rejected: missing code")
            self._send_json(400, {"status": "error", "error": "Missing 'code' string parameter."})
            return

        data = req_data.get("data")
        session_id = req_data.get("session_id")
        mode = req_data.get("mode") or "isolated"
        if mode not in ("isolated", "shared"):
            mode = "isolated"
        init_script = req_data.get("init_script")
        if init_script is not None and not isinstance(init_script, str):
            init_script = None

        timeout_sec = timeout_ms_to_sec(req_data.get("timeout_ms"))
        sid = session_id if isinstance(session_id, str) else None
        print(f"exec /v1/execute mode={mode} session={sid!r} code_len={len(code)} timeout={timeout_sec}s")

        try:
            result_payload = execute_code(
                code=code,
                data=data,
                session_id=sid,
                timeout_sec=timeout_sec,
                mode=mode,
                init_script=init_script,
            )
            status = result_payload.get("status") if isinstance(result_payload, dict) else None
            print(f"done /v1/execute status={status!r}")
            self._send_json(200, result_payload)
        except Exception as e:
            print(f"fail /v1/execute: {e}")
            self._send_json(500, {"status": "error", "error": f"Server execution failure: {e}"})

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        try:
            response_body = json.dumps(payload, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as e:
            # Last-resort: should not happen after normalize_execute_response
            response_body = json.dumps({"status": "error", "error": f"JSON encode failed: {e}"}, allow_nan=False).encode("utf-8")
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)


def run_server(port: int = 8000) -> None:
    httpd = ThreadingHTTPServer(("", port), ComputeHandler)
    print(f"Starting Python Compute Service on port {port}...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Python Compute Service...")
        httpd.server_close()


if __name__ == "__main__":
    port_env = os.environ.get("PORT", "8000")
    try:
        port_num = int(port_env)
    except ValueError:
        port_num = 8000
    run_server(port_num)
