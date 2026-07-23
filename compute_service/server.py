# WriterAgent - Python Compute Service Server
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Lightweight HTTP server for sandboxed Python execution using standard wsgiref."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import selectors
import socket
import sys
import threading
from http.server import ThreadingHTTPServer
from typing import Any, Callable

# Ensure repo root is on sys.path to resolve plugin.* / compute_service imports
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from compute_service.config import ComputeSettings, ConfigError, load_settings

ExecuteFn = Callable[..., dict[str, Any]]


def check_dependencies() -> None:
    """Verify required dependencies are importable; exit if missing."""
    try:
        import sympy  # noqa: F401
    except ImportError:
        print(
            "Error: sympy is not installed in the current Python environment.\n"
            "Please start the server using './compute_service/start.sh' or activate the correct virtual environment.",
            file=sys.stderr,
        )
        sys.exit(1)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, allow_nan=False).encode("utf-8")


def _start_json(
    start_response: Any,
    status: str,
    payload: dict[str, Any],
    *,
    extra_headers: list[tuple[str, str]] | None = None,
) -> list[bytes]:
    body = _json_bytes(payload)
    headers = [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(status, headers)
    return [body]


def authenticate_request(
    environ: dict[str, Any],
    settings: ComputeSettings,
) -> tuple[str | None, str | None]:
    """Validate Authorization when an API key is configured.

    Returns ``(principal, error)``. *principal* is ``settings.default_principal``
    on success (today always ``\"default\"``); *error* is set on failure.
    """
    if not settings.auth_required:
        return settings.default_principal, None

    raw = environ.get("HTTP_AUTHORIZATION")
    if not isinstance(raw, str) or not raw:
        return None, "missing"

    # Exact ``Bearer <token>`` — single space, case-sensitive scheme per coolwsd.
    prefix = "Bearer "
    if not raw.startswith(prefix):
        return None, "malformed"

    provided = raw[len(prefix) :]
    expected = settings.api_key
    if len(provided) != len(expected) or not hmac.compare_digest(provided, expected):
        return None, "invalid"
    return settings.default_principal, None


def create_wsgi_app(
    settings: ComputeSettings,
    *,
    execute_fn: ExecuteFn | None = None,
) -> Callable[[dict[str, Any], Any], list[bytes]]:
    """Build a WSGI app bound to *settings* (and optional test *execute_fn*).

    Executor imports are deferred until the first ``/v1/execute`` so config/auth
    startup does not pull WriterAgent ``plugin.framework.config``.
    """
    run_execute = execute_fn

    def wsgi_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        nonlocal run_execute
        path = environ.get("PATH_INFO", "")
        method = environ.get("REQUEST_METHOD", "GET")

        if path == "/health" and method == "GET":
            return _start_json(start_response, "200 OK", {"status": "healthy"})

        if path == "/v1/execute" and method == "POST":
            _principal, auth_err = authenticate_request(environ, settings)
            if auth_err is not None:
                # Generic body — do not reveal whether the key was missing vs wrong.
                return _start_json(
                    start_response,
                    "401 Unauthorized",
                    {"status": "error", "error": "Unauthorized"},
                    extra_headers=[("WWW-Authenticate", "Bearer")],
                )

            try:
                content_length = int(environ.get("CONTENT_LENGTH", 0))
            except ValueError:
                content_length = 0

            if content_length > settings.max_body_bytes:
                return _start_json(
                    start_response,
                    "413 Payload Too Large",
                    {"status": "error", "error": "Request body too large"},
                )

            try:
                body = environ["wsgi.input"].read(content_length)
                req_data = json.loads(body.decode("utf-8"))
            except Exception:
                return _start_json(
                    start_response,
                    "400 Bad Request",
                    {"status": "error", "error": "Invalid JSON"},
                )

            if not isinstance(req_data, dict):
                return _start_json(
                    start_response,
                    "400 Bad Request",
                    {"status": "error", "error": "JSON body must be an object"},
                )

            code = req_data.get("code")
            if not code or not isinstance(code, str):
                return _start_json(
                    start_response,
                    "400 Bad Request",
                    {"status": "error", "error": "Missing 'code' string parameter."},
                )

            data = req_data.get("data")
            session_id = req_data.get("session_id")
            mode = req_data.get("mode") or "isolated"
            if mode not in ("isolated", "shared"):
                mode = "isolated"
            init_script = req_data.get("init_script")
            if init_script is not None and not isinstance(init_script, str):
                init_script = None

            # Lazy: auth/config layer stays free of plugin.framework.config.
            from compute_service.executor import execute_code, timeout_ms_to_sec

            if run_execute is None:
                run_execute = execute_code

            timeout_sec = timeout_ms_to_sec(
                req_data.get("timeout_ms"),
                default_timeout_sec=settings.default_timeout_sec,
                max_timeout_sec=settings.max_timeout_sec,
            )
            sid = session_id if isinstance(session_id, str) else None
            print(
                f"exec /v1/execute mode={mode} session={sid!r} "
                f"code_len={len(code)} timeout={timeout_sec}s"
            )

            try:
                result_payload = run_execute(
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
                    return _start_json(start_response, "200 OK", result_payload)
                except (TypeError, ValueError) as e:
                    return _start_json(
                        start_response,
                        "500 Internal Server Error",
                        {"status": "error", "error": f"JSON encode failed: {e}"},
                    )
            except Exception as e:
                print(f"fail /v1/execute: {e}")
                return _start_json(
                    start_response,
                    "500 Internal Server Error",
                    {"status": "error", "error": f"Server execution failure: {e}"},
                )

        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"Not Found"]

    return wsgi_app


# Back-compat module-level app for older imports/tests — keyless loopback defaults.
wsgi_app = create_wsgi_app(ComputeSettings())


class DualStackThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that listens on both IPv4 and IPv6 loopback (or a single requested host/IP)."""

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: Any,
        bind_and_activate: bool = True,
    ) -> None:
        self.sockets: list[socket.socket] = []
        # Own shutdown state: BaseServer uses name-mangled ``__is_shut_down`` / ``__shutdown_request``
        # that type checkers cannot see; our multi-socket ``serve_forever`` must pair with ``shutdown``.
        self._dual_is_shut_down = threading.Event()
        self._dual_shutdown_request = False
        super().__init__(server_address, RequestHandlerClass, bind_and_activate=False)

        host, port = server_address

        bind_addresses: list[tuple[socket.AddressFamily, str]] = []
        if host in ("", "127.0.0.1", "::1", "localhost"):
            # Secure default: bind only to local loopback interface.
            bind_addresses = [
                (socket.AF_INET, "127.0.0.1"),
                (socket.AF_INET6, "::1"),
            ]
        elif host in ("0.0.0.0", "::"):
            # Wildcard binds (e.g. for Docker/container networking) allowed only when explicitly requested via HOST env.
            bind_addresses = [
                (socket.AF_INET, "0.0.0.0"),
                (socket.AF_INET6, "::"),
            ]
        else:
            try:
                infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
                seen_families = set()
                for family, _, _, _, sockaddr in infos:
                    if family not in seen_families:
                        seen_families.add(family)
                        bind_addresses.append((family, str(sockaddr[0])))
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
        self._dual_is_shut_down.clear()
        try:
            with selectors.DefaultSelector() as selector:
                for sock in self.sockets:
                    selector.register(sock, selectors.EVENT_READ)

                while not self._dual_shutdown_request:
                    ready = selector.select(poll_interval)
                    if self._dual_shutdown_request:
                        break
                    if ready:
                        for key, _ in ready:
                            ready_sock = key.fileobj
                            if isinstance(ready_sock, socket.socket):
                                self._handle_request_noblock_for_socket(ready_sock)
                    self.service_actions()
        finally:
            self._dual_shutdown_request = False
            self._dual_is_shut_down.set()

    def shutdown(self) -> None:
        """Stop ``serve_forever`` (must be called from another thread while it is running)."""
        self._dual_shutdown_request = True
        self._dual_is_shut_down.wait()

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
            except:  # noqa: E722 — match stdlib BaseServer
                self.shutdown_request(request)
                raise
        else:
            self.shutdown_request(request)


class WSGIDualStackServer:
    """Wrapper that mixes DualStackThreadingHTTPServer with wsgiref.simple_server.WSGIServer."""

    def __init__(self, host: str, port: int) -> None:
        from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

        class _WSGIDualStackServer(DualStackThreadingHTTPServer, WSGIServer):
            def __init__(
                self,
                server_address: tuple[str, int],
                RequestHandlerClass: Any,
                bind_and_activate: bool = True,
            ) -> None:
                DualStackThreadingHTTPServer.__init__(
                    self, server_address, RequestHandlerClass, bind_and_activate
                )
                self.server_name = socket.getfqdn(str(self.server_address[0]))
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


def run_server(settings: ComputeSettings) -> None:
    check_dependencies()
    auth_note = "auth=yes" if settings.auth_required else "auth=no (insecure)"
    print(f"Starting Python Compute Service on {settings.host}:{settings.port} ({auth_note})...")
    server = WSGIDualStackServer(settings.host, settings.port)
    server.set_app(create_wsgi_app(settings))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Python Compute Service...")
        server.server_close()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Standalone Python compute service for Collabora Online =PY()",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Path to python-compute.json (or set PYTHON_COMPUTE_CONFIG)",
    )
    parser.add_argument("--host", default=None, help="Bind host (overrides config/env)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (overrides config/env)")
    parser.add_argument(
        "--api-key-file",
        dest="api_key_file",
        default=None,
        help="Read Bearer shared secret from this file (preferred over argv secrets)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(
            config_path=args.config_path,
            host=args.host,
            port=args.port,
            api_key_file=args.api_key_file,
        )
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    run_server(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
