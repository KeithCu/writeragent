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
(``execute_on_main_thread``). Route reads and register/unregister share
``HttpRouteRegistry``'s lock. MCP toggle mutates the table while ``GET /``
iterates it; the lock is what keeps that from raising ``RuntimeError``.
"""

from __future__ import annotations

from plugin.framework.thread_guard import background
import json
import logging
import socket
import socketserver
import threading
import weakref
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any, cast
from plugin.framework.url_utils import get_url_path, get_url_query_dict
from plugin.framework.errors import safe_json_loads
from plugin.framework.worker_pool import run_in_background
from plugin.mcp.cors import reject_forbidden_origin, send_cors_headers
from plugin.mcp.http_trace import log_cors_preflight, log_http_request, log_no_route

if TYPE_CHECKING:
    from plugin.mcp.routes import HttpRouteRegistry

log = logging.getLogger("writeragent.framework.http_server")


def mcp_endpoint_url(host: str, port: int, use_ssl: bool = False) -> str:
    """Full streamable-HTTP MCP URL for external clients (LM Studio, Cursor, etc.)."""
    scheme = "https" if use_ssl else "http"
    return f"{scheme}://{host}:{port}/mcp"


# Shared with log.error on bind failure and the Toggle/Status/Settings msgbox so users
# see the same actionable text that used to live only in writeragent_debug.log (#379).
_PORT_IN_USE_GUIDANCE = "The port is in use by another process. Close whatever is holding it, or set mcp.mcp_port in Settings (or writeragent.json) to a free port, then try again. A local preview/viewer server may default to the same port."

# errno.EADDRINUSE is 98 (Linux) / 48 (macOS); Windows uses winerror 10048 (WSAEADDRINUSE).
_PORT_IN_USE_ERRNOS = frozenset({98, 48, 10048})


def write_http_json(handler: Any, status: int, data: Any, extra_headers: Any = None, indent: int | None = None) -> None:
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


def write_http_empty(handler: Any, status: int, extra_headers: Any = None) -> None:
    """Status-only response (204/202) with Content-Length: 0 so the client is not left reading to EOF."""
    handler.send_response(status)
    if extra_headers is not None:
        extra_headers(handler)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _sse_state(tcp_server: Any) -> tuple[threading.Event, Any, Any, threading.Lock]:
    """Per-listener keepalive tracking.

    serve_forever()/shutdown() does not join ThreadingMixIn request threads.
    State lives on that listener: stopping one server must not mark a
    different listener's streams as stopped. Weak refs so a handler that
    already exited does not pin the connection.
    """
    lock = getattr(tcp_server, "_sse_lock", None)
    if lock is None:
        lock = threading.Lock()
        tcp_server._sse_lock = lock
        tcp_server._sse_stop = threading.Event()
        tcp_server._sse_sockets = weakref.WeakSet()
        tcp_server._sse_threads = weakref.WeakSet()
    return tcp_server._sse_stop, tcp_server._sse_sockets, tcp_server._sse_threads, lock


def note_sse_keepalive(tcp_server: Any, sock: Any) -> threading.Event | None:
    """Register *sock* on *tcp_server*.

    Returns the stop event the loop must watch, or None when this listener
    is already stopped and the loop must not run.
    """
    if tcp_server is None or sock is None:
        return None
    stop, sockets, threads, lock = _sse_state(tcp_server)
    with lock:
        if stop.is_set():
            return None
        sockets.add(sock)
        threads.add(threading.current_thread())
        return stop


def forget_sse_keepalive(tcp_server: Any, sock: Any) -> None:
    """Drop a keepalive that has left its loop."""
    if tcp_server is None:
        return
    _stop, sockets, threads, lock = _sse_state(tcp_server)
    with lock:
        if sock is not None:
            sockets.discard(sock)
        threads.discard(threading.current_thread())


def _shutdown_sse_socket(sock: Any) -> None:
    """Wake select on *sock*, then close it.

    close() from another thread does not reliably interrupt select, and
    the fd can be reused under that wait. shutdown(SHUT_RDWR) marks the
    socket readable so the keepalive loop returns and checks the stop flag.
    """
    shutdown = getattr(sock, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
    close = getattr(sock, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def stop_sse_keepalives(tcp_server: Any) -> None:
    """End SSE loops still running on *tcp_server* after the accept loop exits.

    What was wrong: HttpServer.stop() only makes serve_forever() return.
    Each GET /mcp and GET /sse keepalive stays on its request thread until
    the client drops or the 15s select timeout, and a restart adds more.
    Why: set this listener's flag and shut down its registered sockets so
    those loops exit. The next HttpServer has its own flag.
    """
    if tcp_server is None:
        return
    stop, sockets, _threads, lock = _sse_state(tcp_server)
    with lock:
        stop.set()
        socks = list(sockets)
    if socks:
        log.info("Closing %d SSE keepalive socket(s)", len(socks))
    for sock in socks:
        _shutdown_sse_socket(sock)


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

    daemon_threads: bool = True


class GenericRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler that dispatches to registered routes."""

    route_registry: HttpRouteRegistry | None = None  # set by HttpServer.start()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        if reject_forbidden_origin(self):
            return
        path = get_url_path(self.path)
        log_cors_preflight(self, path)
        write_http_empty(self, 204, extra_headers=lambda h: send_cors_headers(h, preflight=True))

    def _dispatch(self, method: str) -> None:
        if reject_forbidden_origin(self):
            return
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

    def _read_body(self) -> Any:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length < 0:
            from plugin.framework.errors import AgentParsingError, format_error_payload

            log.warning("Invalid negative Content-Length: %s", content_length)
            err = AgentParsingError("Invalid negative Content-Length in HTTP request", details={"length": content_length})
            self._send_json(400, format_error_payload(err))
            return None
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

    def _send_json(self, status: int, data: Any) -> None:
        write_http_json(self, status, data)

    def log_message(self, format: str, *args: object) -> None:
        log.info("%s - %s", self.client_address[0], format % args)


class HttpServer:
    """Generic threaded HTTP server with optional TLS."""

    route_registry: Any
    port: int
    host: str
    use_ssl: bool
    ssl_cert: str
    ssl_key: str
    _running: bool

    def __init__(self, route_registry: Any, port: int, host: str = "localhost", use_ssl: bool = False, ssl_cert: str = "", ssl_key: str = "") -> None:
        self.route_registry = route_registry
        self.port = port
        self.host = host
        self.use_ssl = use_ssl
        self.ssl_cert = ssl_cert
        self.ssl_key = ssl_key
        self._server: Any = None
        self._thread: Any = None
        self._running = False

    def start(self) -> None:
        if self._running:
            log.warning("HTTP server is already running")
            return

        GenericRequestHandler.route_registry = self.route_registry

        # Single bind — no retry/sleep. A busy port used to block bootstrap and the Start MCP
        # menu for ~4s (5×1s). Stdio clients that start before LO are handled by mcp_bridge.py;
        # callers stash OSError and show _PORT_IN_USE_GUIDANCE in the UI.
        try:
            self._server = _ThreadedHTTPServer((self.host, self.port), GenericRequestHandler)
            # Before the accept thread exists, so request handlers share this state.
            _sse_state(self._server)
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
                try:
                    ssl_ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
                    if self._server:
                        self._server.socket = ssl_ctx.wrap_socket(self._server.socket, server_side=True)
                except Exception:
                    if self._server:
                        self._server.server_close()
                        self._server = None
                    raise
            else:
                log.warning("use_ssl is True but no certificates provided. Disabling TLS.")
                self.use_ssl = False

        self._running = True
        self._thread = run_in_background(self._run, daemon=True, name="http-server", dedicated=True)

        scheme = "https" if self.use_ssl else "http"
        url = "%s://%s:%s" % (scheme, self.host, self.port)
        log.info("HTTP server ready — %s (%d routes)", url, self.route_registry.route_count)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            if self._server:
                self._server.shutdown()
                self._server.server_close()
                log.info("HTTP server stopped")
        finally:
            # shutdown() does not join request threads. SSE keepalives are
            # still blocked in select until their sockets are closed.
            stop_sse_keepalives(self._server)

    @background
    def _run(self) -> None:
        try:
            if self._server:
                self._server.serve_forever()
        except Exception:
            if self._running:
                log.exception("HTTP server error")
        finally:
            # stop() sets _running False before shutdown() and server_close().
            # What was wrong: when serve_forever returned on its own, this
            # finally cleared _running and stopped SSE keepalives but left
            # the listen socket open. stop() then returned immediately, so
            # server_close() never ran and the port stayed bound.
            # Call server_close() only on that unexpected exit. The normal
            # stop() path has already closed the listener, so this branch
            # does not run and does not close it a second time.
            unexpected = self._running
            self._running = False
            if unexpected:
                try:
                    if self._server is not None:
                        self._server.server_close()
                finally:
                    stop_sse_keepalives(self._server)

    def is_running(self) -> bool:
        return self._running

    def get_status(self) -> dict[str, Any]:
        scheme = "https" if self.use_ssl else "http"
        base_url = "%s://%s:%s" % (scheme, self.host, self.port)
        return {"running": self._running, "host": self.host, "port": self.port, "ssl": self.use_ssl, "url": base_url, "mcp_url": mcp_endpoint_url(self.host, self.port, self.use_ssl), "routes": self.route_registry.route_count, "thread_alive": (self._thread.is_alive() if self._thread else False)}
