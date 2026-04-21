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
"""

import json
import logging
import socketserver
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, cast
from plugin.framework.utils import get_url_path, get_url_query_dict

from plugin.framework.errors import safe_json_loads
from plugin.framework.worker_pool import run_in_background

log = logging.getLogger("writeragent.framework.http_server")


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
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def _dispatch(self, method):
        path = get_url_path(self.path)
        route = self.route_registry.match(method, path) if self.route_registry else None

        if route is None:
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
                    result: Any = default_executor.execute(
                        route.handler, body, self.headers, query)
                    status, data = cast("tuple[int, Any]", result)
                else:
                    result = route.handler(body, self.headers, query)
                    status, data = cast("tuple[int, Any]", result)
                self._send_json(status, data)
        except Exception as e:
            log.error("%s %s error: %s", method, path, e, exc_info=True)
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
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(
            data, ensure_ascii=False, default=str).encode("utf-8"))

    def _send_cors_headers(self):
        origin = self.headers.get("Origin")
        if origin:
            import re
            if re.match(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", origin):
                self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, Mcp-Session-Id, X-Document-URL")
        self.send_header("Access-Control-Expose-Headers",
                         "Mcp-Session-Id")

    def log_message(self, format: str, *args: object) -> None:
        log.info("%s - %s", self.client_address[0], format % args)


class HttpServer:
    """Generic threaded HTTP server with optional TLS."""

    def __init__(self, route_registry, port=8766, host="localhost",
                 use_ssl=False, ssl_cert="", ssl_key=""):
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

        self._server = _ThreadedHTTPServer(
            (self.host, self.port), GenericRequestHandler)

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
                    self._server.socket = ssl_ctx.wrap_socket(
                        self._server.socket, server_side=True)
            else:
                log.warning("use_ssl is True but no certificates provided. Disabling TLS.")
                self.use_ssl = False

        self._running = True
        self._thread = run_in_background(
            self._run, daemon=True, name="http-server")

        scheme = "https" if self.use_ssl else "http"
        url = "%s://%s:%s" % (scheme, self.host, self.port)
        log.info("HTTP server ready — %s (%d routes)",
                 url, self.route_registry.route_count)

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            log.info("HTTP server stopped")

    def _run(self):
        try:
            if self._server:
                self._server.serve_forever()
        except Exception as e:
            if self._running:
                log.error("HTTP server error: %s", type(e).__name__)
        finally:
            self._running = False

    def is_running(self):
        return self._running

    def get_status(self):
        scheme = "https" if self.use_ssl else "http"
        return {
            "running": self._running,
            "host": self.host,
            "port": self.port,
            "ssl": self.use_ssl,
            "url": "%s://%s:%s" % (scheme, self.host, self.port),
            "routes": self.route_registry.route_count,
            "thread_alive": (self._thread.is_alive()
                             if self._thread else False),
        }
