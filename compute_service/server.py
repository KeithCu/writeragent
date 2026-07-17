# WriterAgent - Python Compute Service Server
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lightweight HTTP server for sandboxed Python execution using standard wsgiref."""

from __future__ import annotations

import json
import os
import selectors
import socket
import sys
from http.server import ThreadingHTTPServer
from typing import Any

# Ensure repo root is on sys.path to resolve plugin.* / compute_service imports
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from compute_service.executor import execute_code, timeout_ms_to_sec

# Reject absurd bodies early. Kit should not send multi-GB grids.
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


def wsgi_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    path = environ.get('PATH_INFO', '')
    method = environ.get('REQUEST_METHOD', 'GET')
    
    if path == '/health' and method == 'GET':
        start_response('200 OK', [('Content-Type', 'application/json')])
        return [b'{"status": "healthy"}']
        
    if path == '/v1/execute' and method == 'POST':
        try:
            content_length = int(environ.get('CONTENT_LENGTH', 0))
        except ValueError:
            content_length = 0
            
        if content_length > _MAX_BODY_BYTES:
            start_response('413 Payload Too Large', [('Content-Type', 'application/json')])
            return [b'{"status": "error", "error": "Request body too large"}']
            
        try:
            body = environ['wsgi.input'].read(content_length)
            req_data = json.loads(body.decode('utf-8'))
        except Exception:
            start_response('400 Bad Request', [('Content-Type', 'application/json')])
            return [b'{"status": "error", "error": "Invalid JSON"}']
            
        if not isinstance(req_data, dict):
            start_response('400 Bad Request', [('Content-Type', 'application/json')])
            return [b'{"status": "error", "error": "JSON body must be an object"}']
            
        code = req_data.get("code")
        if not code or not isinstance(code, str):
            start_response('400 Bad Request', [('Content-Type', 'application/json')])
            return [b'{"status": "error", "error": "Missing \'code\' string parameter."}']

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
            
            try:
                response_body = json.dumps(result_payload, allow_nan=False).encode("utf-8")
                start_response('200 OK', [
                    ('Content-Type', 'application/json'),
                    ('Content-Length', str(len(response_body)))
                ])
                return [response_body]
            except (TypeError, ValueError) as e:
                response_body = json.dumps({"status": "error", "error": f"JSON encode failed: {e}"}, allow_nan=False).encode("utf-8")
                start_response('500 Internal Server Error', [
                    ('Content-Type', 'application/json'),
                    ('Content-Length', str(len(response_body)))
                ])
                return [response_body]
        except Exception as e:
            print(f"fail /v1/execute: {e}")
            response_body = json.dumps({"status": "error", "error": f"Server execution failure: {e}"}, allow_nan=False).encode("utf-8")
            start_response('500 Internal Server Error', [
                ('Content-Type', 'application/json'),
                ('Content-Length', str(len(response_body)))
            ])
            return [response_body]
            
    # Default 404
    start_response('404 Not Found', [('Content-Type', 'text/plain')])
    return [b'Not Found']


class DualStackThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that listens on both IPv4 and IPv6 loopback (or a single requested host/IP)."""
    def __init__(self, server_address: tuple[str, int], RequestHandlerClass: Any, bind_and_activate: bool = True) -> None:
        self.sockets: list[socket.socket] = []
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
                print(f"Warning: Failed to bind to {ip}:{port} ({family}): {e}", file=sys.stderr)

        if not self.sockets:
            raise OSError(f"Could not bind to any address for {host}:{port}")

        self.socket = self.sockets[0]
        self.address_family = self.socket.family
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


class WSGIDualStackServer:
    """Wrapper that mixes DualStackThreadingHTTPServer with wsgiref.simple_server.WSGIServer."""
    def __init__(self, host: str, port: int) -> None:
        from wsgiref.simple_server import WSGIServer, WSGIRequestHandler
        
        class _WSGIDualStackServer(DualStackThreadingHTTPServer, WSGIServer):
            def __init__(self, server_address: tuple[str, int], RequestHandlerClass: Any, bind_and_activate: bool = True) -> None:
                DualStackThreadingHTTPServer.__init__(self, server_address, RequestHandlerClass, bind_and_activate)
                self.server_name = socket.getfqdn(self.server_address[0])
                self.server_port = self.server_address[1]
                self.setup_environ()
                
        self.srv = _WSGIDualStackServer((host, port), WSGIRequestHandler)

    def set_app(self, app: Any) -> None:
        self.srv.set_app(app)

    def serve_forever(self) -> None:
        self.srv.serve_forever()

    def shutdown(self) -> None:
        self.srv.shutdown()

    def server_close(self) -> None:
        self.srv.server_close()


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    check_dependencies()
    print(f"Starting Python Compute Service on {host}:{port}...")
    server = WSGIDualStackServer(host, port)
    server.set_app(wsgi_app)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Python Compute Service...")
        server.server_close()


if __name__ == "__main__":
    host_env = os.environ.get("HOST", "127.0.0.1")
    port_env = os.environ.get("PORT", "8000")
    try:
        port_num = int(port_env)
    except ValueError:
        port_num = 8000
    run_server(host_env, port_num)
