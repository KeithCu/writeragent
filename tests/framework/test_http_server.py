# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dual-stack HTTP server: reuse, TLS wrap, cancel, and daemon workers."""

from concurrent.futures import Future
import errno
import os
import socket
import ssl
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

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


def test_accept_benign_error_returns_without_sleep_or_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _Handler)
    listener = MagicMock()
    listener.accept.side_effect = OSError(errno.ECONNABORTED, "Software caused connection abort")
    slept: list[float] = []
    monkeypatch.setattr("plugin.framework.http_server.time.sleep", lambda secs: slept.append(secs))
    caplog.set_level("WARNING", logger="plugin.framework.http_server")
    try:
        server._accept_one(listener)
        assert slept == []
        assert server._accept_pause_until == 0.0
        assert caplog.text == ""
    finally:
        server.server_close()


def test_accept_resource_error_backs_off_and_caps(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _Handler)
    listener = MagicMock()
    listener.accept.side_effect = OSError(errno.EMFILE, "Too many open files")
    slept: list[float] = []
    monkeypatch.setattr("plugin.framework.http_server.time.sleep", lambda secs: slept.append(secs))
    fixed_time = 1000.0
    monkeypatch.setattr("plugin.framework.http_server.time.monotonic", lambda: fixed_time)
    caplog.set_level("WARNING", logger="plugin.framework.http_server")
    pauses: list[float] = []
    try:
        for _ in range(10):
            server._accept_one(listener)
            pauses.append(server._accept_pause_until)
        assert slept == []
        assert pauses == [
            fixed_time + 0.05,
            fixed_time + 0.1,
            fixed_time + 0.2,
            fixed_time + 0.4,
            fixed_time + 0.8,
            fixed_time + 1.0,
            fixed_time + 1.0,
            fixed_time + 1.0,
            fixed_time + 1.0,
            fixed_time + 1.0,
        ]
        assert server._accept_backoff_sec == 1.0
        warnings = [
            r for r in caplog.records
            if r.levelname == "WARNING" and "Accept error" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert str(errno.EMFILE) in warnings[0].getMessage()
    finally:
        server.server_close()


def test_successful_accept_resets_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _Handler)
    listener = MagicMock()
    listener.accept.side_effect = OSError(errno.EMFILE, "Too many open files")
    slept: list[float] = []
    monkeypatch.setattr("plugin.framework.http_server.time.sleep", lambda secs: slept.append(secs))
    try:
        server._accept_one(listener)
        assert server._accept_pause_until > 0.0
        assert server._accept_backoff_sec == 0.1

        mock_conn = MagicMock()
        listener.accept.side_effect = None
        listener.accept.return_value = (mock_conn, ("127.0.0.1", 12345))
        server._accept_one(listener)
        assert server._accept_backoff_sec == 0.05
        assert server._accept_pause_until == 0.0
        assert slept == []
    finally:
        server.server_close()


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

    setattr(server.executor, "shutdown", _shutdown_spy)
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


def test_accept_queue_cap_closes_excess_socket() -> None:
    ran: list[object] = []

    class _Counting:
        def __init__(self, request: object, client_address: object, server: object) -> None:
            ran.append(request)

    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _Counting, max_threads=1, max_queued=1)
    hold = threading.Event()
    started = threading.Event()

    def _occupy() -> None:
        started.set()
        hold.wait(timeout=5)

    sock1 = MagicMock()
    sock2 = MagicMock()
    try:
        server.executor.submit(_occupy)
        assert started.wait(timeout=2)
        # One request stays queued and its mock socket is not closed.
        server.process_request(sock1, ("127.0.0.1", 1001))
        sock1.close.assert_not_called()
        assert server.executor._work_queue.qsize() == 1

        # The next request closes the socket, does not run the handler,
        # and leaves the queue depth unchanged.
        server.process_request(sock2, ("127.0.0.1", 1002))
        sock2.close.assert_called_once()
        assert ran == []
        assert server.executor._work_queue.qsize() == 1
    finally:
        hold.set()
        server.server_close()


def test_handle_error_connection_error_does_not_call_super() -> None:
    from unittest.mock import patch

    server = DualStackThreadPoolHTTPServer.__new__(DualStackThreadPoolHTTPServer)
    for exc_type in (ConnectionResetError, BrokenPipeError):
        try:
            raise exc_type("client closed")
        except ConnectionError:
            with patch("socketserver.BaseServer.handle_error") as super_handle:
                server.handle_error(None, ("127.0.0.1", 1))
        super_handle.assert_not_called()


def test_worker_start_failure_leaves_queue_empty_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _RequestThreads(max_workers=2, thread_name_prefix="test_start_fail")
    fail = True
    real_start = pool._start_one_locked

    def _failing_start() -> None:
        if fail:
            raise RuntimeError("can't start new thread")
        real_start()

    monkeypatch.setattr(pool, "_start_one_locked", _failing_start)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        pool.submit(lambda: 42)

    assert pool._pending == 0
    assert pool._work_queue.qsize() == 0
    assert len(pool._threads) == 0

    fail = False
    fut = pool.submit(lambda: 42)
    assert fut.result(timeout=2) == 42
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and pool._pending > 0:
        time.sleep(0.001)
    assert pool._pending == 0
    pool.shutdown(wait=True)


def test_worker_start_failure_with_live_worker_queues_and_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _RequestThreads(max_workers=2, thread_name_prefix="test_start_fallback")
    started = threading.Event()
    release = threading.Event()

    def _occupy() -> int:
        started.set()
        release.wait(timeout=5)
        return 1

    fut1 = pool.submit(_occupy)
    assert started.wait(timeout=2)
    assert len(pool._threads) == 1

    def _failing_start() -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(pool, "_start_one_locked", _failing_start)
    fut2 = pool.submit(lambda: 2)
    assert len(pool._threads) == 1

    release.set()
    assert fut1.result(timeout=2) == 1
    assert fut2.result(timeout=2) == 2
    assert len(pool._threads) == 1
    pool.shutdown(wait=True)


def test_process_request_closes_socket_on_start_failure_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[object] = []

    class _CountingHandler:
        def __init__(self, request: object, client_address: object, server: object) -> None:
            ran.append(request)

    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _CountingHandler, max_threads=2)
    fail = True
    real_start = server.executor._start_one_locked

    def _failing_start() -> None:
        if fail:
            raise RuntimeError("can't start new thread")
        real_start()

    monkeypatch.setattr(server.executor, "_start_one_locked", _failing_start)
    sock1 = MagicMock()
    server.process_request(sock1, ("127.0.0.1", 1001))
    sock1.close.assert_called_once()
    assert ran == []

    fail = False
    sock2 = MagicMock()
    server.process_request(sock2, ("127.0.0.1", 1002))
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not ran:
        time.sleep(0.01)
    assert ran == [sock2]
    server.server_close()


def _ipv6_loopback_supported() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as test_sock:
            test_sock.bind(("::1", 0))
            return True
    except OSError:
        return False


@pytest.mark.parametrize("host", ["", "localhost", "127.0.0.1", "::1"])
def test_loopback_hosts_listen_on_ipv4_and_ipv6_shared_port(host: str) -> None:
    if not _ipv6_loopback_supported():
        pytest.skip("IPv6 loopback not supported on this host")
    server = DualStackThreadPoolHTTPServer((host, 0), _Handler)
    try:
        assert len(server.sockets) == 2
        families = {s.family for s in server.sockets}
        assert socket.AF_INET in families
        assert socket.AF_INET6 in families
        ports = {s.getsockname()[1] for s in server.sockets}
        assert len(ports) == 1
        port = next(iter(ports))
        assert port != 0
        addresses = {s.getsockname()[0] for s in server.sockets}
        assert "0.0.0.0" not in addresses
        assert "::" not in addresses
        assert "127.0.0.1" in addresses
        assert "::1" in addresses
    finally:
        server.server_close()


def test_bind_ignores_eafnosupport_for_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    real_bind = socket.socket.bind

    def _fake_bind(self: socket.socket, addr: Any) -> None:
        if self.family == socket.AF_INET6:
            raise OSError(errno.EAFNOSUPPORT, "Address family not supported")
        real_bind(self, addr)

    monkeypatch.setattr(socket.socket, "bind", _fake_bind)
    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _Handler)
    try:
        assert len(server.sockets) == 1
        assert server.sockets[0].family == socket.AF_INET
    finally:
        server.server_close()


def test_occupied_ipv4_port_fails_fast_and_releases_ipv6() -> None:
    occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupier.bind(("127.0.0.1", 0))
    occupier.listen(1)
    port = occupier.getsockname()[1]
    try:
        with pytest.raises(OSError) as exc_info:
            DualStackThreadPoolHTTPServer(("127.0.0.1", port), _Handler)
        assert exc_info.value.errno in (errno.EADDRINUSE, 98, 48, 10048)

        if _ipv6_loopback_supported():
            test_v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            try:
                test_v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                test_v6.bind(("::1", port))
            finally:
                test_v6.close()
    finally:
        occupier.close()


def test_all_families_eafnosupport_raises_could_not_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_bind(self: socket.socket, addr: Any) -> None:
        raise OSError(errno.EAFNOSUPPORT, "Address family not supported")

    monkeypatch.setattr(socket.socket, "bind", _fake_bind)
    with pytest.raises(OSError, match="Could not bind to any address"):
        DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _Handler)


def test_shutdown_before_serve_forever_returns_immediately_and_clears_flag() -> None:
    ran: list[object] = []

    class _CountingHandler:
        def __init__(self, request: object, client_address: object, server: object) -> None:
            ran.append(request)

    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _CountingHandler)
    try:
        t0 = time.monotonic()
        server.shutdown()
        assert time.monotonic() - t0 < 0.1
        assert server._dual_shutdown_request is True

        serve_thread1 = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5})
        serve_thread1.start()
        serve_thread1.join(timeout=1.0)
        assert not serve_thread1.is_alive()
        assert server._dual_shutdown_request is False
        assert ran == []

        serve_thread2 = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5})
        serve_thread2.start()
        port = server.server_address[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2.0) as client:
            client.sendall(b"GET / HTTP/1.0\r\n\r\n")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not ran:
                time.sleep(0.01)
        assert len(ran) == 1
        server.shutdown()
        serve_thread2.join(timeout=2.0)
        assert not serve_thread2.is_alive()
    finally:
        server.server_close()


def test_shutdown_wakes_serve_forever_immediately_without_fd_leak() -> None:
    fd_dir = "/proc/self/fd"
    has_proc_fd = os.path.exists(fd_dir)
    fds_before = set(os.listdir(fd_dir)) if has_proc_fd else set()

    for _ in range(20):
        server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _Handler)
        try:
            serve_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 5.0}, daemon=True)
            serve_thread.start()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not server._serving:
                time.sleep(0.001)
            assert server._serving
            t0 = time.monotonic()
            server.shutdown()
            elapsed = time.monotonic() - t0
            assert elapsed < 0.1
            serve_thread.join(timeout=1.0)
            assert not serve_thread.is_alive()
        finally:
            server.server_close()
        assert server._wake_write is None

    if has_proc_fd:
        fds_after = set(os.listdir(fd_dir))
        assert len(fds_after - fds_before) == 0


def test_serve_forever_paused_accept_shutdown_wakes_in_under_100ms(monkeypatch: pytest.MonkeyPatch) -> None:
    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _Handler)
    slept: list[float] = []
    # Tip: patching http_server.time.sleep replaces the global time.sleep, so poll with the real one.
    real_sleep = time.sleep
    monkeypatch.setattr("plugin.framework.http_server.time.sleep", lambda secs: slept.append(secs))

    orig_accept = socket.socket.accept

    def _mock_accept(self_sock: socket.socket) -> tuple[socket.socket, Any]:
        if self_sock in server.sockets:
            raise OSError(errno.EMFILE, "Too many open files")
        return orig_accept(self_sock)

    monkeypatch.setattr(socket.socket, "accept", _mock_accept)

    serve_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 5.0}, daemon=True)
    try:
        serve_thread.start()
        port = server.server_address[1]
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                pass
        except OSError:
            pass
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and server._accept_pause_until == 0.0:
            real_sleep(0.005)
        assert server._accept_pause_until > 0.0

        t0 = time.monotonic()
        server.shutdown()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1
        serve_thread.join(timeout=1.0)
        assert not serve_thread.is_alive()
        assert slept == []
    finally:
        server.server_close()



