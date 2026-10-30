# WriterAgent - Python Compute Service HTTP Server
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Accept loop, listener budget, and the deadline clock.

Route handlers import the clock and the sticky cap. ``server.py`` builds
one ``WSGIDualStackServer`` and still owns process startup.
"""

from __future__ import annotations

import logging
import queue
import select
import selectors
import socket
import ssl
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from wsgiref.simple_server import ServerHandler, WSGIRequestHandler, WSGIServer

from plugin.framework.http_server import DualStackThreadPoolHTTPServer

if TYPE_CHECKING:
    from compute_service.config import ComputeSettings

log = logging.getLogger("compute_service")

# Bound the post-accept drain. Matches README 30s termination grace.
_HTTP_DRAIN_SEC = 30.0
# Header read deadline. Shorter fixed budget for request line and headers.
_HEADER_READ_TIMEOUT_SEC = 10.0
# Body read. Reset to _REQUEST_WRITE_TIMEOUT_SEC once the body is
# buffered so long calculations do not trip it during execution. Parked idle
# timeout also stays on this 30s constant so an idle keep-alive is not closed
# inside the header budget.
_REQUEST_READ_TIMEOUT_SEC = 30.0
# Socket write deadline for sending the response once computation completes.
_REQUEST_WRITE_TIMEOUT_SEC = 30.0


def listener_thread_count(max_threads: int | None) -> int:
    """Listener threads when the server is built with an explicit ``max_threads``.

    ``None`` is the class default of 16. A configured count keeps four spare
    threads and never goes below 8. Tests and the benchmark pass ``max_threads``
    this way. A running service uses ``service_listener_threads`` instead, so
    the sticky cap cannot drift from the accept pool.
    """
    if max_threads is None:
        return 16
    return max(8, max_threads + 4)


def _listener_budget(settings: ComputeSettings) -> tuple[int, int]:
    """Return ``(listeners, sticky)`` from one formula.

    ``listeners`` is ``max(listener_thread_count(settings.threads), needed)``.
    ``needed`` is formula workers, plus ``max(1, ocr_workers)`` vision permits,
    plus one sticky slot per formula worker, plus two threads that are not
    given a permit. ``sticky`` is what remains after the isolated workers, the
    vision permits, and those two. Admitted work cannot fill every listener,
    so ``GET /health`` is not stuck behind a permit. A header or body read
    still occupies its listener before that permit. When the historical floor
    wins, sticky is larger than the worker count (default: 8 listeners and 3
    sticky slots). ``settings.threads`` plus four left one spare once vision
    and the two unpermitted threads were counted, so a second sticky workbook
    got 503 while other workers were idle.
    """
    vision_permits = max(1, settings.ocr_workers)
    sticky_floor = max(1, settings.workers)
    needed = settings.workers + vision_permits + sticky_floor + 2
    listeners = max(listener_thread_count(settings.threads), needed)
    spare = listeners - 2 - settings.workers - vision_permits
    return listeners, max(1, spare)


def service_listener_threads(settings: ComputeSettings) -> int:
    """Accept-pool size for one running compute service."""
    return _listener_budget(settings)[0]


def sticky_listener_slots(settings: ComputeSettings) -> int:
    """How many sticky execute / session-reset requests may hold a listener."""
    return _listener_budget(settings)[1]


def _accept_clock(accept_time: Any) -> float:
    """Numeric accept timestamp, or now when the value is missing or not a number.

    Bool is an int subclass and is not a clock. A non-numeric value must
    not raise, or the request becomes HTTP 500.
    """
    if isinstance(accept_time, (int, float)) and not isinstance(accept_time, bool):
        return float(accept_time)
    return time.monotonic()


def _request_deadline(accept_time: Any, timeout_sec: float) -> float:  # pyright: ignore[reportUnusedFunction]
    """One clock from request start through the worker lease and the child.

    Stamping occurs on the accept thread when a connection becomes readable,
    including the first byte of a new connection and kept-alive resubmits.
    Without an accept timestamp (direct WSGI tests) the clock starts now.
    Queue wait is subtracted from *timeout_sec*; it is not a second 30s timer.
    """
    return _accept_clock(accept_time) + float(timeout_sec)


# =============================================================================
# HTTP Server Plumbing
# =============================================================================


@dataclass
class ConnState:
    """Per-connection state for persistent keep-alive handling."""

    sock: socket.socket
    client_address: Any
    rfile: Any
    request_count: int = 0
    finished_at: float = 0.0
    disposition: str = "close"
    close_reason: str = "close"
    close_connection: bool = False


class ComputeServerHandler(ServerHandler):
    """WSGI ServerHandler with HTTP/1.1 and keep-alive header enforcement."""

    http_version: str = "1.1"

    def write(self, data: bytes) -> None:
        assert type(data) is bytes, "write() argument must be a bytes instance"
        h = cast("Any", self)
        if not h.status:
            raise AssertionError("write() before start_response()")
        if not h.headers_sent:
            h.bytes_sent = len(data)
            h.send_headers()
        else:
            h.bytes_sent += len(data)
        if h.environ.get("REQUEST_METHOD") == "HEAD":
            return
        h._write(data)
        h._flush()

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
            or bool(h.environ.get("compute.close_connection"))
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
        h.environ["compute.close_connection"] = True
        super().handle_error()


class DeadlineRequestHandler(WSGIRequestHandler):
    """WSGI request handler with total header read deadline and compute context logging."""

    protocol_version: str = "HTTP/1.1"
    connection: Any
    rfile: Any
    wfile: Any
    close_connection: bool
    raw_requestline: bytes = b""
    requestline: str = ""
    request_version: str = ""
    command: str = ""
    _header_rfile_raw: Any = None
    _header_orig_readinto: Any = None

    def setup(self) -> None:
        server = cast("Any", self.server)
        self.connection = self.request
        state = server._conn_states.get(self.connection)
        if state is None:
            rfile = self.connection.makefile("rb", -1)
            state = ConnState(sock=self.connection, client_address=self.client_address, rfile=rfile)
            server._conn_states[self.connection] = state
        else:
            state.close_connection = False
            state.disposition = "close"
        self.rfile = state.rfile
        self.wfile = self.connection.makefile("wb", self.wbufsize)
        try:
            self.connection.settimeout(_REQUEST_READ_TIMEOUT_SEC)
        except Exception:
            pass

    def finish(self) -> None:
        if not self.wfile.closed:
            try:
                self.wfile.flush()
            except socket.error:
                pass
        self.wfile.close()

    def _mark_close(self, state: ConnState | None, reason: str) -> None:
        self.close_connection = True
        if state is not None:
            state.close_reason = reason
            state.close_connection = True

    def handle(self) -> None:
        """Read the request line and headers under one deadline, then run the app.

        ``WSGIRequestHandler.handle`` never calls ``handle_one_request``, so a
        deadline installed there did not run. The patch stays off during the
        app: a long calculation must not inherit the header clock.
        """
        self._install_header_deadline()
        header_timed_out = False
        server = cast("Any", self.server)
        state: ConnState | None = getattr(server, "_conn_states", {}).get(self.connection)
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if not self.raw_requestline:
                self._mark_close(state, "eof")
                return
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self._mark_close(state, "error")
                self.send_error(414)
                return
            if not self.parse_request():
                self._mark_close(state, "error")
                return
        except TimeoutError:
            # Sent after finally. The patched readinto is still installed
            # here, and its socket timeout is already expired.
            header_timed_out = True
        finally:
            self._clear_header_deadline()
        if header_timed_out:
            self._mark_close(state, "error")
            self._send_header_timeout()
            return

        if state is not None:
            state.request_count += 1

        # A return above leaves this function after the finally. Reaching
        # here means the request line and headers parsed.
        environ = self.get_environ()
        handler = ComputeServerHandler(self.rfile, cast("Any", self.wfile), self.get_stderr(), environ, multithread=False)
        cast("Any", handler).request_handler = self
        try:
            handler.run(cast("Any", self.server).get_app())
        except Exception:
            self._mark_close(state, "error")
            raise

        if environ.get("compute.close_connection"):
            self._mark_close(state, "undrained")
        elif self.close_connection:
            if state is not None:
                if state.close_reason not in ("undrained", "error", "eof"):
                    state.close_reason = "close"
                state.close_connection = True

    def _send_header_timeout(self) -> None:
        """Answer a header-read deadline with the same 408 the body path sends.

        The patched ``readinto`` raises ``socket.timeout``. Answering here
        sends a status; ``handle_error`` would print a traceback and send
        none. The write runs after the deadline patch is removed, so it is
        not on an already-expired socket timeout.
        """
        body = b'{"status": "error", "error": "Request read timeout"}'
        try:
            self.send_response(408, "Request Timeout")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except Exception:
            log.debug("header timeout response failed", exc_info=True)

    def _install_header_deadline(self) -> None:
        accept_time = self._connection_accept_time()
        header_deadline = _accept_clock(accept_time) + _HEADER_READ_TIMEOUT_SEC
        rfile_raw: Any = getattr(self.rfile, "raw", None)
        orig_readinto = getattr(rfile_raw, "readinto", None) if rfile_raw is not None else None
        self._header_rfile_raw = rfile_raw
        self._header_orig_readinto = orig_readinto

        def _deadline_readinto(b: Any) -> int:
            now = time.monotonic()
            remaining = header_deadline - now
            if remaining <= 0:
                raise socket.timeout("Header read deadline expired")
            self.connection.settimeout(max(0.01, remaining))
            # Bugfix: what was wrong: returning 0 on non-int or non-callable made the HTTP
            # parser treat a bad read as EOF and close the connection without a 408 response.
            # How it happened: silent fallback `return 0` masked invalid states.
            # Why this change fixes it: assert orig_readinto is callable and returned int,
            # which is guaranteed for a timeout socket's readinto.
            assert callable(orig_readinto)
            res = orig_readinto(b)
            assert isinstance(res, int)
            return res

        if rfile_raw is not None and callable(orig_readinto):
            rfile_raw.readinto = _deadline_readinto

    def _clear_header_deadline(self) -> None:
        rfile_raw = getattr(self, "_header_rfile_raw", None)
        orig_readinto = getattr(self, "_header_orig_readinto", None)
        if rfile_raw is not None and orig_readinto is not None:
            rfile_raw.readinto = orig_readinto
        try:
            self.connection.settimeout(_REQUEST_READ_TIMEOUT_SEC)
        except Exception:
            pass

    def address_string(self) -> str:
        return str(self.client_address[0])

    def log_message(self, format: str, *args: Any) -> None:
        log.debug(format, *args)

    def get_environ(self) -> dict[str, Any]:
        environ = super().get_environ()
        environ["compute.connection"] = self.connection
        environ["compute.accept_time"] = self._connection_accept_time()
        return environ

    def _connection_accept_time(self) -> float | None:
        """Accept stamp for this connection.

        The map is keyed by the socket object. An ``id()`` key is reused
        after the socket is closed, so a later accept would inherit this stamp.
        """
        times = getattr(self.server, "_accept_times", None)
        if times is None:
            return None
        found = times.get(self.connection)
        if isinstance(found, (int, float)) and not isinstance(found, bool):
            return float(found)
        return None


def _socket_readable(sock: socket.socket, selector: selectors.BaseSelector | None = None) -> bool:
    # select() rejects fds >= 1024. Use the accept selector when this fd is
    # registered on it; otherwise poll(), which has no fd cap.
    if isinstance(selector, selectors.BaseSelector):
        try:
            selector.get_key(sock)
        except KeyError:
            pass
        else:
            try:
                ready = selector.select(0)
                return any(key.fileobj is sock for key, _event in ready)
            except (OSError, ValueError):
                pass
    try:
        if hasattr(select, "poll"):
            poller = select.poll()
            poller.register(sock, select.POLLIN)
            return bool(poller.poll(0))
        # Windows has no poll(); its select() has no fd-number cap.
        r, _, _ = select.select([sock], [], [], 0)
        return bool(r)
    except (OSError, ValueError):
        return False


class WSGIDualStackServer(DualStackThreadPoolHTTPServer, WSGIServer):
    """Dual-stack thread-pooled WSGI server with idle connection parking."""

    park_idle_connections: bool = True

    server_name: str
    server_port: int
    srv: Any
    _conn_states: dict[socket.socket, ConnState]
    _parked: dict[socket.socket, ConnState]
    _last_park_error_warn: float
    _parking_closed: bool

    def __init__(self, host: str, port: int, max_threads: int | None = None, *, listener_threads: int | None = None) -> None:
        # listener_threads is the final pool size. Passing that number through
        # max_threads would add four again.
        effective_threads = listener_threads if listener_threads is not None else listener_thread_count(max_threads)
        DualStackThreadPoolHTTPServer.__init__(self, (host, port), DeadlineRequestHandler, bind_and_activate=True, max_threads=effective_threads)
        raw_host = str(self.server_address[0])
        self.server_name = raw_host if raw_host and raw_host not in ("", "0.0.0.0", "::") else "localhost"
        self.server_port = self.server_address[1]
        self._conn_states = {}
        self._parked = {}
        self._last_park_error_warn = 0.0
        self.setup_environ()
        self.srv = self

    def _park_accepted(self, conn: socket.socket, client_address: Any, selector: selectors.BaseSelector) -> bool:
        if self.ssl_ctx is not None or isinstance(conn, ssl.SSLSocket):
            return False
        assert not self._parking_closed
        assert conn not in self._conn_states
        rfile = conn.makefile("rb", -1)
        state = ConnState(
            sock=conn,
            client_address=client_address,
            rfile=rfile,
            disposition="park",
            finished_at=time.monotonic(),
        )
        self._conn_states[conn] = state
        try:
            selector.register(conn, selectors.EVENT_READ, data=state)
        except (KeyError, OSError) as exc:
            now = time.monotonic()
            if self._warn_due("_last_park_error_warn", now):
                log.warning("Failed to park connection (%s): %s; closing", type(exc).__name__, exc)
            state.close_reason = "error"
            try:
                self.shutdown_request(conn)
            except Exception:
                pass
            return True
        self._parked[conn] = state
        return True

    def _park_poll_timeout(self, timeout: float) -> float:
        if not self._parked:
            return timeout
        now = time.monotonic()
        earliest = min(state.finished_at + _REQUEST_READ_TIMEOUT_SEC for state in self._parked.values())
        return min(timeout, max(0.0, earliest - now))

    def _park_on_readable(self, state: Any, fileobj: Any, selector: selectors.BaseSelector) -> None:
        # Accept thread only. Unregister before submit so the idle sweep
        # cannot close a socket a worker is about to own.
        assert isinstance(state, ConnState) and state.sock is fileobj
        sock = state.sock
        try:
            selector.unregister(sock)
        except (KeyError, ValueError, OSError):
            pass
        self._parked.pop(sock, None)
        self._accept_times[sock] = time.monotonic()
        self.process_request(sock, state.client_address, established=True)

    def _park_service_loop(self, selector: selectors.BaseSelector) -> None:
        queued: list[ConnState] = []
        with self._park_lock:
            while True:
                try:
                    queued.append(self._park_queue.get_nowait())
                except queue.Empty:
                    break

        for state in queued:
            sock = state.sock
            if state.disposition == "ready_now":
                self._accept_times[sock] = time.monotonic()
                self.process_request(sock, state.client_address, established=True)
            elif state.disposition == "park":
                try:
                    selector.register(sock, selectors.EVENT_READ, data=state)
                    self._parked[sock] = state
                except (KeyError, OSError) as exc:
                    now = time.monotonic()
                    if self._warn_due("_last_park_error_warn", now):
                        log.warning("Failed to park connection (%s): %s; closing", type(exc).__name__, exc)
                    state.close_reason = "error"
                    try:
                        self.shutdown_request(sock)
                    except Exception:
                        pass

        # Idle sweep: readable sockets were already processed in ready loop, but check select again so readable wins over expired
        now = time.monotonic()
        expired: list[ConnState] = []
        for sock, state in list(self._parked.items()):
            if now >= state.finished_at + _REQUEST_READ_TIMEOUT_SEC:
                if _socket_readable(sock, selector):
                    self._park_on_readable(state, sock, selector)
                    continue
                expired.append(state)

        for state in expired:
            sock = state.sock
            try:
                selector.unregister(sock)
            except (KeyError, ValueError, OSError):
                pass
            self._parked.pop(sock, None)
            state.close_reason = "idle"
            try:
                self.shutdown_request(sock)
            except Exception:
                pass

    def _park_close_one_on_select_overflow(self, selector: selectors.BaseSelector) -> None:
        if not self._parked:
            return
        sock, state = next(iter(self._parked.items()))
        try:
            selector.unregister(sock)
        except (KeyError, ValueError, OSError):
            pass
        self._parked.pop(sock, None)
        state.close_reason = "idle"
        now = time.monotonic()
        if self._warn_due("_last_park_error_warn", now):
            log.warning("Select descriptor overflow; closed oldest parked connection")
        try:
            self.shutdown_request(sock)
        except Exception:
            pass

    def _park_shutdown(self, selector: selectors.BaseSelector) -> None:
        with self._park_lock:
            self._parking_closed = True
            queued: list[ConnState] = []
            while True:
                try:
                    queued.append(self._park_queue.get_nowait())
                except queue.Empty:
                    break

        for state in queued:
            sock = state.sock
            state.close_reason = "shutdown"
            try:
                self.shutdown_request(sock)
            except Exception:
                pass

        parked_states = list(self._parked.values())
        self._parked.clear()
        for state in parked_states:
            sock = state.sock
            try:
                selector.unregister(sock)
            except (KeyError, ValueError, OSError):
                pass
            state.close_reason = "shutdown"
            try:
                self.shutdown_request(sock)
            except Exception:
                pass

    def _park_hand_off(self, request: Any, client_address: Any) -> bool:
        state = self._conn_states.get(request)
        if state is None:
            return False
        if self.ssl_ctx is not None or isinstance(request, ssl.SSLSocket):
            state.close_reason = "close"
            return False
        if state.close_connection or state.close_reason in ("undrained", "error", "eof"):
            return False

        try:
            request.setblocking(False)
            peeked = state.rfile.peek(1)
            if peeked:
                state.disposition = "ready_now"
            else:
                try:
                    raw_peek = request.recv(1, socket.MSG_PEEK)
                    if not raw_peek:
                        state.disposition = "close"
                        state.close_reason = "eof"
                    else:
                        state.disposition = "ready_now"
                except (BlockingIOError, InterruptedError):
                    state.disposition = "park"
                    state.finished_at = time.monotonic()
        except Exception:
            state.disposition = "close"
            state.close_reason = "error"

        if state.disposition == "close":
            return False

        with self._park_lock:
            if self._parking_closed or not self._serving:
                state.close_reason = "shutdown" if self._parking_closed else "close"
                return False
            self._park_queue.put(state)
        self._wake_accept()
        return True

    def _on_queue_full(self, request: Any) -> None:
        state = self._conn_states.get(request)
        if state is not None:
            state.close_reason = "queue_full"

    def shutdown_request(self, request: Any) -> None:
        # Workers call this too. _parked is accept-thread only; every accept
        # path pops it before closing, and a worker never owns a parked socket.
        state = self._conn_states.pop(request, None)
        if state is not None:
            if state.rfile is not None and not state.rfile.closed:
                try:
                    state.rfile.close()
                except Exception:
                    pass
            peer = state.client_address[0] if isinstance(state.client_address, tuple) and state.client_address else str(state.client_address)
            log.debug("Closed connection from %s after %d request(s) (reason=%s)", peer, state.request_count, state.close_reason)
        super().shutdown_request(request)
