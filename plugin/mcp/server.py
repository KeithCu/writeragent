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
import threading
import time
import weakref
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any, ClassVar, cast
from plugin.framework.url_utils import get_url_path, get_url_query_dict
from plugin.framework.errors import safe_json_loads
from plugin.framework.worker_pool import run_in_background
from plugin.framework.http_server import (
    DualStackThreadPoolHTTPServer,
    _PORT_IN_USE_GUIDANCE as _PORT_IN_USE_GUIDANCE,
    format_bind_failure,
    is_port_in_use_error as is_port_in_use_error,
)
from plugin.mcp.cors import reject_forbidden_host, reject_forbidden_origin, send_cors_headers
from plugin.mcp.http_trace import log_cors_preflight, log_http_request, log_no_route

if TYPE_CHECKING:
    from plugin.mcp.routes import HttpRouteRegistry

log = logging.getLogger("writeragent.framework.http_server")

# Maximum concurrent SSE streams to prevent thread exhaustion.
SSE_MAX_STREAMS = 8

# MCP JSON-RPC bodies are tool arguments, not file uploads. A few MiB is
# enough for a document slice and small enough that one request cannot
# force a multi-gigabyte allocation in the ThreadingMixIn worker.
MCP_HTTP_MAX_BODY_BYTES = 4 * 1024 * 1024

# rfile.read blocks forever when BaseHTTPRequestHandler.timeout is None.
# A client that sends Content-Length and then stalls held one request
# thread until the process exited. 30s fails that read closed. SSE
# keepalive waits in select(), which does not follow this socket timeout,
# so a long GET /mcp is not cut off by it.
MCP_HTTP_SOCKET_TIMEOUT_SEC = 30.0


def mcp_endpoint_url(host: str, port: int, use_ssl: bool = False) -> str:
    """Full streamable-HTTP MCP URL for external clients (LM Studio, Cursor, etc.)."""
    scheme = "https" if use_ssl else "http"
    return f"{scheme}://{host}:{port}/mcp"


def write_http_json(handler: Any, status: int, data: Any, extra_headers: Any = None, indent: int | None = None) -> None:
    """Send a JSON body with Content-Length and flush.

    ThreadingMixIn closes the client socket when the request thread exits.
    Without Content-Length, urllib on Darwin treats that close as
    ``ConnectionResetError`` while reading a 400 body (macOS CI
    ``test_post_unsupported_protocol_version``).
    """
    body = json.dumps(data, ensure_ascii=False, default=str, indent=indent).encode("utf-8")
    handler._response_started = True
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
        except OSError:
            # Client disconnect during flush (BrokenPipeError, ConnectionResetError).
            pass


def _transfer_encoding_is_chunked(handler: Any) -> bool:
    """True when Transfer-Encoding contains chunked.

    Same predicate as compute_service.server._transfer_encoding_is_chunked.
    Not imported: plugin must not depend on compute_service. This listener
    is HTTP/1.0 and reads Content-Length. Chunked is not decoded.
    """
    headers = getattr(handler, "headers", None)
    raw = headers.get("Transfer-Encoding") if headers is not None else None
    if not isinstance(raw, str):
        return False
    return "chunked" in raw.lower()


def read_json_body(handler: Any) -> tuple[Any, tuple[int, BaseException] | None]:
    """Parse a JSON object body, or ``(None, (status, error))`` when it is refused.

    The caller writes the error response. A Transfer-Encoding containing
    chunked is HTTP 400 and is not read, including when Content-Length is
    missing, zero, or set. Negative Content-Length stays 400. A length above
    :data:`MCP_HTTP_MAX_BODY_BYTES` is 413 and is not read. A socket timeout
    while reading a body that is under the cap is 408. An empty body with no
    chunked coding is still ``{}`` and does not touch ``rfile``.
    """
    from plugin.framework.errors import AgentParsingError

    if _transfer_encoding_is_chunked(handler):
        # What was wrong: a missing or zero Content-Length returned {}
        # without reading rfile. Transfer-Encoding: chunked was then
        # handled as an empty object (MCP reported Invalid JSON-RPC) and
        # the chunk bytes stayed on the socket.
        # How: the length check ran first and treated 0 as empty. A
        # Content-Length next to chunked would also have been trusted.
        # Why: refuse with 400 before any read, matching compute_service
        # _reject_chunked. close_connection stops keep-alive from parsing
        # the leftover bytes as the next request. No chunk parser.
        handler.close_connection = True
        log.warning("Rejecting chunked Transfer-Encoding")
        err = AgentParsingError(
            "Chunked transfer encoding is not supported",
            details={"transfer_encoding": handler.headers.get("Transfer-Encoding")},
        )
        return None, (400, err)

    raw_length = handler.headers.get("Content-Length", 0)
    try:
        content_length = int(raw_length)
    except (TypeError, ValueError):
        err = AgentParsingError("Invalid Content-Length in HTTP request", details={"length": raw_length})
        return None, (400, err)
    if content_length < 0:
        # BaseHTTPRequestHandler / rfile.read treats a negative size as "read
        # until EOF". Content-Length: -1 would block the worker until the
        # socket closed. Reject before any read.
        log.warning("Invalid negative Content-Length: %s", content_length)
        err = AgentParsingError("Invalid negative Content-Length in HTTP request", details={"length": content_length})
        return None, (400, err)
    if content_length == 0:
        setattr(handler, "_body_read", True)
        return {}, None
    if content_length > MCP_HTTP_MAX_BODY_BYTES:
        # A trusted Content-Length with no ceiling pins the worker on the
        # allocation, and a stalled body with no socket timeout pins it
        # forever. Refuse the length before the read. The handler/server
        # timeout covers a stall whose declared length is still under the cap.
        log.warning("Rejecting oversized Content-Length: %s", content_length)
        err = AgentParsingError("HTTP body exceeds %s bytes" % MCP_HTTP_MAX_BODY_BYTES, details={"length": content_length, "max": MCP_HTTP_MAX_BODY_BYTES})
        return None, (413, err)
    try:
        raw_bytes = handler.rfile.read(content_length)
        setattr(handler, "_body_read", True)
    except TimeoutError:
        log.warning("Timed out reading HTTP body (%s bytes declared)", content_length)
        err = AgentParsingError("Timed out reading HTTP body", details={"length": content_length})
        return None, (408, err)
    if isinstance(raw_bytes, str):
        raw = raw_bytes
    else:
        try:
            raw = bytes(raw_bytes).decode("utf-8")
        except UnicodeDecodeError:
            err = AgentParsingError("HTTP body is not UTF-8", details={"length": content_length})
            return None, (400, err)
    data = safe_json_loads(raw, default=None, strict=True)
    if data is None and raw.strip():
        log.warning("Invalid JSON body: %s", raw[:200])
        err = AgentParsingError("Invalid JSON body in HTTP request", details={"raw": raw[:200]})
        return None, (400, err)
    return (data if data is not None else {}), None


def write_http_empty(handler: Any, status: int, extra_headers: Any = None) -> None:
    """Status-only response (204/202) with Content-Length: 0 so the client is not left reading to EOF."""
    handler._response_started = True
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


def try_reserve_sse(tcp_server: Any, sock: Any = None) -> threading.Event | None:
    """Reserve an SSE stream slot on *tcp_server*.

    Returns the stop event the loop must watch, or None when this listener
    is already stopped or the stream cap is reached.
    """
    if tcp_server is None:
        return None
    stop, sockets, threads, lock = _sse_state(tcp_server)
    with lock:
        count = getattr(tcp_server, "_sse_count", 0)
        active = max(count, len(threads))
        if stop.is_set() or active >= SSE_MAX_STREAMS:
            return None
        tcp_server._sse_count = count + 1
        if sock is not None:
            sockets.add(sock)
        threads.add(threading.current_thread())
        return stop


def note_sse_keepalive(tcp_server: Any, sock: Any) -> threading.Event | None:
    """Register *sock* on *tcp_server*.

    Returns the stop event the loop must watch, or None when this listener
    is already stopped and the loop must not run.
    """
    if tcp_server is None or sock is None:
        return None
    return try_reserve_sse(tcp_server, sock)


def forget_sse_keepalive(tcp_server: Any, sock: Any = None) -> None:
    """Drop a keepalive that has left its loop and release its reservation."""
    if tcp_server is None:
        return
    _stop, sockets, threads, lock = _sse_state(tcp_server)
    with lock:
        if sock is not None:
            sockets.discard(sock)
        threads.discard(threading.current_thread())
        count = getattr(tcp_server, "_sse_count", 0)
        if count > 0:
            tcp_server._sse_count = count - 1


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
        except OSError:
            # Already-closed socket. Other exceptions are bugs.
            pass
    close = getattr(sock, "close", None)
    if callable(close):
        try:
            close()
        except OSError:
            # Already-closed socket. Other exceptions are bugs.
            pass


def stop_sse_keepalives(tcp_server: Any) -> None:
    """End SSE loops still running on *tcp_server* after the accept loop exits.

    HttpServer.stop() only makes serve_forever() return. Each GET /mcp and
    GET /sse keepalive stays on its request thread until the client drops or
    the 15s select timeout, and a restart adds more. Set this listener's flag
    and shut down its registered sockets so those loops exit. The next
    HttpServer has its own flag.
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


def format_mcp_start_failure(host: str, port: int | str, exc: BaseException) -> str:
    """Short user-facing body for MCP/HTTP start failures (no full traceback).

    Always includes host:port and the exception line. Port conflicts get the same
    guidance as the bind log so the dialog is actionable without opening the debug log.
    """
    return format_bind_failure(host, port, exc, service_name="MCP")


class _ThreadedHTTPServer(DualStackThreadPoolHTTPServer):
    """Dual-stack thread-pooled HTTP server for MCP and local routes.

    ``route_registry`` and ``bind_host`` are MCP-only. The shared base does
    not carry them. ``cors`` reads ``bind_host`` with getattr.
    """

    route_registry: HttpRouteRegistry | None = None
    bind_host: str | None = None



class GenericRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler that dispatches to registered routes."""

    # Applied in StreamRequestHandler.setup to the accepted socket.
    # ClassVar matches StreamRequestHandler.timeout so this stays a class
    # attribute (a bare annotation is treated as an instance variable).
    timeout: ClassVar[float | None] = MCP_HTTP_SOCKET_TIMEOUT_SEC
    _response_started: bool = False

    def handle_one_request(self) -> None:
        """A client that goes away while the response is written is a closed request.

        What was wrong: the 404 ``_send_json`` in ``_dispatch`` is outside the
        ``try`` that swallows ``OSError`` on the 500 write. ``do_GET`` returns
        into ``BaseHTTPRequestHandler.handle_one_request``, which only catches
        ``TimeoutError`` and then flushes ``wfile`` again. ``BrokenPipeError``
        and ``ConnectionResetError`` from the 404 write, from the 403 writes in
        ``reject_forbidden_host`` / ``reject_forbidden_origin``, or from that
        second flush reach ``handle_error`` and print a traceback.
        How: this override is the one frame above ``do_*`` and that flush.
        Why: those disconnects are ``ConnectionError``. Any other exception
        still propagates.
        """
        try:
            super().handle_one_request()
        except ConnectionError:
            self.close_connection: bool = True

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        if reject_forbidden_host(self):
            return
        if reject_forbidden_origin(self):
            return
        path = get_url_path(self.path)
        log_cors_preflight(self, path)
        write_http_empty(self, 204, extra_headers=lambda h: send_cors_headers(h, preflight=True))

    def _dispatch(self, method: str) -> None:
        if reject_forbidden_host(self):
            return
        if reject_forbidden_origin(self):
            return
        path = get_url_path(self.path)
        log_http_request(self, method, path)
        setattr(self.headers, "_client_address", self.client_address)
        server = cast("_ThreadedHTTPServer", self.server)
        route_registry = server.route_registry
        route = route_registry.match(method, path) if route_registry else None

        if route is None:
            log_no_route(self, method, path)
            from plugin.framework.errors import WriterAgentException, format_error_payload

            err = WriterAgentException("Not found", code="NOT_FOUND", details={"path": path})
            self._send_json(404, format_error_payload(err))
            return

        try:
            if route.raw:
                route.handler(self)
            else:
                body = self._read_body()
                if body is None:
                    return  # _read_body already sent error response
                query = get_url_query_dict(self.path)
                result = route.handler(body, self.headers, query)
                status, data = cast("tuple[int, Any]", result)
                self._send_json(status, data)
        except Exception as e:
            log.exception("%s %s failed", method, path)
            if not self._response_started:
                try:
                    from plugin.framework.errors import format_error_payload
                    self._send_json(500, format_error_payload(e))
                except OSError:
                    pass

    def _read_body(self) -> Any:
        data, rejected = read_json_body(self)
        if rejected is not None:
            from plugin.framework.errors import format_error_payload

            status, err = rejected
            self._send_json(status, format_error_payload(err))
            return None
        return data

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

        # Certs are loaded before bind. A missing file or use_ssl without
        # certs used to bind the port and then close it. The caller logs the
        # failure once (_start_server); logging here too wrote two tracebacks.
        ssl_ctx = None
        if self.use_ssl:
            # TLS server mode requires explicit certificates.
            # Local generation of certificates has been removed from ssl_helpers.
            if not (self.ssl_cert and self.ssl_key):
                raise ValueError("use_ssl is True but no certificates provided.")
            cert_path, key_path = self.ssl_cert, self.ssl_key
            log.info("TLS using custom certs: %s", cert_path)
            import ssl

            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)

        # Single bind — no retry/sleep. A busy port used to block bootstrap and the Start MCP
        # menu for ~4s (5×1s). Stdio clients that start before LO are handled by mcp_bridge.py;
        # callers stash OSError and show _PORT_IN_USE_GUIDANCE in the UI.
        self._server = _ThreadedHTTPServer(
            (self.host, self.port),
            GenericRequestHandler,
            max_threads=32,
            thread_name_prefix="mcp-worker",
            socket_timeout=MCP_HTTP_SOCKET_TIMEOUT_SEC,
        )
        self._server.route_registry = self.route_registry
        # The configured bind host (e.g. a LAN name) is an allowed Host header
        # alongside loopback and the tunnel host (DNS-rebinding check).
        self._server.bind_host = self.host
        if ssl_ctx is not None:
            self._server.ssl_ctx = ssl_ctx
        # Before the accept thread exists, so request handlers share this state.
        _sse_state(self._server)

        self._running = True
        self._thread = run_in_background(self._run, daemon=True, name="http-server", dedicated=True)

        scheme = "https" if self.use_ssl else "http"
        url = "%s://%s:%s" % (scheme, self.host, self.port)
        log.info("HTTP server ready — %s (%d routes)", url, self.route_registry.route_count)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        threads_to_join = []
        try:
            if self._server:
                # The SSE state registry tracks active SSE keepalive threads so we can
                # wait for them to exit after their sockets are closed. POST handler threads
                # are daemon threads and are not joined (joining POST threads on the main thread
                # while they await the main-thread queue causes deadlocks/freezes).
                _, _, threads, lock = _sse_state(self._server)
                with lock:
                    threads_to_join = list(threads)
                self._server.shutdown()
                self._server.server_close()
                log.info("HTTP server stopped")
        finally:
            # shutdown() does not join request threads. SSE keepalives are
            # still blocked in select until their sockets are closed.
            stop_sse_keepalives(self._server)

            # Join in-flight SSE keepalive threads within a single 2s deadline.
            deadline = time.monotonic() + 2.0
            still_alive: list[str] = []
            # Join in-flight SSE keepalive threads. We give them a bit of time, shutdown() only shuts down new accept calls.
            for t in threads_to_join:
                if not t.is_alive() or t is threading.current_thread():
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    still_alive.append(getattr(t, "name", "unknown"))
                    continue
                t.join(timeout=remaining)
                if t.is_alive():
                    still_alive.append(getattr(t, "name", "unknown"))
            if still_alive:
                log.warning("HTTP server request thread(s) still alive after stop: %s", ", ".join(still_alive))

    @background
    def _run(self) -> None:
        server = self._server
        try:
            if server:
                server.serve_forever()
        except Exception:
            if self._running:
                log.exception("HTTP server error")
        finally:
            # stop() sets _running False before shutdown() and server_close().
            # When serve_forever returns on its own, this finally clears
            # _running and stops SSE keepalives but would leave the listen
            # socket open. stop() then returns immediately, so server_close()
            # never runs and the port stays bound. Call server_close() only on
            # that unexpected exit. The normal stop() path has already closed
            # the listener, so this branch does not run and does not close it
            # a second time.
            unexpected = self._running and self._server is server
            self._running = False
            if unexpected:
                try:
                    if server is not None:
                        server.server_close()
                finally:
                    if server is not None:
                        stop_sse_keepalives(server)

    def is_running(self) -> bool:
        return self._running

    def get_status(self) -> dict[str, Any]:
        scheme = "https" if self.use_ssl else "http"
        base_url = "%s://%s:%s" % (scheme, self.host, self.port)
        return {"running": self._running, "host": self.host, "port": self.port, "ssl": self.use_ssl, "url": base_url, "mcp_url": mcp_endpoint_url(self.host, self.port, self.use_ssl), "routes": self.route_registry.route_count, "thread_alive": (self._thread.is_alive() if self._thread else False)}
