# WriterAgent - Python Compute Service Server
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lightweight HTTP server for sandboxed Python execution."""

import base64
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Ensure repo root is on sys.path to resolve plugin.* imports
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from compute_service.executor import execute_code

def _encode_bytes_to_base64(obj: Any) -> Any:
    """Recursively encode all bytes instances in the object to base64 strings."""
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("utf-8")
    elif isinstance(obj, dict):
        return {k: _encode_bytes_to_base64(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_encode_bytes_to_base64(x) for x in obj]
    elif isinstance(obj, tuple):
        return tuple(_encode_bytes_to_base64(x) for x in obj)
    return obj

class ComputeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "healthy"}).encode("utf-8"))
            return
        
        self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        if self.path != "/v1/execute":
            self.send_error(404, "Not Found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if not content_length:
            self.send_error(400, "Bad Request: Missing Content-Length")
            return

        body = self.rfile.read(content_length)
        try:
            req_data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400, "Bad Request: Invalid JSON")
            return

        code = req_data.get("code")
        if not code or not isinstance(code, str):
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": "Missing 'code' string parameter."}).encode("utf-8"))
            return

        data = req_data.get("data")
        session_id = req_data.get("session_id")
        timeout_ms = req_data.get("timeout_ms")
        
        # Default to 30 seconds
        timeout_sec = 30
        if isinstance(timeout_ms, int) and timeout_ms > 0:
            timeout_sec = max(1, timeout_ms // 1000)

        try:
            result_payload = execute_code(
                code=code,
                data=data,
                session_id=session_id,
                timeout_sec=timeout_sec,
            )
            
            # Base64-encode any binary bytes (e.g. image payloads) so it's JSON-safe
            safe_payload = _encode_bytes_to_base64(result_payload)
            
            response_body = json.dumps(safe_payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "error",
                "message": f"Server execution failure: {str(e)}"
            }).encode("utf-8"))

def run_server(port: int = 8000) -> None:
    server_address = ("", port)
    httpd = ThreadingHTTPServer(server_address, ComputeHandler)
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
