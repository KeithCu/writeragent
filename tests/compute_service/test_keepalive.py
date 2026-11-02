# WriterAgent - Keep-Alive Tests for Python Compute Service
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTP/1.1 persistent connections: idle sockets live on the accept thread, a worker holds one only per request."""

from __future__ import annotations

import json
import selectors
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

import compute_service.wsgi_server as cs_http
from compute_service.config import ComputeSettings
from compute_service.server import create_wsgi_app
from compute_service.wsgi_server import ConnState, WSGIDualStackServer, _next_action
from plugin.framework.http_server import DualStackThreadPoolHTTPServer


class ConnectionReader:
    """Read HTTP responses one after another from a persistent socket."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.f = sock.makefile("rb")

    def read_response(self, timeout: float = 3.0) -> tuple[str, int, dict[str, str], bytes]:
        self.sock.settimeout(timeout)
        status_line = self.f.readline().decode("latin1").rstrip("\r\n")
        if not status_line:
            return "", 0, {}, b""
        parts = status_line.split(" ", 2)
        headers: dict[str, str] = {}
        while line := self.f.readline().decode("latin1").rstrip("\r\n"):
            name, val = line.split(":", 1)
            headers[name.strip().lower()] = val.strip()
        if "content-length" in headers:
            body = self.f.read(int(headers["content-length"]))
        elif headers.get("connection") == "close":
            body = self.f.read()
        else:
            body = b""
        return parts[0], int(parts[1]), headers, body

    def assert_eof(self, timeout: float = 1.0) -> None:
        self.sock.settimeout(timeout)
        assert self.sock.recv(1024) == b""


def _start_server(
    max_threads: int = 2,
    semaphore: threading.Semaphore | None = None,
    settings_override: dict[str, Any] | None = None,
) -> tuple[WSGIDualStackServer, int, threading.Thread]:
    opts: dict[str, Any] = {"log_level": "DEBUG", "api_key": ""}
    opts.update(settings_override or {})

    def _exec(code: str, **kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "result": 1, "id": kwargs.get("req_id")}

    app = create_wsgi_app(ComputeSettings(**opts), execute_fn=_exec, semaphore=semaphore)
    server = WSGIDualStackServer("127.0.0.1", 0, max_threads=max_threads)
    server.set_app(app)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, server.srv.server_port, t


def _stop_server(server: WSGIDualStackServer, t: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    t.join(timeout=3.0)


@contextmanager
def _server(**kwargs: Any) -> Iterator[tuple[WSGIDualStackServer, int]]:
    server, port, t = _start_server(**kwargs)
    try:
        yield server, port
    finally:
        _stop_server(server, t)


@contextmanager
def _client(port: int) -> Iterator[ConnectionReader]:
    sock = socket.create_connection(("127.0.0.1", port))
    reader = ConnectionReader(sock)
    try:
        yield reader
    finally:
        reader.f.close()
        sock.close()


def _execute(req_id: str, *, version: str = "1.1", extra: str = "", **fields: Any) -> bytes:
    body = json.dumps({"id": req_id, "code": "result = 1", **fields}).encode()
    head = f"POST /v1/execute HTTP/{version}\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n{extra}\r\n"
    return head.encode("latin1") + body


_HEALTH = b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"


def _parked(server: WSGIDualStackServer, count: int, timeout: float = 1.0) -> bool:
    """Wait until ``count`` connections are parked and no worker is busy."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(server._conns) == count and server.executor._pending == 0:
            return True
        time.sleep(0.02)
    return False


def test_mcp_park_idle_connections_is_false() -> None:
    from plugin.mcp.server import _ThreadedHTTPServer

    assert DualStackThreadPoolHTTPServer.park_idle_connections is False
    assert _ThreadedHTTPServer.park_idle_connections is False
    assert WSGIDualStackServer.park_idle_connections is True


def test_keepalive_reuse_across_requests_and_no_held_thread() -> None:
    with _server() as (server, port), _client(port) as c:
        for n in (1, 2):
            c.sock.sendall(_execute(f"req-{n}"))
            ver, status, hdrs, body = c.read_response()
            assert (ver, status) == ("HTTP/1.1", 200)
            assert hdrs.get("content-length") == str(len(body))
            assert "close" not in hdrs.get("connection", "")
            assert json.loads(body)["id"] == f"req-{n}"
            assert _parked(server, 1)


def test_idle_connections_hold_no_threads_concurrency() -> None:
    """More idle connections than pool threads all stay usable, and /health still answers."""
    with _server() as (server, port):
        socks = [socket.create_connection(("127.0.0.1", port)) for _ in range(5)]
        readers = [ConnectionReader(s) for s in socks]
        try:
            for idx, r in enumerate(readers):
                r.sock.sendall(_execute(f"conn-{idx}"))
                assert r.read_response()[1] == 200
            assert _parked(server, 5)
            with _client(port) as h:
                h.sock.sendall(_HEALTH)
                assert h.read_response()[1] == 200
            for idx, r in enumerate(readers):
                r.sock.sendall(_execute(f"conn-{idx}-second"))
                _ver, status, _hdrs, body = r.read_response()
                assert status == 200 and json.loads(body)["id"] == f"conn-{idx}-second"
        finally:
            for s in socks:
                s.close()


def test_fresh_accept_parks_without_holding_a_worker() -> None:
    with _server() as (server, port):
        socks = [socket.create_connection(("127.0.0.1", port)) for _ in range(10)]
        try:
            assert _parked(server, 10)
            reader = ConnectionReader(socks[0])
            socks[0].sendall(_execute("first", timeout_ms=100))
            assert reader.read_response()[1] == 200
            assert _parked(server, 10)
            with _client(port) as h:
                h.sock.sendall(_HEALTH)
                assert json.loads(h.read_response(timeout=1.0)[3])["status"] == "healthy"
        finally:
            for s in socks:
                s.close()


def test_idle_timeout_closes_without_408(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs_http, "REQUEST_READ_TIMEOUT_SEC", 0.15)
    with _server() as (_server_obj, port), _client(port) as c:
        c.sock.sendall(_execute("req-1"))
        assert c.read_response()[1] == 200
        c.assert_eof(timeout=1.5)


def test_parked_cap_closes_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs_http, "_MAX_PARKED", 2)
    with _server() as (server, port):
        oldest = socket.create_connection(("127.0.0.1", port))
        try:
            assert _parked(server, 1)
            with _client(port) as b, _client(port) as c:
                oldest.settimeout(1.0)
                assert oldest.recv(1024) == b""
                assert _parked(server, 2)
                for r in (b, c):
                    r.sock.sendall(_HEALTH)
                    assert r.read_response()[1] == 200
        finally:
            oldest.close()


def test_pipelined_requests_both_answered() -> None:
    with _server() as (_s, port), _client(port) as c:
        c.sock.sendall(_execute("pipe-1") + _execute("pipe-2"))
        for n in (1, 2):
            _ver, status, _hdrs, body = c.read_response()
            assert status == 200 and json.loads(body)["id"] == f"pipe-{n}"


def test_client_shutdown_wr_eof_closes_cleanly() -> None:
    with _server() as (server, port), _client(port) as c:
        c.sock.sendall(_execute("req-eof"))
        assert c.read_response()[1] == 200
        c.sock.shutdown(socket.SHUT_WR)
        c.assert_eof(timeout=1.5)
        assert _parked(server, 0)


def test_header_timeout_closes_without_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request line that stalls past the per-read header timeout closes the connection; no 408 is sent."""
    monkeypatch.setattr(cs_http, "_HEADER_READ_TIMEOUT_SEC", 0.2)
    with _server() as (_s, port), _client(port) as c:
        c.sock.sendall(b"POST /v1/ex")
        c.assert_eof(timeout=2.0)


@pytest.mark.parametrize("extra", ["", "Connection: keep-alive\r\n"])
def test_http_1_0_closes(extra: str) -> None:
    with _server() as (_s, port), _client(port) as c:
        c.sock.sendall(_execute("http10", version="1.0", extra=extra))
        ver, status, hdrs, body = c.read_response()
        assert (ver, status) == ("HTTP/1.1", 200)
        assert json.loads(body)["id"] == "http10"
        assert hdrs.get("connection") == "close"
        c.assert_eof()


def test_expect_100_continue_sent_once() -> None:
    with _server() as (_s, port), _client(port) as c:
        raw = _execute("req-100", extra="Expect: 100-continue\r\n")
        head, body = raw.split(b"\r\n\r\n", 1)
        c.sock.sendall(head + b"\r\n\r\n")
        c.sock.settimeout(3.0)
        assert "100 Continue" in c.f.readline().decode("latin1")
        assert c.f.readline() == b"\r\n"
        c.sock.sendall(body)
        assert "200 OK" in c.f.readline().decode("latin1")


def test_reuse_after_503_queue_timeout() -> None:
    sem = threading.Semaphore(0)
    try:
        with _server(semaphore=sem) as (_s, port), _client(port) as c:
            c.sock.sendall(_execute("req-503", timeout_ms=50))
            _ver, status, hdrs, _body = c.read_response()
            assert status == 503 and "close" not in hdrs.get("connection", "")
            sem.release()
            c.sock.sendall(_execute("req-ok", timeout_ms=1000))
            _ver, status, _hdrs, body = c.read_response()
            assert status == 200 and json.loads(body)["id"] == "req-ok"
    finally:
        sem.release()


def test_accept_times_restamped_per_request() -> None:
    """Each request on a reused socket gets a fresh clock, so idle time is not queue time."""
    with _server() as (_s, port), _client(port) as c:
        c.sock.sendall(_execute("req-1"))
        assert c.read_response()[1] == 200
        time.sleep(0.25)
        c.sock.sendall(_execute("req-2", timeout_ms=100))
        _ver, status, _hdrs, body = c.read_response()
        assert status == 200 and json.loads(body)["id"] == "req-2"


_CHUNKED = "Transfer-Encoding: chunked\r\n"


@pytest.mark.parametrize(
    ("settings", "raw", "status"),
    [
        ({"max_body_bytes": 1024}, b"POST /v1/execute HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: 2048\r\n\r\n" + b"x" * 2048, 413),
        ({}, f"POST /v1/execute HTTP/1.1\r\nHost: 127.0.0.1\r\n{_CHUNKED}\r\n5\r\nhello\r\n0\r\n\r\n".encode(), 400),
        ({}, b"POST /nonexistent HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 5\r\n\r\nhello", 404),
        ({}, b"POST /health HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 5\r\n\r\nhello", 405),
        ({}, f"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: {len(_HEALTH)}\r\n\r\n".encode() + _HEALTH, 200),
        ({}, f"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n{_CHUNKED}\r\n5\r\nhello\r\n0\r\n\r\n".encode(), 200),
        ({"api_key": "secret"}, f"POST /v1/execute HTTP/1.1\r\nHost: 127.0.0.1\r\n{_CHUNKED}Authorization: Bearer wrong\r\n\r\n".encode() + _HEALTH, 401),
        ({"api_key": "secret"}, f"POST /v1/execute HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 5\r\n{_CHUNKED}Authorization: Bearer wrong\r\n\r\n0\r\n\r\n".encode() + _HEALTH, 401),
        ({"api_key": "secret"}, b"POST /v1/execute HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 2097152\r\n\r\n", 401),
        ({"api_key": "secret"}, f"POST /v1/execute HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n{_CHUNKED}\r\n5\r\nhello\r\n0\r\n\r\n".encode(), 401),
        ({}, b'POST /v1/execute HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://malicious.example.com\r\nContent-Type: application/json\r\nContent-Length: 21\r\n\r\n{"code":"result = 1"}', 403),
    ],
)
def test_unread_body_closes_connection(settings: dict[str, Any], raw: bytes, status: int) -> None:
    """A response that leaves body bytes unread sends Connection: close and closes, so they are never parsed as a request."""
    with _server(settings_override=settings) as (_s, port), _client(port) as c:
        c.sock.sendall(raw)
        _ver, got, hdrs, _body = c.read_response()
        assert got == status
        assert hdrs.get("connection") == "close"
        c.assert_eof()


def test_head_is_rejected_bodiless_and_closes() -> None:
    with _server() as (_s, port), _client(port) as c:
        c.sock.sendall(b"HEAD /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        c.sock.settimeout(2.0)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = c.sock.recv(1024)
            assert chunk, "unexpected EOF reading HEAD headers"
            buf += chunk
        head, extra = buf.split(b"\r\n\r\n", 1)
        assert extra == b""
        lines = head.decode("latin1").split("\r\n")
        assert "405" in lines[0]
        hdrs = {k.strip().lower(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:])}
        assert hdrs.get("content-length") == "0"
        assert hdrs.get("allow") == "GET, POST"
        assert hdrs.get("connection") == "close"
        c.assert_eof()


def test_body_drain_stops_at_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import compute_service.server as cs_server

    monkeypatch.setattr(cs_server, "REQUEST_READ_TIMEOUT_SEC", 0.3)
    stop = threading.Event()
    with _server(settings_override={"api_key": "correct-key"}) as (_s, port), _client(port) as c:

        def _trickle() -> None:
            while not stop.wait(0.02):
                try:
                    c.sock.sendall(b"x")
                except OSError:
                    return

        c.sock.sendall(b"POST /v1/execute HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 1000000\r\n\r\n")
        trickler = threading.Thread(target=_trickle, daemon=True)
        trickler.start()
        try:
            started = time.monotonic()
            _ver, status, hdrs, _body = c.read_response(timeout=2.0)
            assert time.monotonic() - started < 0.85
            assert status == 401 and hdrs.get("connection") == "close"
            c.assert_eof()
        finally:
            stop.set()
            trickler.join(timeout=1.0)


def test_shutdown_closes_parked_sockets_promptly() -> None:
    server, port, t = _start_server()
    with _client(port) as c:
        c.sock.sendall(_execute("req-1"))
        assert c.read_response()[1] == 200
        assert _parked(server, 1)
        started = time.monotonic()
        server.shutdown()
        server.close_sockets()
        server.drain_executor(5.0)
        server.server_close()
        t.join(timeout=3.0)
        assert time.monotonic() - started < 1.5
        c.assert_eof()


def test_next_action() -> None:
    a, b = socket.socketpair()
    try:
        state = ConnState(a, ("127.0.0.1", 1), a.makefile("rb", -1), 0.0)
        assert _next_action(state) == "park"
        b.sendall(b"GET")
        assert _next_action(state) == "again"
        assert state.rfile.read(3) == b"GET"
        b.close()
        assert _next_action(state) == "close"
        state.rfile.close()
    finally:
        a.close()
        b.close()


def _offline_server() -> WSGIDualStackServer:
    """A bound server whose accept loop never runs, so a test can drive ``_park_tick`` itself."""
    return WSGIDualStackServer("127.0.0.1", 0, max_threads=1)


def test_park_register_failure_closes_connection() -> None:
    server = _offline_server()
    a, b = socket.socketpair()
    try:
        state = ConnState(a, ("127.0.0.1", 1), a.makefile("rb", -1), time.monotonic())
        server._conns[a] = state
        server._park_queue.put(state)
        selector = MagicMock()
        selector.register.side_effect = OSError(24, "Too many open files")
        selector.get_map.return_value = {}
        assert server._park_tick(selector, []) is None
        assert a not in server._conns
        assert a.fileno() == -1
    finally:
        a.close()
        b.close()
        server.server_close()


def test_tick_dispatches_readable_and_sweeps_idle() -> None:
    """A readable parked socket goes to a worker even with the accept queue full; a quiet expired one closes."""
    server = _offline_server()
    ran: list[Any] = []
    server.process_request_thread = lambda request, client_address: ran.append(request)  # type: ignore[method-assign]
    (r1, r2), (q1, q2) = socket.socketpair(), socket.socketpair()
    with selectors.DefaultSelector() as selector:
        try:
            now = time.monotonic()
            readable = ConnState(r1, ("127.0.0.1", 1), r1.makefile("rb", -1), now)
            quiet = ConnState(q1, ("127.0.0.1", 2), q1.makefile("rb", -1), now - cs_http.REQUEST_READ_TIMEOUT_SEC - 1)
            for st in (readable, quiet):
                server._conns[st.sock] = st
                selector.register(st.sock, selectors.EVENT_READ, data=st)
            r2.sendall(b"x")
            with server.executor._lock:
                server.executor._pending = server.executor._max_workers + server.executor._max_queued
            assert server._park_tick(selector, selector.select(0.5)) is None
            deadline = time.monotonic() + 2.0
            while not ran and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ran == [r1]
            assert q1 not in server._conns and q1.fileno() == -1
            assert not selector.get_map()
        finally:
            with server.executor._lock:
                server.executor._pending = 0
            for s in (r1, r2, q1, q2):
                s.close()
            server.server_close()


def test_queue_full_on_dispatch_closes_connection() -> None:
    server = _offline_server()
    a, b = socket.socketpair()
    try:
        server._conns[a] = ConnState(a, ("127.0.0.1", 1), a.makefile("rb", -1), time.monotonic())
        with server.executor._lock:
            server.executor._pending = server.executor._max_workers + server.executor._max_queued + 10
        server.process_request(a, ("127.0.0.1", 1))
        assert a not in server._conns and a.fileno() == -1
    finally:
        with server.executor._lock:
            server.executor._pending = 0
        a.close()
        b.close()
        server.server_close()
