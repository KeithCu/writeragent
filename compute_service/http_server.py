# WriterAgent - Python Compute Service HTTP Server
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Accept loop, listener budget, and the deadline clock.

Route handlers import the clock and the sticky cap. ``server.py`` builds
one ``WSGIDualStackServer`` and still owns process startup.
"""

from __future__ import annotations

import errno
import logging
import selectors
import socket
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import HTTPServer
from typing import TYPE_CHECKING, Any, cast
from wsgiref.simple_server import ServerHandler, WSGIRequestHandler, WSGIServer

if TYPE_CHECKING:
    from compute_service.config import ComputeSettings

log = logging.getLogger("compute_service")

# Imported by routes and server. A leading underscore would otherwise look unused here.
__all__ = [
    "DeadlineRequestHandler",
    "DualStackThreadPoolHTTPServer",
    "ListenerBudget",
    "WSGIDualStackServer",
    "_HTTP_DRAIN_SEC",
    "_REQUEST_READ_TIMEOUT_SEC",
    "_REQUEST_WRITE_TIMEOUT_SEC",
    "_accept_clock",
    "_request_deadline",
    "listener_budget",
    "listener_thread_count",
    "service_listener_threads",
    "sticky_listener_slots",
]

# Bound the post-accept drain. Matches README 30s termination grace.
_HTTP_DRAIN_SEC = 30.0
# Header and body read. Reset to _REQUEST_WRITE_TIMEOUT_SEC once the body is
# buffered so long calculations do not trip it during execution.
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
    return max(8, (max_threads or 2) + 4)


@dataclass(frozen=True)
class ListenerBudget:
    """Accept-pool size and the sticky spare taken from that same total.

    ``listeners`` is ``max(listener_thread_count(settings.threads), needed)``
    where ``needed`` is formula workers, plus ``max(1, ocr_workers)`` vision
    permits, plus one sticky slot per formula worker, plus two threads for
    ``GET /health``. ``sticky`` is what remains after the isolated workers,
    the vision permits, and those two health threads. When the historical
    floor wins, sticky is larger than the worker count (default: 8 listeners
    and 3 sticky slots).
    """

    listeners: int
    sticky: int
    vision_permits: int


def listener_budget(settings: ComputeSettings) -> ListenerBudget:
    """The one derivation of listener count and sticky spare.

    ``service_listener_threads`` and ``sticky_listener_slots`` both read this
    so the accept pool and the sticky cap cannot be edited apart.
    """
    vision_permits = max(1, settings.ocr_workers)
    sticky_floor = max(1, settings.workers)
    needed = settings.workers + vision_permits + sticky_floor + 2
    listeners = max(listener_thread_count(settings.threads), needed)
    spare = listeners - 2 - settings.workers - vision_permits
    return ListenerBudget(listeners=listeners, sticky=max(1, spare), vision_permits=vision_permits)


def service_listener_threads(settings: ComputeSettings) -> int:
    """Accept-pool size for one running compute service.

    ``settings.threads`` is formula workers plus OCR workers. That count plus
    four left one spare once vision and the two health threads were reserved,
    so a second sticky workbook got 503 while other workers were idle. One
    sticky slot per formula worker, the vision permit (present even when OCR
    is off), and two threads for ``GET /health``. Small pools stay on the
    historical floor from ``listener_thread_count``.
    """
    return listener_budget(settings).listeners


def sticky_listener_slots(settings: ComputeSettings) -> int:
    """How many sticky execute / session-reset requests may hold a listener.

    Isolated execute holds at most ``settings.workers`` threads and vision
    holds ``max(1, ocr_workers)``. Two listeners stay free for ``GET /health``.
    """
    return listener_budget(settings).sticky


def _accept_clock(accept_time: Any) -> float:
    """Numeric accept timestamp, or now when the value is missing or not a number.

    What was wrong: the body and header deadlines called ``float(accept_time)``
    whenever the key was set. A WSGI test double passing a string raised
    TypeError and the request became HTTP 500. Bool is an int subclass and
    is not a clock.
    Why this change: the same check ``_request_deadline`` already used.
    """
    if isinstance(accept_time, (int, float)) and not isinstance(accept_time, bool):
        return float(accept_time)
    return time.monotonic()


def _request_deadline(accept_time: Any, timeout_sec: float) -> float:
    """One clock from TCP accept through the worker lease and the child.

    Without an accept timestamp (direct WSGI tests) the clock starts now.
    Queue wait is subtracted from *timeout_sec*; it is not a second 30s timer.
    """
    return _accept_clock(accept_time) + float(timeout_sec)


# =============================================================================
# HTTP Server Plumbing
# =============================================================================

class DualStackThreadPoolHTTPServer(HTTPServer):
    """HTTPServer that listens on both IPv4 and IPv6 loopback (or a single host) using a ThreadPoolExecutor.

    The thread pool capacity is ``service_listener_threads`` when the process
    starts, or ``listener_thread_count`` for an explicit ``max_threads``.
    Isolated ``/v1/execute`` and ``/v1/vision`` each have a semaphore. Sticky
    execute and ``/v1/session/reset`` share a smaller one. All three gates run
    before the request body is read and wait until the request deadline. 503
    is that timeout. A few milliseconds of queueing is normal at 200-400 rps.
    At least two listener threads stay available for ``GET /health``.
    """

    request_queue_size: int = 128
    _dual_is_shut_down: threading.Event
    _dual_shutdown_request: bool
    _serving: bool
    executor: ThreadPoolExecutor
    address_family: int
    server_address: tuple[str | bytes | bytearray, int] | tuple[str | bytes | bytearray, int, int, int]

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: Any,
        bind_and_activate: bool = True,
        max_threads: int | None = None,
    ) -> None:
        self.sockets: list[socket.socket] = []
        self._dual_is_shut_down = threading.Event()
        self._dual_shutdown_request = False
        self._serving = False
        # Keyed by the connection object, not id(conn). CPython reuses ids
        # after the socket is freed; a weak key disappears with the object,
        # so a recycled id cannot read another request's accept time.
        self._accept_times: weakref.WeakKeyDictionary[socket.socket, float] = weakref.WeakKeyDictionary()
        # The executor queue is unbounded. Semaphores bound how many requests
        # run at once; extras wait until the request deadline. A connection
        # flood can still grow this queue and _accept_times. That stays
        # acceptable while the only client is loopback coolwsd.
        self.executor = ThreadPoolExecutor(max_workers=max_threads, thread_name_prefix="compute-worker")

        super().__init__(server_address, RequestHandlerClass, bind_and_activate=False)
        inherited = self.socket
        try:
            inherited.close()
        except OSError:
            pass

        host, port = server_address

        bind_addresses: list[tuple[socket.AddressFamily, str]] = []
        if host in ("", "127.0.0.1", "::1", "localhost"):
            bind_addresses = [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")]
        elif host in ("0.0.0.0", "::"):
            bind_addresses = [(socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")]
        else:
            try:
                infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
                seen_families = set()
                for family, _unused, _unused2, _unused3, sockaddr in infos:
                    if family not in seen_families:
                        seen_families.add(family)
                        bind_addresses.append((family, str(sockaddr[0])))
            except Exception:
                bind_addresses = [(socket.AF_INET, host)]

        # What was wrong: one family failing (port taken on 127.0.0.1, ::1
        # free) logged a warning and kept serving. Clients on the failed
        # family never connected, and the process still looked up.
        # Why this change: EAFNOSUPPORT / EADDRNOTAVAIL means that family is
        # not on this host. Any other error closes what did bind and raises.
        bind_errors: list[OSError] = []
        optional_family = {errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL}
        for family, ip in bind_addresses:
            sock: socket.socket | None = None
            try:
                sock = socket.socket(family, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    try:
                        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    except OSError:
                        pass
                sock.bind((ip, port))
                if port == 0:
                    port = sock.getsockname()[1]
                self.sockets.append(sock)
                sock = None
            except OSError as e:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                if e.errno in optional_family:
                    log.warning("Address family unavailable for %s:%s: %s", ip, port, e)
                    continue
                log.warning("Failed to bind to %s:%s: %s", ip, port, e)
                bind_errors.append(e)

        if bind_errors or not self.sockets:
            # The executor was already created. Raising without shutdown leaks
            # its threads; server_close drops them and any socket that bound.
            self.server_close()
            if bind_errors:
                raise bind_errors[0]
            raise OSError(f"Could not bind to any address for {host}:{port}")

        self.socket: socket.socket = self.sockets[0]
        self.address_family = self.socket.family
        actual_port = self.socket.getsockname()[1]
        self.server_address = (host, actual_port)

        if bind_and_activate:
            try:
                self.server_activate()
            except Exception:
                self.server_close()
                raise

    def close_sockets(self) -> None:
        """Close listening sockets so new incoming connections are refused immediately."""
        for sock in self.sockets:
            try:
                sock.close()
            except Exception:
                pass
        self.sockets.clear()

    def server_activate(self) -> None:
        for sock in self.sockets:
            sock.listen(self.request_queue_size)

    def server_close(self) -> None:
        self.close_sockets()
        # Accept times are popped per connection in shutdown_request. A handler
        # abandoned after drain_executor can leave a key. The signal path
        # drains before this, so clearing does not shorten a live deadline.
        self._accept_times.clear()
        self.executor.shutdown(wait=False, cancel_futures=False)

    def drain_executor(self, timeout: float) -> None:
        """Wait until accepted requests finish, then return."""
        done = threading.Event()

        def _wait() -> None:
            self.executor.shutdown(wait=True, cancel_futures=False)
            done.set()

        threading.Thread(target=_wait, name="http-drain", daemon=True).start()
        if not done.wait(timeout):
            log.warning("HTTP request drain exceeded %.0fs; abandoning in-flight handlers", timeout)

    def fileno(self) -> int:
        return self.socket.fileno()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self._serving = True
        self._dual_is_shut_down.clear()
        listen = set(self.sockets)
        try:
            with selectors.DefaultSelector() as selector:
                for sock in self.sockets:
                    selector.register(sock, selectors.EVENT_READ)

                while not self._dual_shutdown_request:
                    ready = selector.select(poll_interval)
                    if self._dual_shutdown_request:
                        break
                    for key, _unused in ready:
                        ready_sock = key.fileobj
                        if not isinstance(ready_sock, socket.socket):
                            continue
                        if ready_sock in listen:
                            try:
                                conn, client_address = ready_sock.accept()
                            except OSError as e:
                                log.warning("Accept error: %s; backing off", e)
                                time.sleep(0.05)
                                continue
                            if not self.verify_request(conn, client_address):
                                self.shutdown_request(conn)
                                continue
                            self._accept_times[conn] = time.monotonic()
                            self.process_request(conn, client_address)
                    self.service_actions()
        finally:
            self._serving = False
            self._dual_shutdown_request = False
            self._dual_is_shut_down.set()

    def shutdown(self) -> None:
        """Stop ``serve_forever`` when it is running.

        Waiting with the flag clear blocks forever if ``serve_forever`` was
        never started. The signal path only calls this while the accept loop
        is in ``_serving``.
        """
        self._dual_shutdown_request = True
        if self._serving:
            self._dual_is_shut_down.wait()

    def process_request(self, request: Any, client_address: Any) -> None:
        """Submit incoming request to the thread pool executor.

        A failed submit used to re-raise out of ``serve_forever`` and leave
        the accept-time entry in place. Close the socket here and keep accepting.
        """
        try:
            self.executor.submit(self.process_request_thread, request, client_address)
        except Exception:
            log.exception("Failed to submit accepted connection")
            try:
                self.shutdown_request(request)
            except Exception:
                log.exception("Failed to close accepted connection")

    def shutdown_request(self, request: Any) -> None:
        self._accept_times.pop(request, None)
        super().shutdown_request(request)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        """Process incoming request inside a pooled worker thread."""
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


class DeadlineRequestHandler(WSGIRequestHandler):
    """WSGI request handler with total header read deadline and compute context logging."""

    raw_requestline: bytes = b""
    requestline: str = ""
    request_version: str = ""
    command: str = ""
    _header_rfile_raw: Any = None
    _header_orig_readinto: Any = None

    def setup(self) -> None:
        super().setup()
        try:
            self.connection.settimeout(_REQUEST_READ_TIMEOUT_SEC)
        except Exception:
            pass

    def handle(self) -> None:
        """Read the request line and headers under one deadline, then run the app.

        ``WSGIRequestHandler.handle`` never calls ``handle_one_request``, so a
        deadline installed there did not run. The patch stays off during the
        app: a long calculation must not inherit the header clock.
        """
        self._install_header_deadline()
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(414)
                return
            if not self.parse_request():
                return
        finally:
            self._clear_header_deadline()
        # A return above leaves this function after the finally. Reaching
        # here means the request line and headers parsed.
        self._send_100_continue_if_expected()
        handler = ServerHandler(
            self.rfile,
            cast("Any", self.wfile),
            self.get_stderr(),
            self.get_environ(),
            multithread=False,
        )
        # request_handler is assigned by wsgiref at runtime; the stub omits it.
        # get_app lives on WSGIServer, which this handler is only mounted on.
        cast("Any", handler).request_handler = self
        handler.run(cast("Any", self.server).get_app())

    def _send_100_continue_if_expected(self) -> None:
        """Answer ``Expect: 100-continue`` before the app reads the body.

        wsgiref never writes the interim response. curl then waits about a
        second before sending a larger body.
        """
        expected = self.headers.get("Expect") if self.headers is not None else None
        if not isinstance(expected, str) or expected.lower() != "100-continue":
            return
        try:
            self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            self.wfile.flush()
        except Exception:
            log.debug("100-continue write failed", exc_info=True)

    def _install_header_deadline(self) -> None:
        accept_time = self._connection_accept_time()
        header_deadline = _accept_clock(accept_time) + _REQUEST_READ_TIMEOUT_SEC
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
            if callable(orig_readinto):
                res = orig_readinto(b)
                if isinstance(res, int):
                    return res
            return 0

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

        The map is keyed by the socket object. ``id(connection)`` was wrong
        once that id was reused for a later accept.
        """
        times = getattr(self.server, "_accept_times", None)
        if times is None:
            return None
        found = times.get(self.connection)
        if isinstance(found, (int, float)) and not isinstance(found, bool):
            return float(found)
        return None


class WSGIDualStackServer(DualStackThreadPoolHTTPServer, WSGIServer):
    """Dual-stack thread-pooled WSGI server."""

    server_name: str
    server_port: int
    srv: Any

    def __init__(self, host: str, port: int, max_threads: int | None = None, *, listener_threads: int | None = None) -> None:
        # listener_threads is the final pool size. Passing that number through
        # max_threads would add four again.
        effective_threads = listener_threads if listener_threads is not None else listener_thread_count(max_threads)
        DualStackThreadPoolHTTPServer.__init__(
            self,
            (host, port),
            DeadlineRequestHandler,
            bind_and_activate=True,
            max_threads=effective_threads,
        )
        raw_host = str(self.server_address[0])
        self.server_name = raw_host if raw_host and raw_host not in ("", "0.0.0.0", "::") else "localhost"
        self.server_port = self.server_address[1]
        self.setup_environ()
        self.srv = self

