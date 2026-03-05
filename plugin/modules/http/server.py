"""Generic threaded HTTP server with route dispatch.

Extracted from the MCP module so any module can register HTTP endpoints.
The server handles CORS, JSON encode/decode, and main-thread dispatch.
Route handlers are looked up from an HttpRouteRegistry instance.
"""

import json
import logging
import socketserver
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

log = logging.getLogger("localwriter.framework.http_server")


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
        path = urlparse(self.path).path
        route = self.route_registry.match(method, path) if self.route_registry else None

        if route is None:
            self._send_json(404, {"error": "Not found"})
            return

        try:
            if route.raw:
                if route.main_thread:
                    from plugin.framework.main_thread import execute_on_main_thread
                    execute_on_main_thread(route.handler, self)
                else:
                    route.handler(self)
            else:
                body = self._read_body()
                if body is None:
                    return  # _read_body already sent error response
                query = parse_qs(urlparse(self.path).query)
                if route.main_thread:
                    from plugin.framework.main_thread import execute_on_main_thread
                    status, data = execute_on_main_thread(
                        route.handler, body, self.headers, query)
                else:
                    status, data = route.handler(body, self.headers, query)
                self._send_json(status, data)
        except Exception as e:
            log.error("%s %s error: %s", method, path, e, exc_info=True)
            self._send_json(500, {"error": str(e)})

    def _read_body(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Invalid JSON body: %s", raw[:200])
            self._send_json(400, {"error": "Invalid JSON"})
            return None

    def _send_json(self, status, data):
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(
            data, ensure_ascii=False, default=str).encode("utf-8"))

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, Mcp-Session-Id")
        self.send_header("Access-Control-Expose-Headers",
                         "Mcp-Session-Id")

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.client_address[0], fmt % args)


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
            from plugin.modules.http.ssl_certs import ensure_certs, create_ssl_context
            if self.ssl_cert and self.ssl_key:
                cert_path, key_path = self.ssl_cert, self.ssl_key
                log.info("TLS using custom certs: %s", cert_path)
            else:
                cert_path, key_path = ensure_certs()
                log.info("TLS using auto-generated certs: %s", cert_path)
            ssl_ctx = create_ssl_context(cert_path, key_path)
            self._server.socket = ssl_ctx.wrap_socket(
                self._server.socket, server_side=True)

        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="http-server")
        self._thread.start()

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
            self._server.serve_forever()
        except Exception as e:
            if self._running:
                log.error("HTTP server error: %s", e)
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
