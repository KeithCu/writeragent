# WriterAgent - Python Compute Service HTTP Server
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Accept loop, listener budget, and the deadline clock.

Route handlers import the clock and the admission permit count. ``server.py`` builds
one ``WSGIDualStackServer`` and still owns process startup.
"""

from __future__ import annotations

import logging
import queue
import selectors
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from wsgiref.simple_server import ServerHandler, WSGIRequestHandler, WSGIServer

from plugin.framework.http_server import DualStackThreadPoolHTTPServer

if TYPE_CHECKING:
    from compute_service.config import ComputeSettings

log = logging.getLogger("compute_service")

# Bound the post-accept drain. Matches README 30s termination grace.
HTTP_DRAIN_SEC = 30.0
# Per-read socket timeout while the request line and headers arrive.
_HEADER_READ_TIMEOUT_SEC = 10.0
# Body read. Reset to REQUEST_WRITE_TIMEOUT_SEC once the body is
# buffered so long calculations do not trip it during execution. Also the
# idle timeout of a parked keep-alive socket.
REQUEST_READ_TIMEOUT_SEC = 30.0
# Socket write deadline for sending the response once computation completes.
REQUEST_WRITE_TIMEOUT_SEC = 30.0

# Single WSGI environ slot for per-request socket state (see RequestConn).
WSGI_REQUEST_CONN = "compute_service.request_conn"


@dataclass
class RequestConn:
    """Per-request connection state shared by the WSGI handler and server.py.

    Stored by reference on ``environ``: wsgiref gives the app a shallow copy of
    ``environ``, so a scalar flag set only in the app dict would not reach
    ``DeadlineRequestHandler.handle()``; this object is shared across both.
    """

    connection: socket.socket | None = None
    accept_time: float | None = None
    close_connection: bool = False
    raw_body: bytes | None = None


def request_conn(environ: dict[str, Any]) -> RequestConn:
    """Return the request connection object, creating one for direct WSGI callers."""
    rc = environ.get(WSGI_REQUEST_CONN)
    if not isinstance(rc, RequestConn):
        rc = RequestConn()
        environ[WSGI_REQUEST_CONN] = rc
    return rc


def listener_thread_count(max_threads: int | None) -> int:
    """Listener threads for ``max_threads``: ``None`` is the class default of 16, otherwise ``max(8, n + 4)``."""
    if max_threads is None:
        return 16
    return max(8, max_threads + 4)


def admission_permits(settings: ComputeSettings) -> int:
    """Permits shared by execute, session reset and vision; two listeners stay free for ``GET /health``."""
    return listener_thread_count(settings.threads) - 2


# =============================================================================
# HTTP Server Plumbing
# =============================================================================


# Idle keep-alive sockets the accept thread holds at once. Past this the
# oldest closes; it also keeps select()-based selectors under their fd limit.
_MAX_PARKED = 500


@dataclass(eq=False)
class ConnState:
    """A keep-alive connection: the accept thread owns it while idle, a worker while it serves a request."""

    sock: socket.socket
    client_address: Any
    rfile: Any  # kept across requests so pipelined bytes are not lost
    last_active: float


def _next_action(state: ConnState) -> str:
    """After a response: ``"again"`` when the next request is already readable, ``"close"`` at EOF or error, ``"park"`` when idle."""
    try:
        state.sock.setblocking(False)
        if state.rfile.peek(1) or state.sock.recv(1, socket.MSG_PEEK):
            return "again"
        return "close"
    except (BlockingIOError, InterruptedError):
        return "park"
    except OSError:
        return "close"


def _drain_queue(q: queue.SimpleQueue[ConnState]) -> list[ConnState]:
    items: list[ConnState] = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            return items


class ComputeServerHandler(ServerHandler):
    """WSGI ServerHandler with HTTP/1.1 and keep-alive header enforcement."""

    http_version: str = "1.1"

    def cleanup_headers(self) -> None:
        super().cleanup_headers()
        # headers, environ, bytes_sent and request_handler are set by wsgiref
        # at runtime; the stub omits some of them.
        h = cast("Any", self)
        assert "Content-Length" in h.headers, "Content-Length header required"
        req_handler = getattr(h, "request_handler", None)
        # HTTP/1.0 keep-alive is opt-in on the response. This server does not
        # advertise it, so the socket closes with the response.
        close_conn = (
            h.environ.get("SERVER_PROTOCOL") == "HTTP/1.0"
            or request_conn(h.environ).close_connection
            or bool(getattr(req_handler, "close_connection", False))
        )
        # write() sets bytes_sent to the first block before send_headers. A
        # length that does not match that block cannot keep the socket.
        try:
            if int(h.headers["Content-Length"]) != h.bytes_sent:
                close_conn = True
        except (ValueError, TypeError):
            close_conn = True
        if close_conn:
            h.headers["Connection"] = "close"
            if req_handler is not None:
                req_handler.close_connection = True

    def handle_error(self) -> None:
        h = cast("Any", self)
        req_handler = getattr(h, "request_handler", None)
        if req_handler is not None:
            req_handler.close_connection = True
        request_conn(h.environ).close_connection = True
        super().handle_error()


class DeadlineRequestHandler(WSGIRequestHandler):
    """One request: headers under a per-read socket timeout, then the app on the body clock in ``server.py``."""

    protocol_version: str = "HTTP/1.1"
    connection: Any
    rfile: Any
    wfile: Any
    close_connection: bool
    raw_requestline: bytes = b""
    requestline: str = ""
    request_version: str = ""
    command: str = ""
    _state: ConnState | None = None

    def setup(self) -> None:
        self.connection = self.request
        self._state = cast("Any", self.server)._conns.get(self.connection)
        self.rfile = self._state.rfile if self._state is not None else self.connection.makefile("rb", -1)
        self.wfile = self.connection.makefile("wb", self.wbufsize)
        self.connection.settimeout(_HEADER_READ_TIMEOUT_SEC)

    def finish(self) -> None:
        if not self.wfile.closed:
            try:
                self.wfile.flush()
            except OSError:
                pass
        self.wfile.close()
        if self._state is None:
            self.rfile.close()

    def handle(self) -> None:
        """Read the request line and headers, then run the app. Every early return closes the connection.

        One request per call, modeled on ``BaseHTTPRequestHandler.handle_one_request``
        (``http/server.py:406``) and ``handle`` (``:443``), and
        ``WSGIRequestHandler.handle`` (``wsgiref/simple_server.py:111``). The stdlib
        ``while not close_connection`` loop is not used; idle sockets are parked by the
        accept thread.
        """
        self.close_connection = True
        self.raw_requestline = self.rfile.readline(65537)
        if not self.raw_requestline:
            return
        if len(self.raw_requestline) > 65536:
            self.requestline = ""
            self.request_version = ""
            self.command = ""
            self.send_error(414)
            return
        if not self.parse_request():
            self.close_connection = True
            return
        if self.command == "HEAD":
            # No route serves HEAD. Answering here keeps the WSGI write path free of a bodiless case.
            self.send_response(405)
            self.send_header("Allow", "GET, POST")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        self.connection.settimeout(REQUEST_READ_TIMEOUT_SEC)
        environ = self.get_environ()
        handler = ComputeServerHandler(self.rfile, cast("Any", self.wfile), self.get_stderr(), environ, multithread=False)
        cast("Any", handler).request_handler = self
        handler.run(cast("Any", self.server).get_app())
        # RequestConn is shared with the app's shallow copy of environ (see RequestConn).
        if request_conn(environ).close_connection:
            self.close_connection = True

    def handle_expect_100(self) -> bool:
        """Answer an oversized ``Expect: 100-continue`` body with 413, not 100 Continue."""
        limit = getattr(self.server, "max_body_bytes", None)
        raw_len = self.headers.get("Content-Length", "")
        if limit is None or not raw_len.isdigit() or int(raw_len) <= limit:
            return super().handle_expect_100()
        body = b'{"status": "error", "error": "Request body too large"}'
        self.send_response(413, "Payload Too Large")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        return False

    def address_string(self) -> str:
        return str(self.client_address[0])

    def log_message(self, format: str, *args: Any) -> None:
        log.debug(format, *args)

    def get_environ(self) -> dict[str, Any]:
        environ = super().get_environ()
        # Keyed by the socket object: an ``id()`` key is reused after close, so a later accept would inherit the stamp.
        environ[WSGI_REQUEST_CONN] = RequestConn(
            connection=self.connection,
            accept_time=cast("Any", self.server)._accept_times.get(self.connection),
        )
        return environ


class WSGIDualStackServer(DualStackThreadPoolHTTPServer, WSGIServer):
    """Dual-stack thread-pooled WSGI server; the accept thread holds every idle keep-alive socket."""

    park_idle_connections: bool = True
    max_body_bytes: int | None = None

    server_name: str
    server_port: int
    srv: Any
    _conns: dict[socket.socket, ConnState]
    _park_lock: threading.Lock
    _park_queue: queue.SimpleQueue[ConnState]
    _parking_closed: bool
    _last_park_error_warn: float = 0.0

    def __init__(self, host: str, port: int, max_threads: int | None = None, *, listener_threads: int | None = None) -> None:
        # listener_threads is the final pool size. Passing that number through
        # max_threads would add four again.
        effective_threads = listener_threads if listener_threads is not None else listener_thread_count(max_threads)
        DualStackThreadPoolHTTPServer.__init__(self, (host, port), DeadlineRequestHandler, bind_and_activate=True, max_threads=effective_threads)
        raw_host = str(self.server_address[0])
        self.server_name = raw_host if raw_host and raw_host not in ("", "0.0.0.0", "::") else "localhost"
        self.server_port = self.server_address[1]
        self._conns = {}
        self._park_lock = threading.Lock()
        self._park_queue = queue.SimpleQueue()
        self._parking_closed = False
        self.setup_environ()
        self.srv = self

    def _park_register(self, request: Any, client_address: Any) -> bool:
        """Queue an idle connection (new or after a response) for the accept thread; False leaves it with the caller."""
        if self.ssl_ctx is not None or isinstance(request, ssl.SSLSocket):
            return False
        with self._park_lock:
            if self._parking_closed or not self._serving:
                return False
            state = self._conns.get(request)
            if state is None:
                state = self._conns[request] = ConnState(request, client_address, request.makefile("rb", -1), 0.0)
            state.last_active = time.monotonic()
            self._park_queue.put(state)
        self._wake_accept()
        return True

    def _park_tick(self, selector: selectors.BaseSelector, ready: list[tuple[Any, int]]) -> float | None:
        """Accept thread: dispatch readable sockets, park queued ones, close idle and over-cap ones."""
        for key, _events in ready:
            if isinstance(key.data, ConnState):
                self._dispatch(key.data, selector)
        with self._park_lock:
            queued = _drain_queue(self._park_queue)
        for state in queued:
            try:
                selector.register(state.sock, selectors.EVENT_READ, data=state)
            except (KeyError, OSError, ValueError) as exc:
                if self._warn_due("_last_park_error_warn", time.monotonic()):
                    log.warning("Failed to park connection (%s); closing", type(exc).__name__)
                self.shutdown_request(state.sock)
        parked = [key.data for key in list(selector.get_map().values()) if isinstance(key.data, ConnState)]
        expired = time.monotonic() - REQUEST_READ_TIMEOUT_SEC
        over = len(parked) - _MAX_PARKED
        kept: list[ConnState] = []
        for i, state in enumerate(parked):
            if i < over or state.last_active <= expired:
                self._close_parked(state, selector)
            else:
                kept.append(state)
        if not kept:
            return None
        return min(state.last_active for state in kept) - expired

    def _park_shutdown(self, selector: selectors.BaseSelector) -> None:
        with self._park_lock:
            self._parking_closed = True
            queued = _drain_queue(self._park_queue)
        for state in queued:
            self.shutdown_request(state.sock)
        for key in list(selector.get_map().values()):
            if isinstance(key.data, ConnState):
                self._close_parked(key.data, selector)

    def _dispatch(self, state: ConnState, selector: selectors.BaseSelector) -> None:
        # Unregister before submit: a worker never shares a socket with the selector.
        selector.unregister(state.sock)
        self._accept_times[state.sock] = time.monotonic()
        self.process_request(state.sock, state.client_address, established=True)

    def _close_parked(self, state: ConnState, selector: selectors.BaseSelector) -> None:
        selector.unregister(state.sock)
        self.shutdown_request(state.sock)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        """Serve requests while the next one is already readable, then park or close the connection."""
        state = self._conns.get(request)
        if state is None:
            super().process_request_thread(request, client_address)
            return
        action = "close"
        try:
            while True:
                handler = cast("Any", self.RequestHandlerClass)(request, client_address, self)
                action = "close" if handler.close_connection else _next_action(state)
                if action != "again":
                    break
                self._accept_times[request] = time.monotonic()
        except Exception:
            action = "close"
            self.handle_error(request, client_address)
        if action != "park" or not self._park_register(request, client_address):
            self.shutdown_request(request)

    def shutdown_request(self, request: Any) -> None:
        state = self._conns.pop(request, None)
        if state is not None:
            state.rfile.close()
        super().shutdown_request(request)
