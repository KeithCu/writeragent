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


def check_dependencies() -> None:
    """Verify required dependencies are importable; exit if missing."""
    try:
        import sympy  # noqa: F401
    except ImportError:
        print(
            "Error: sympy is not installed in the current Python environment.\n"
            "Please start the server using './compute_service/start.sh' or activate the correct virtual environment.",
            file=sys.stderr
        )
        sys.exit(1)



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


import selectors
import socket

class DualStackThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that listens on both IPv4 and IPv6 loopback (or a single requested host/IP)."""
    def __init__(self, server_address: tuple[str, int], RequestHandlerClass: type[BaseHTTPRequestHandler], bind_and_activate: bool = True) -> None:
        self.sockets: list[socket.socket] = []
        # Call the super constructor with bind_and_activate=False, so we can set up our sockets ourselves.
        super().__init__(server_address, RequestHandlerClass, bind_and_activate=False)

        host, port = server_address

        bind_addresses = []
        if host in ("", "127.0.0.1", "::1", "localhost"):
            # Secure default: bind only to local loopback interface.
            bind_addresses = [
                (socket.AF_INET, "127.0.0.1"),
                (socket.AF_INET6, "::1")
            ]
        elif host in ("0.0.0.0", "::"):
            # Wildcard binds (e.g. for Docker/container networking) allowed only when explicitly requested via HOST env.
            bind_addresses = [
                (socket.AF_INET, "0.0.0.0"),
                (socket.AF_INET6, "::")
            ]
        else:
            try:
                infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
                seen_families = set()
                for family, _, _, _, sockaddr in infos:
                    if family not in seen_families:
                        seen_families.add(family)
                        bind_addresses.append((family, sockaddr[0]))
            except Exception:
                bind_addresses = [(socket.AF_INET, host)]

        for family, ip in bind_addresses:
            try:
                sock = socket.socket(family, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    try:
                        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    except OSError:
                        pass
                sock.bind((ip, port))
                self.sockets.append(sock)
            except OSError as e:
                # Log warning to stderr, but continue if other binds succeed
                print(f"Warning: Failed to bind to {ip}:{port} ({family}): {e}", file=sys.stderr)

        if not self.sockets:
            raise OSError(f"Could not bind to any address for {host}:{port}")

        # For compatibility with any code checking self.socket or self.address_family
        self.socket = self.sockets[0]
        self.address_family = self.socket.family
        # Retrieve actual bound port from first successful socket (especially if port was 0)
        actual_port = self.socket.getsockname()[1]
        self.server_address = (host, actual_port)

        if bind_and_activate:
            try:
                self.server_activate()
            except Exception:
                self.server_close()
                raise

    def server_activate(self) -> None:
        for sock in self.sockets:
            sock.listen(self.request_queue_size)

    def server_close(self) -> None:
        for sock in self.sockets:
            try:
                sock.close()
            except Exception:
                pass

    def fileno(self) -> int:
        return self.socket.fileno()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self._BaseServer__is_shut_down.clear()
        try:
            with selectors.DefaultSelector() as selector:
                for sock in self.sockets:
                    selector.register(sock, selectors.EVENT_READ)

                while not self._BaseServer__shutdown_request:
                    ready = selector.select(poll_interval)
                    if self._BaseServer__shutdown_request:
                        break
                    if ready:
                        for key, _ in ready:
                            self._handle_request_noblock_for_socket(key.fileobj)
                    self.service_actions()
        finally:
            self._BaseServer__shutdown_request = False
            self._BaseServer__is_shut_down.set()

    def _handle_request_noblock_for_socket(self, sock: socket.socket) -> None:
        try:
            request, client_address = sock.accept()
        except OSError:
            return
        if self.verify_request(request, client_address):
            try:
                self.process_request(request, client_address)
            except Exception:
                self.handle_error(request, client_address)
                self.shutdown_request(request)
            except:
                self.shutdown_request(request)
                raise
        else:
            self.shutdown_request(request)


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    check_dependencies()
    httpd = DualStackThreadingHTTPServer((host, port), ComputeHandler)
    print(f"Starting Python Compute Service on {host}:{port}...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Python Compute Service...")
        httpd.server_close()


if __name__ == "__main__":
    host_env = os.environ.get("HOST", "127.0.0.1")
    port_env = os.environ.get("PORT", "8000")
    try:
        port_num = int(port_env)
    except ValueError:
        port_num = 8000
    run_server(host_env, port_num)
