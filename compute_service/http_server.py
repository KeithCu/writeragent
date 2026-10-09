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
import socket
import time
from dataclasses import dataclass
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
    permits, plus one sticky slot per formula worker, plus two threads that
    are not given a permit. ``sticky`` is what remains after the isolated
    workers, the vision permits, and those two. Admitted work cannot fill
    every listener, so ``GET /health`` is not stuck behind a permit. A header
    or body read still occupies its listener before that permit. When the
    historical floor wins, sticky is larger than the worker count (default:
    8 listeners and 3 sticky slots).
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
    four left one spare once vision and the two unpermitted threads were
    counted, so a second sticky workbook got 503 while other workers were
    idle. One sticky slot per formula worker, the vision permit (present even
    when OCR is off), and two threads with no permit. Those two are not held
    back during a header or body read. Small pools stay on the historical
    floor from ``listener_thread_count``.
    """
    return listener_budget(settings).listeners


def sticky_listener_slots(settings: ComputeSettings) -> int:
    """How many sticky execute / session-reset requests may hold a listener.

    Isolated execute holds at most ``settings.workers`` threads and vision
    holds ``max(1, ocr_workers)``. Two listeners are not given a permit, so
    ``GET /health`` is not stuck behind admitted work. A header or body read
    still occupies its listener before that permit.
    """
    return listener_budget(settings).sticky


def _accept_clock(accept_time: Any) -> float:
    """Numeric accept timestamp, or now when the value is missing or not a number.

    Bool is an int subclass and is not a clock. A non-numeric value must
    not raise, or the request becomes HTTP 500.
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

from plugin.framework.http_server import DualStackThreadPoolHTTPServer



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
        header_timed_out = False
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
        except TimeoutError:
            # Sent after finally. The patched readinto is still installed
            # here, and its socket timeout is already expired.
            header_timed_out = True
        finally:
            self._clear_header_deadline()
        if header_timed_out:
            self._send_header_timeout()
            return
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

