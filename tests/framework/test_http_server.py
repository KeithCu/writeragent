# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dual-stack HTTP server: reuse, TLS wrap, cancel, and daemon workers."""

import socket
import ssl
import threading
from unittest.mock import MagicMock

from plugin.framework.http_server import DualStackThreadPoolHTTPServer


class _Handler:
    def __init__(self, request: object, client_address: object, server: object) -> None:
        self.request = request
        self.client_address = client_address
        self.server = server


def _reuse(sock: socket.socket) -> int:
    return sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)


def test_reuseaddr_follows_allow_reuse_address() -> None:
    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _Handler)
    try:
        assert server.sockets
        assert all(_reuse(sock) == 1 for sock in server.sockets)
    finally:
        server.server_close()

    class _NoReuse(DualStackThreadPoolHTTPServer):
        allow_reuse_address = False

    off = _NoReuse(("127.0.0.1", 0), _Handler)
    try:
        assert off.sockets
        assert all(_reuse(sock) == 0 for sock in off.sockets)
    finally:
        off.server_close()


def test_accept_error_backs_off(monkeypatch) -> None:
    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _Handler)
    listener = MagicMock()
    listener.accept.side_effect = OSError(24, "Too many open files")
    slept: list[float] = []
    monkeypatch.setattr("plugin.framework.http_server.time.sleep", lambda secs: slept.append(secs))
    try:
        server._accept_one(listener)
    finally:
        server.server_close()
    assert slept == [0.05]


def test_wrap_failure_closes_socket_without_backoff(monkeypatch) -> None:
    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _Handler)
    raw = MagicMock()
    listener = MagicMock()
    listener.accept.return_value = (raw, ("127.0.0.1", 1))
    server.ssl_ctx = MagicMock()
    server.ssl_ctx.wrap_socket.side_effect = ssl.SSLError("handshake failed")
    slept: list[float] = []
    monkeypatch.setattr("plugin.framework.http_server.time.sleep", lambda secs: slept.append(secs))
    try:
        server._accept_one(listener)
    finally:
        server.server_close()
    raw.close.assert_called_once()
    assert slept == []


def test_socket_timeout_none_does_not_set_deadline() -> None:
    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _Handler)
    conn = MagicMock()
    try:
        assert server.socket_timeout is None
        wrapped, _addr = server._wrap_accepted_socket(conn, ("127.0.0.1", 1))
    finally:
        server.server_close()
    assert wrapped is conn
    conn.settimeout.assert_not_called()


def test_server_close_closes_queued_socket_without_running_handler() -> None:
    ran: list[object] = []

    class _Counting:
        def __init__(self, request: object, client_address: object, server: object) -> None:
            ran.append(request)

    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _Counting, max_threads=1)
    hold = threading.Event()
    started = threading.Event()

    def _occupy() -> None:
        started.set()
        hold.wait(timeout=5)

    sock = MagicMock()
    try:
        server.executor.submit(_occupy)
        assert started.wait(timeout=2)
        server.process_request(sock, ("127.0.0.1", 9))
        server.server_close()
        sock.close.assert_called()
        assert ran == []
    finally:
        hold.set()
        server.server_close()


def test_request_workers_are_daemon_and_not_on_executor_atexit() -> None:
    import concurrent.futures.thread as cf_thread

    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _Handler, max_threads=1)
    started = threading.Event()
    try:
        server.executor.submit(started.set)
        assert started.wait(timeout=2)
        threads = server.executor._threads
        assert threads
        assert all(thread.daemon for thread in threads)
        for thread in threads:
            assert thread not in cf_thread._threads_queues
    finally:
        server.server_close()
