# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dual-stack (IPv4/IPv6) thread-pooled HTTP server.

Shared networking engine used by:
- WriterAgent MCP Server (plugin/mcp/server.py)
- Collabora Online Python Compute Service (compute_service/http_server.py)
"""

from __future__ import annotations

import errno
import logging
import os
import queue
import selectors
import socket
from socket import socket as Socket
import sys
import threading
import time
from concurrent.futures import Future
from http.server import HTTPServer
from typing import TYPE_CHECKING, Any, NamedTuple

from plugin.framework.worker_pool import run_in_background

if TYPE_CHECKING:
    import ssl
    from collections.abc import Callable

log = logging.getLogger("writeragent.framework.http_server")

__all__ = [
    "DualStackThreadPoolHTTPServer",
    "_PORT_IN_USE_ERRNOS",
    "_PORT_IN_USE_GUIDANCE",
    "format_bind_failure",
    "is_port_in_use_error",
]

# Shared with log.error on bind failure and the Toggle/Status/Settings msgbox so users
# see the same actionable text that used to live only in writeragent_debug.log (#379).
_PORT_IN_USE_GUIDANCE = (
    "The port is in use by another process. Close whatever is holding it, "
    "or set mcp.mcp_port in Settings (or writeragent.json) to a free port, then try again. "
    "A local preview/viewer server may default to the same port."
)

# errno.EADDRINUSE is 98 (Linux) / 48 (macOS); Windows uses winerror 10048 (WSAEADDRINUSE).
_PORT_IN_USE_ERRNOS = frozenset({98, 48, 10048})


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


class _RequestWork(NamedTuple):
    """One queued call. ``None`` on the queue is the stop sentinel."""

    future: Future[Any]
    fn: Callable[..., Any]
    args: tuple[Any, ...]


class _RequestThreads:
    """Bounded daemon pool for accepted connections.

    What was wrong: ``daemon_threads = True`` had no effect. Requests were
    submitted to ``ThreadPoolExecutor``. Since Python 3.9 those workers are
    non-daemon, and ``concurrent.futures.thread._python_exit`` joins every
    one of them even if ``daemon`` is set.
    How it happened: the accept loop replaced ``ThreadingMixIn`` with an
    executor so the pool stayed bounded. The atexit join came with that
    executor. A handler blocked in SSE ``select`` or on the UI queue then
    held LibreOffice open. ``HttpServer.stop`` does not join POST workers,
    because a worker waiting for the UI thread deadlocks if the UI joins it.
    Why this change: these threads are daemon and are not registered with
    that atexit hook. The worker count is still bounded. The queue is not.
    The shared ``wa-bg`` pool is not used: one SSE connection would hold a
    UI worker until the client left.

    ``submit`` / ``shutdown(wait, cancel_futures)`` match the old executor
    so ``drain_executor`` can wait for in-flight work. Threads start on the
    first submit, so a failed bind does not spawn any.
    """

    _max_workers: int
    _prefix: str
    _work_queue: queue.SimpleQueue[_RequestWork | None]
    _threads: list[threading.Thread]
    _lock: threading.Lock
    _shutdown: bool
    _sentinels: bool
    _idle: int

    def __init__(self, max_workers: int | None, thread_name_prefix: str) -> None:
        # Same default as ThreadPoolExecutor: an omitted count stays bounded.
        if max_workers is None:
            max_workers = min(32, (os.cpu_count() or 1) + 4)
        if max_workers <= 0:
            raise ValueError("max_workers must be greater than 0")
        self._max_workers = max_workers
        self._prefix = thread_name_prefix
        self._work_queue = queue.SimpleQueue()
        self._threads = []
        self._lock = threading.Lock()
        self._shutdown = False
        self._sentinels = False
        self._idle = 0

    def submit(self, fn: Callable[..., Any], /, *args: Any) -> Future[Any]:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("request pool is shut down")
            fut: Future[Any] = Future()
            self._work_queue.put(_RequestWork(fut, fn, args))
            # An idle worker is already in get(). Starting another would
            # grow the pool on every serial request up to the cap.
            if self._idle == 0 and len(self._threads) < self._max_workers:
                self._start_one_locked()
            return fut

    def _start_one_locked(self) -> None:
        idx = len(self._threads)
        # ThreadPoolExecutor's atexit hook joins its workers. A raw daemon
        # thread is the way out of that join. The ban exists so UI work goes
        # through run_in_background; this pool is the HTTP accept set.
        thread = threading.Thread(target=self._run, name=f"{self._prefix}_{idx}", daemon=True)  # nosemgrep: raw-uno-thread-ban
        thread.start()
        self._threads.append(thread)

    def _run(self) -> None:
        while True:
            with self._lock:
                self._idle += 1
            item = self._work_queue.get()
            with self._lock:
                self._idle -= 1
            if item is None:
                return
            if not item.future.set_running_or_notify_cancel():
                continue
            try:
                result = item.fn(*item.args)
            except BaseException as exc:
                item.future.set_exception(exc)
            else:
                item.future.set_result(result)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> list[Future[Any]]:
        """Stop the pool. Cancelled futures are returned so the caller can close their sockets.

        A second call is a no-op apart from ``wait``. ``drain_executor`` waits,
        then ``server_close`` shuts the pool down again.
        """
        cancelled: list[Future[Any]] = []
        with self._lock:
            self._shutdown = True
            # A second shutdown must not pull the stop sentinels back out.
            # Workers blocked in get() would then wait forever.
            if cancel_futures and not self._sentinels:
                while True:
                    try:
                        item = self._work_queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None and item.future.cancel():
                        cancelled.append(item.future)
            if not self._sentinels:
                for _unused in self._threads:
                    self._work_queue.put(None)
                self._sentinels = True
            threads = list(self._threads)
        if wait:
            for thread in threads:
                thread.join()
        return cancelled


def format_bind_failure(host: str, port: int | str, exc: BaseException, service_name: str = "HTTP") -> str:
    """Short user-facing body for server start failures (no full traceback).

    Always includes host:port and the exception line. Port conflicts get the same
    guidance as the bind log so the dialog is actionable without opening the debug log.
    """
    endpoint = f"{host}:{port}"
    exc_line = f"{type(exc).__name__}: {exc}"
    lines = [f"Could not bind {endpoint} — {exc_line}"]
    if is_port_in_use_error(exc):
        lines.append(_PORT_IN_USE_GUIDANCE)
    return "\n".join(lines)


class DualStackThreadPoolHTTPServer(HTTPServer):
    """HTTPServer that listens on both IPv4 and IPv6 loopback (or one host) using a bounded daemon pool.

    The socket accept loop uses selectors.DefaultSelector to monitor all bound sockets concurrently.
    Incoming connections are submitted to ``_RequestThreads``. The worker count is bounded.
    The accept queue is not: a burst waits for a thread instead of being rejected.
    Supports optional TLS socket wrapping.

    ``socket_timeout`` is None unless the caller passes one. None means the
    request handler sets the deadline. MCP passes its 30s constant. The compute
    service leaves it None and ``DeadlineRequestHandler`` sets the read deadline.
    """

    request_queue_size: int = 128
    allow_reuse_address: bool = os.name != "nt"
    # ThreadingMixIn is not used. The request pool is daemon for the same reason.
    daemon_threads: bool = True

    _dual_is_shut_down: threading.Event
    _dual_shutdown_request: bool
    _serving: bool
    executor: _RequestThreads
    _queued_lock: threading.Lock
    address_family: int
    server_address: tuple[str | bytes | bytearray, int] | tuple[str | bytes | bytearray, int, int, int]
    sockets: list[Socket]
    socket: Socket
    ssl_ctx: ssl.SSLContext | None = None
    socket_timeout: float | None = None

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: Any,
        bind_and_activate: bool = True,
        max_threads: int | None = None,
        *,
        thread_name_prefix: str = "http-worker",
        socket_timeout: float | None = None,
    ) -> None:
        self.sockets = []
        self._dual_is_shut_down = threading.Event()
        self._dual_shutdown_request = False
        self._serving = False
        # Keyed by the connection object, not id(conn). CPython reuses ids
        # after the socket is freed, so an id key could read a later accept's
        # timestamp. shutdown_request pops the entry on every path that
        # inserted one.
        self._accept_times: dict[socket.socket, float] = {}
        # Futures that have not finished. server_close closes the socket when
        # the future is cancelled, because the worker never reaches
        # shutdown_request. Keyed by the Future the pool returns.
        self._queued: dict[Future[Any], Any] = {}
        self._queued_lock = threading.Lock()
        self.socket_timeout = socket_timeout
        self.executor = _RequestThreads(max_threads, thread_name_prefix)

        super().__init__(server_address, RequestHandlerClass, bind_and_activate=False)
        inherited = self.socket
        try:
            inherited.close()
        except OSError:
            pass

        host, port = server_address

        bind_addresses: list[tuple[socket.AddressFamily, str]] = []
        if host in ("", "127.0.0.1", "::1", "localhost"):
            # "" is loopback here, not INADDR_ANY. Stdlib HTTPServer treats ""
            # as every interface. An empty host must not listen on the LAN.
            # compute_service/README.md documents the same choice.
            bind_addresses = [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")]
        elif host in ("0.0.0.0", "::"):  # nosec B104
            bind_addresses = [(socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")]  # nosec B104
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

        # One family failing (port taken on 127.0.0.1, ::1 free) closes what did bind and raises.
        # EAFNOSUPPORT / EADDRNOTAVAIL means that family is not on this host and is safely ignored.
        bind_errors: list[OSError] = []
        optional_family = {errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL}
        for family, ip in bind_addresses:
            sock: socket.socket | None = None
            try:
                sock = socket.socket(family, socket.SOCK_STREAM)
                # What was wrong: SO_REUSEADDR was set on every listener.
                # allow_reuse_address is False on Windows and was never read.
                # How: on Windows that option lets a second process bind the
                # same port, so a busy port looks free and can be taken over.
                # Why: set it only when the flag is true. POSIX keeps it so a
                # restart during TIME_WAIT still binds. Windows does not.
                if self.allow_reuse_address:
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
        self._accept_times.clear()
        # What was wrong: shutdown(cancel_futures=False) still ran connections
        # already queued after the listeners closed. MCP stop does not drain
        # first, so a stop kept handling those requests.
        # How: Future.cancel() never calls process_request_thread, so the
        # socket would also leak if we only cancelled.
        # Why: cancel work that has not started and close those sockets.
        # A future that already started still finishes and closes itself.
        # drain_executor waits with cancel_futures=False before this, so the
        # compute 30s grace still finishes queued work.
        with self._queued_lock:
            pending = list(self._queued.items())
        self.executor.shutdown(wait=False, cancel_futures=True)
        for fut, sock in pending:
            if fut.cancelled():
                try:
                    sock.close()
                except OSError:
                    pass

    def drain_executor(self, timeout: float) -> None:
        """Wait until accepted requests finish, then return."""
        done = threading.Event()

        def _wait() -> None:
            self.executor.shutdown(wait=True, cancel_futures=False)
            done.set()

        run_in_background(_wait, name="http-drain", daemon=True, dedicated=True)
        if not done.wait(timeout):
            log.warning("HTTP request drain exceeded %.0fs; abandoning in-flight handlers", timeout)

    def fileno(self) -> int:
        return self.socket.fileno()

    def get_request(self) -> tuple[Any, Any]:
        """TCPServer interface compatibility method."""
        conn, addr = super().get_request()
        return self._wrap_accepted_socket(conn, addr)

    def _wrap_accepted_socket(self, conn: Socket, addr: Any) -> tuple[Socket, Any]:
        """Apply configured socket timeout and optional TLS wrap."""
        # None means the handler sets the deadline. The old getattr fallback
        # of 30s never ran: __init__ always assigns this attribute.
        if self.socket_timeout is not None:
            try:
                conn.settimeout(self.socket_timeout)
            except OSError:
                pass
        if self.ssl_ctx is not None:
            conn = self.ssl_ctx.wrap_socket(conn, server_side=True, do_handshake_on_connect=False)
        return conn, addr

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A stalled read is a closed request, not a traceback on the console.

        What was wrong: only ``TimeoutError`` was treated as a stall.
        How: before Python 3.10, ``socket.timeout`` is a different class, so
        a timed-out ``recv`` still printed a traceback. On 3.10+ the names
        are one class. ``errors.py`` already catches both.
        Why: one tuple covers the older runtime this extension still claims.
        """
        _typ, exc, _tb = sys.exc_info()
        if isinstance(exc, (socket.timeout, TimeoutError)):
            host = client_address[0] if isinstance(client_address, tuple) and client_address else client_address
            log.info("HTTP read timed out from %s", host)
            return
        super().handle_error(request, client_address)

    def _accept_one(self, ready_sock: Socket) -> None:
        """Accept one connection, wrap it, and hand it to the pool.

        What was wrong: accept and TLS wrap shared one ``except OSError``.
        ``ssl.SSLError`` is an ``OSError``, so a failed wrap was logged as an
        accept error, slept 50 ms, and left the accepted socket open.
        How: the sleep is for a failing ``accept`` (EMFILE and similar). One
        bad client then held a file descriptor and stalled the accept thread.
        Why: close ``raw_conn`` on wrap failure and do not sleep. ``close``
        on a socket whose fd was already detached does not close that fd.
        """
        try:
            raw_conn, client_address = ready_sock.accept()
        except OSError as e:
            log.warning("Accept error: %s; backing off", e)
            time.sleep(0.05)
            return
        try:
            conn, client_address = self._wrap_accepted_socket(raw_conn, client_address)
        except OSError as e:
            try:
                raw_conn.close()
            except OSError:
                pass
            log.warning("Socket setup failed: %s", e)
            return
        if not self.verify_request(conn, client_address):
            self.shutdown_request(conn)
            return
        self._accept_times[conn] = time.monotonic()
        self.process_request(conn, client_address)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self._serving = True
        self._dual_is_shut_down.clear()
        # Do not clear _dual_shutdown_request here. shutdown() before this
        # thread enters leaves the flag set, so a stop that won the race does
        # not start listening again.
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
                        # Only listener sockets are registered. The check
                        # narrows fileobj for the type checker.
                        if not isinstance(ready_sock, socket.socket):
                            continue
                        self._accept_one(ready_sock)
                    self.service_actions()
        finally:
            self._serving = False
            self._dual_shutdown_request = False
            self._dual_is_shut_down.set()

    def shutdown(self) -> None:
        """Stop serve_forever when it is running.

        If this runs before serve_forever, the flag stays set and the next
        serve_forever returns immediately. That is the stop-before-listen
        race. Clearing the flag at the start of serve_forever would undo it.
        """
        self._dual_shutdown_request = True
        if self._serving:
            self._dual_is_shut_down.wait()

    def _drop_queued(self, fut: Future[Any]) -> None:
        with self._queued_lock:
            self._queued.pop(fut, None)

    def process_request(self, request: Any, client_address: Any) -> None:
        """Submit incoming request to the thread pool executor."""
        try:
            fut = self.executor.submit(self.process_request_thread, request, client_address)
        except Exception:
            log.exception("Failed to submit accepted connection")
            try:
                self.shutdown_request(request)
            except Exception:
                log.exception("Failed to close accepted connection")
            return
        # Register before the done callback. The worker can finish during
        # submit; add_done_callback then runs immediately and pops the entry.
        with self._queued_lock:
            self._queued[fut] = request
        fut.add_done_callback(self._drop_queued)

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
