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
"""Generic threaded HTTP server with route dispatch.

Extracted from the MCP module so any module can register HTTP endpoints.
The server handles CORS, JSON encode/decode, and main-thread dispatch.
Route handlers are looked up from an HttpRouteRegistry instance.

Concurrency: the socket accept loop runs on its **own** daemon thread
(``run_in_background(..., dedicated=True, name="http-server")``) so it
does not occupy the short-job pool. Incoming HTTP is **not** the
LibreOffice UI thread. Anything that touches a document, a dialog, or
most UNO services must be posted through ``QueueExecutor``
(``execute_on_main_thread``). The route table is registered at server
start and then only read — no lock.
"""

from plugin.framework.thread_guard import background
import json
import logging
import socketserver
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, cast
from plugin.framework.url_utils import get_url_path, get_url_query_dict
from plugin.framework.errors import safe_json_loads
from plugin.framework.worker_pool import run_in_background
from plugin.mcp.cors import send_cors_headers
from plugin.mcp.http_trace import log_cors_preflight, log_http_request, log_no_route

log = logging.getLogger("writeragent.framework.http_server")


def mcp_endpoint_url(host: str, port: int, use_ssl: bool = False) -> str:
    """Full streamable-HTTP MCP URL for external clients (LM Studio, Cursor, etc.)."""
    scheme = "https" if use_ssl else "http"
    return f"{scheme}://{host}:{port}/mcp"


# Shared with log.error on bind failure and the Toggle/Status/Settings msgbox so users
# see the same actionable text that used to live only in writeragent_debug.log (#379).
_PORT_IN_USE_GUIDANCE = (
    "The port is in use by another process. "
    "Close whatever is holding it, or set mcp.mcp_port in Settings "
    "(or writeragent.json) to a free port, then try again. "
    "A local preview/viewer server may default to the same port."
)

# errno.EADDRINUSE is 98 (Linux) / 48 (macOS); Windows uses winerror 10048 (WSAEADDRINUSE).
_PORT_IN_USE_ERRNOS = frozenset({98, 48, 10048})


def write_http_json(handler, status, data, extra_headers=None, indent=None) -> None:
    """Send a JSON body with Content-Length and flush.

    ThreadingMixIn closes the client socket when the request thread exits.
    Without Content-Length, urllib on Darwin treats that close as
    ``ConnectionResetError`` while reading a 400 body (macOS CI
    ``test_post_unsupported_protocol_version``).
    """
    body = json.dumps(data, ensure_ascii=False, default=str, indent=indent).encode("utf-8")
    handler.send_response(status)
    if extra_headers is not None:
        extra_headers(handler)
    else:
        send_cors_headers(handler, preflight=False)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    flush = getattr(handler.wfile, "flush", None)
    if callable(flush):
        try:
            flush()
        except Exception:
            pass


def write_http_empty(handler, status, extra_headers=None) -> None:
    """Status-only response (204/202) with Content-Length: 0 so the client is not left reading to EOF."""
    handler.send_response(status)
    if extra_headers is not None:
        extra_headers(handler)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def is_port_in_use_error(exc: BaseException) -> bool:
    """True when *exc* is a bind failure because the TCP port is already taken."""
    if isinstance(exc, OSError):
        err = getattr(exc, "errno", None)
        if err in _PORT_IN_USE_ERRNOS:
            return True
        winerr = getattr(exc, "winerror", None)
        if winerr in _PORT_IN_USE_ERRNOS:
            return True
    msg = str(exc).lower()
    return "address already in use" in msg or "only one usage of each socket address" in msg


def format_mcp_start_failure(host: str, port: int | str, exc: BaseException) -> str:
    """Short user-facing body for MCP/HTTP start failures (no full traceback).

    Always includes host:port and the exception line. Port conflicts get the same
    guidance as the bind log so the dialog is actionable without opening the debug log.
    """
    endpoint = f"{host}:{port}"
    exc_line = f"{type(exc).__name__}: {exc}"
    lines = [f"Could not bind {endpoint} — {exc_line}"]
    if is_port_in_use_error(exc):
        lines.append(_PORT_IN_USE_GUIDANCE)
    return "\n".join(lines)


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request in its own thread."""

    daemon_threads = True


class GenericRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler that dispatches to registered routes."""

    route_registry = None  # HttpRouteRegistry, set by HttpServer.start()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        path = get_url_path(self.path)
        log_cors_preflight(self, path)
        write_http_empty(self, 204, extra_headers=lambda h: send_cors_headers(h, preflight=True))

    def _dispatch(self, method):
        path = get_url_path(self.path)
        log_http_request(self, method, path)
        route = self.route_registry.match(method, path) if self.route_registry else None

        if route is None:
            log_no_route(self, method, path)
            from plugin.framework.errors import WriterAgentException, format_error_payload

            err = WriterAgentException("Not found", code="NOT_FOUND", details={"path": path})
            self._send_json(404, format_error_payload(err))
            return

        try:
            if route.raw:
                if route.main_thread:
                    from plugin.framework.queue_executor import default_executor

                    default_executor.execute(route.handler, self)
                else:
                    route.handler(self)
            else:
                body = self._read_body()
                if body is None:
                    return  # _read_body already sent error response
                query = get_url_query_dict(self.path)
                if route.main_thread:
                    from plugin.framework.queue_executor import default_executor

                    result: Any = default_executor.execute(route.handler, body, self.headers, query)
                    status, data = cast("tuple[int, Any]", result)
                else:
                    result = route.handler(body, self.headers, query)
                    status, data = cast("tuple[int, Any]", result)
                self._send_json(status, data)
        except Exception as e:
            log.exception("%s %s failed", method, path)
            from plugin.framework.errors import format_error_payload

            self._send_json(500, format_error_payload(e))

    def _read_body(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length).decode("utf-8")
        data = safe_json_loads(raw, default=None, strict=True)
        if data is None and raw.strip():
            from plugin.framework.errors import AgentParsingError, format_error_payload

            log.warning("Invalid JSON body: %s", raw[:200])
            err = AgentParsingError("Invalid JSON body in HTTP request", details={"raw": raw[:200]})
            self._send_json(400, format_error_payload(err))
            return None
        return data if data is not None else {}

    def _send_json(self, status, data):
        write_http_json(self, status, data)

    def log_message(self, format: str, *args: object) -> None:
        log.info("%s - %s", self.client_address[0], format % args)


class HttpServer:
    """Generic threaded HTTP server with optional TLS."""

    def __init__(self, route_registry, port, host="localhost", use_ssl=False, ssl_cert="", ssl_key=""):
        self.route_registry = route_registry
        self.port = port
        self.host = host
        self.use_ssl = use_ssl
        self.ssl_cert = ssl_cert
        self.ssl_key = ssl_key
        self._server = None
        self._thread = None
        self._running = False

    def start(self):
        if self._running:
            log.warning("HTTP server is already running")
            return

        GenericRequestHandler.route_registry = self.route_registry

        # Single bind — no retry/sleep. A busy port used to block bootstrap and the Start MCP
        # menu for ~4s (5×1s). Stdio clients that start before LO are handled by mcp_bridge.py;
        # callers stash OSError and show _PORT_IN_USE_GUIDANCE in the UI.
        try:
            self._server = _ThreadedHTTPServer((self.host, self.port), GenericRequestHandler)
        except OSError:
            log.exception("Could not bind %s:%s — %s", self.host, self.port, _PORT_IN_USE_GUIDANCE)
            raise

        if self.use_ssl:
            # TLS server mode requires explicit certificates.
            # Local generation of certificates has been removed from ssl_helpers.
            if self.ssl_cert and self.ssl_key:
                cert_path, key_path = self.ssl_cert, self.ssl_key
                log.info("TLS using custom certs: %s", cert_path)
                import ssl

                ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                ssl_ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
                if self._server:
                    self._server.socket = ssl_ctx.wrap_socket(self._server.socket, server_side=True)
            else:
                log.warning("use_ssl is True but no certificates provided. Disabling TLS.")
                self.use_ssl = False

        self._running = True
        self._thread = run_in_background(self._run, daemon=True, name="http-server", dedicated=True)

        scheme = "https" if self.use_ssl else "http"
        url = "%s://%s:%s" % (scheme, self.host, self.port)
        log.info("HTTP server ready — %s (%d routes)", url, self.route_registry.route_count)

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            log.info("HTTP server stopped")

    @background
    def _run(self):
        try:
            if self._server:
                self._server.serve_forever()
        except Exception:
            if self._running:
                log.exception("HTTP server error")
        finally:
            self._running = False

    def is_running(self):
        return self._running

    def get_status(self):
        scheme = "https" if self.use_ssl else "http"
        base_url = "%s://%s:%s" % (scheme, self.host, self.port)
        return {
            "running": self._running,
            "host": self.host,
            "port": self.port,
            "ssl": self.use_ssl,
            "url": base_url,
            "mcp_url": mcp_endpoint_url(self.host, self.port, self.use_ssl),
            "routes": self.route_registry.route_count,
            "thread_alive": (self._thread.is_alive() if self._thread else False),
        }
