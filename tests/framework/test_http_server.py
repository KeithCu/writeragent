# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dual-stack HTTP server: reuse, TLS wrap, cancel, and daemon workers."""

from concurrent.futures import Future
import socket
import ssl
import threading
import time
from typing import Any
from unittest.mock import MagicMock

from plugin.framework.http_server import DualStackThreadPoolHTTPServer, _RequestThreads


class _Handler:
    def __init__(self, request: object, client_address: object, server: object) -> None:
        self.request = request
        self.client_address = client_address
        self.server = server


def _reuse(sock: socket.socket) -> int:
    return sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)


def test_reuseaddr_follows_allow_reuse_address() -> None:
    # The default is off on Windows (there SO_REUSEADDR lets another program grab the port),
    # so turn it on explicitly to check that the flag is honored.
    class _Reuse(DualStackThreadPoolHTTPServer):
        allow_reuse_address = True

    server = _Reuse(("127.0.0.1", 0), _Handler)
    try:
        assert server.sockets
        # macOS/BSD getsockopt returns the option's flag bit (4), not 1; any nonzero means on.
        assert all(_reuse(sock) != 0 for sock in server.sockets)
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


def test_burst_submit_runs_every_call_until_cap() -> None:
    for _iteration in range(40):
        pool = _RequestThreads(max_workers=8, thread_name_prefix=f"test_burst_{_iteration}")
        entered = 0
        lock = threading.Lock()
        hold = threading.Event()
        reached = threading.Event()

        def _task() -> None:
            nonlocal entered
            with lock:
                entered += 1
                if entered == 7:
                    reached.set()
            hold.wait(timeout=5)

        futures: list[Future[Any]] = []
        threads: list[threading.Thread] = []
        futures_lock = threading.Lock()
        gate = threading.Event()

        def _submitter() -> None:
            gate.wait(timeout=5)
            fut = pool.submit(_task)
            with futures_lock:
                futures.append(fut)

        for _i in range(7):
            t = threading.Thread(target=_submitter, daemon=True)
            threads.append(t)
            t.start()

        gate.set()
        for t in threads:
            t.join(timeout=2)

        try:
            assert reached.wait(timeout=1.0)
            with lock:
                assert entered == 7
            assert len(pool._threads) == 7
        finally:
            hold.set()
            for fut in futures:
                fut.result(timeout=5)
            pool.shutdown(wait=True)


def test_serial_submit_reuses_the_idle_thread() -> None:
    pool = _RequestThreads(max_workers=4, thread_name_prefix="test_serial")
    try:
        fut1 = pool.submit(lambda: 1)
        assert fut1.result(timeout=2) == 1
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and pool._pending > 0:
            time.sleep(0.001)
        assert len(pool._threads) == 1
        fut2 = pool.submit(lambda: 2)
        assert fut2.result(timeout=2) == 2
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and pool._pending > 0:
            time.sleep(0.001)
        assert len(pool._threads) == 1
    finally:
        pool.shutdown(wait=True)


def test_pool_cap_queues_the_overflow() -> None:
    pool = _RequestThreads(max_workers=1, thread_name_prefix="test_cap")
    entered = 0
    lock = threading.Lock()
    hold = threading.Event()
    first_entered = threading.Event()

    def _task() -> None:
        nonlocal entered
        with lock:
            entered += 1
        first_entered.set()
        hold.wait(timeout=5)

    try:
        fut1 = pool.submit(_task)
        fut2 = pool.submit(_task)
        assert first_entered.wait(timeout=2)
        time.sleep(0.2)
        with lock:
            assert entered == 1
        assert len(pool._threads) == 1
    finally:
        hold.set()
        fut1.result(timeout=5)
        fut2.result(timeout=5)
        pool.shutdown(wait=True)

    assert len(pool._threads) == 1


# ---- drain_executor: the compute server's shutdown grace -------------------
# compute_service/server.py stops with close_sockets() -> drain_executor(30s)
# -> server_close(). These pin both sides of that contract without the 30s.


def _drain_server_with_blocked_handler() -> tuple[DualStackThreadPoolHTTPServer, threading.Event, threading.Event, list[str]]:
    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _Handler, max_threads=2)
    started = threading.Event()
    release = threading.Event()
    finished: list[str] = []

    def _in_flight() -> None:
        started.set()
        release.wait(timeout=10)
        finished.append("done")

    server.executor.submit(_in_flight)
    assert started.wait(timeout=5)
    server.close_sockets()
    return server, started, release, finished


def test_drain_executor_waits_for_in_flight_handler_released_mid_wait(caplog) -> None:
    server, _started, release, finished = _drain_server_with_blocked_handler()
    waiting = threading.Event()
    real_shutdown = server.executor.shutdown

    def _shutdown_spy(*args: Any, **kwargs: Any) -> Any:
        # drain's background waiter is now blocked on the in-flight handler.
        waiting.set()
        return real_shutdown(*args, **kwargs)

    server.executor.shutdown = _shutdown_spy  # type: ignore[method-assign]
    drained = threading.Event()

    def _drain() -> None:
        server.drain_executor(10.0)
        drained.set()

    caplog.set_level("WARNING", logger="plugin.framework.http_server")
    drainer = threading.Thread(target=_drain, daemon=True)
    try:
        drainer.start()
        assert waiting.wait(timeout=5)
        # Still blocked: the handler has not been released yet.
        assert not drained.wait(timeout=0.05)
        assert finished == []
        release.set()
        assert drained.wait(timeout=5)
        assert finished == ["done"]
        assert "abandoning in-flight handlers" not in caplog.text
    finally:
        release.set()
        drainer.join(timeout=5)
        server.server_close()


def test_drain_executor_abandons_after_timeout_and_warns(caplog) -> None:
    server, _started, release, finished = _drain_server_with_blocked_handler()
    caplog.set_level("WARNING", logger="plugin.framework.http_server")
    try:
        server.drain_executor(0.05)
        # Returned while the handler is still in flight: it was abandoned.
        assert finished == []
        warnings = [r for r in caplog.records if r.levelname == "WARNING" and "abandoning in-flight handlers" in r.getMessage()]
        assert len(warnings) == 1
        assert "exceeded 0s" in warnings[0].getMessage()
    finally:
        release.set()
        server.server_close()
