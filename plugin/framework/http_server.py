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
import selectors
import socket
from socket import socket as Socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer
from typing import TYPE_CHECKING, Any

from plugin.framework.worker_pool import run_in_background

if TYPE_CHECKING:
    import ssl

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
    """HTTPServer that listens on both IPv4 and IPv6 loopback (or a single host) using a ThreadPoolExecutor.

    The socket accept loop uses selectors.DefaultSelector to monitor all bound sockets concurrently.
    Incoming connections are submitted to a ThreadPoolExecutor. The worker count is bounded.
    The accept queue is not: a burst waits for a thread instead of being rejected.
    Supports optional TLS socket wrapping and socket read/write timeouts.
    """

    request_queue_size: int = 128
    allow_reuse_address: bool = os.name != "nt"
    daemon_threads: bool = True

    _dual_is_shut_down: threading.Event
    _dual_shutdown_request: bool
    _serving: bool
    executor: ThreadPoolExecutor
    address_family: int
    server_address: tuple[str | bytes | bytearray, int] | tuple[str | bytes | bytearray, int, int, int]
    sockets: list[Socket]
    socket: Socket
    ssl_ctx: ssl.SSLContext | None = None
    socket_timeout: float | None = 30.0
    route_registry: Any = None
    bind_host: str | None = None

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
        self.socket_timeout = socket_timeout
        self.executor = ThreadPoolExecutor(max_workers=max_threads, thread_name_prefix=thread_name_prefix)

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
        self.executor.shutdown(wait=False, cancel_futures=False)

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
        timeout = getattr(self, "socket_timeout", 30.0)
        if timeout is not None:
            try:
                conn.settimeout(timeout)
            except OSError:
                pass
        ssl_ctx = getattr(self, "ssl_ctx", None)
        if ssl_ctx is not None:
            conn = ssl_ctx.wrap_socket(conn, server_side=True, do_handshake_on_connect=False)
        return conn, addr

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A stalled read is a closed request, not a traceback on the console."""
        _typ, exc, _tb = sys.exc_info()
        if isinstance(exc, TimeoutError):
            host = client_address[0] if isinstance(client_address, tuple) and client_address else client_address
            log.info("HTTP read timed out from %s", host)
            return
        super().handle_error(request, client_address)

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
                                raw_conn, client_address = ready_sock.accept()
                                conn, client_address = self._wrap_accepted_socket(raw_conn, client_address)
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
        """Stop serve_forever when it is running."""
        self._dual_shutdown_request = True
        if self._serving:
            self._dual_is_shut_down.wait()

    def process_request(self, request: Any, client_address: Any) -> None:
        """Submit incoming request to the thread pool executor."""
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
