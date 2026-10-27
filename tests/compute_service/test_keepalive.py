# WriterAgent - Keep-Alive Tests for Python Compute Service
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for HTTP/1.1 persistent connections, idle connection parking, and close semantics."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any

import pytest

import compute_service.http_server as cs_http
from compute_service.config import ComputeSettings
from compute_service.http_server import WSGIDualStackServer
from compute_service.server import create_wsgi_app
from plugin.framework.http_server import DualStackThreadPoolHTTPServer


class ConnectionReader:
    """Helper to read HTTP responses sequentially from a persistent socket."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.f = sock.makefile("rb")

    def read_response(self, timeout: float = 3.0) -> tuple[str, int, str, dict[str, str], bytes]:
        self.sock.settimeout(timeout)
        status_line = self.f.readline().decode("latin1").rstrip("\r\n")
        if not status_line:
            return "", 0, "", {}, b""
        parts = status_line.split(" ", 2)
        http_ver = parts[0]
        status_code = int(parts[1])
        status_msg = parts[2] if len(parts) > 2 else ""
        headers: dict[str, str] = {}
        while True:
            line = self.f.readline().decode("latin1").rstrip("\r\n")
            if not line:
                break
            name, val = line.split(":", 1)
            headers[name.strip().lower()] = val.strip()

        body = b""
        if "content-length" in headers:
            length = int(headers["content-length"])
            body = self.f.read(length)
        elif headers.get("connection") == "close":
            body = self.f.read()
        return http_ver, status_code, status_msg, headers, body

    def close(self) -> None:
        try:
            self.f.close()
        except Exception:
            pass


def _start_server(
    max_threads: int = 4,
    execute_fn: Any = None,
    worker_semaphore: threading.Semaphore | None = None,
    settings_override: dict[str, Any] | None = None,
) -> tuple[WSGIDualStackServer, int, threading.Thread]:
    opts: dict[str, Any] = {"log_level": "DEBUG", "api_key": ""}
    if settings_override:
        opts.update(settings_override)
    settings = ComputeSettings(**opts)

    def _default_exec(code: str, **kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "result": 1, "id": kwargs.get("req_id")}

    fn = execute_fn or _default_exec
    app = create_wsgi_app(settings, execute_fn=fn, worker_semaphore=worker_semaphore)
    server = WSGIDualStackServer("127.0.0.1", 0, max_threads=max_threads)
    server.set_app(app)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, server.srv.server_port, t


def _stop_server(server: WSGIDualStackServer, t: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    t.join(timeout=3.0)


def test_mcp_park_idle_connections_is_false() -> None:
    """MCP server and base class park_idle_connections must be False."""
    from plugin.mcp.server import _ThreadedHTTPServer

    assert DualStackThreadPoolHTTPServer.park_idle_connections is False
    assert _ThreadedHTTPServer.park_idle_connections is False
    assert WSGIDualStackServer.park_idle_connections is True


def test_keepalive_reuse_across_requests_and_no_held_thread() -> None:
    """Two POSTs on one TCP socket with HTTP/1.1; idle socket holds 0 pool threads."""
    server, port, t = _start_server(max_threads=2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        reader = ConnectionReader(sock)

        # First request
        body1 = b'{"id":"req-1","code":"result = 1"}'
        req1 = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body1)}\r\n"
            f"\r\n"
        ).encode("latin1") + body1
        sock.sendall(req1)

        http_ver, status, msg, headers, resp_body1 = reader.read_response()
        assert http_ver == "HTTP/1.1"
        assert status == 200
        assert headers.get("content-length") == str(len(resp_body1))
        assert "close" not in headers.get("connection", "")
        parsed1 = json.loads(resp_body1.decode("utf-8"))
        assert parsed1.get("id") == "req-1"

        # Wait briefly for worker to hand socket back to accept thread
        time.sleep(0.05)
        # Verify no threads are busy/pending in the executor
        assert server.executor._pending == 0

        # Second request on the SAME socket
        body2 = b'{"id":"req-2","code":"result = 2"}'
        req2 = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body2)}\r\n"
            f"\r\n"
        ).encode("latin1") + body2
        sock.sendall(req2)

        http_ver2, status2, msg2, headers2, resp_body2 = reader.read_response()
        assert http_ver2 == "HTTP/1.1"
        assert status2 == 200
        assert headers2.get("content-length") == str(len(resp_body2))
        parsed2 = json.loads(resp_body2.decode("utf-8"))
        assert parsed2.get("id") == "req-2"

        reader.close()
    finally:
        sock.close()
        _stop_server(server, t)


def test_idle_connections_hold_no_threads_concurrency() -> None:
    """More idle connections than pool threads all remain served."""
    # 2 worker threads, but 5 idle connections
    server, port, t = _start_server(max_threads=2)
    socks: list[socket.socket] = []
    readers: list[ConnectionReader] = []
    try:
        for idx in range(5):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("127.0.0.1", port))
            socks.append(s)
            r = ConnectionReader(s)
            readers.append(r)

            body = json.dumps({"id": f"conn-{idx}", "code": "x = 1"}).encode("utf-8")
            req = (
                f"POST /v1/execute HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"\r\n"
            ).encode("latin1") + body
            s.sendall(req)
            _ver, status, _msg, _hdrs, resp = r.read_response()
            assert status == 200

        # Wait briefly for all 5 to park
        time.sleep(0.08)
        assert server.executor._pending == 0

        # In that idle gap, a GET /health on a 6th connection completes immediately
        s_health = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s_health.connect(("127.0.0.1", port))
        r_health = ConnectionReader(s_health)
        s_health.sendall(f"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode("latin1"))
        _ver, status, _msg, _hdrs, resp = r_health.read_response()
        assert status == 200
        r_health.close()
        s_health.close()

        # And each of the 5 idle connections can be reused for another request
        for idx in range(5):
            s = socks[idx]
            r = readers[idx]
            body = json.dumps({"id": f"conn-{idx}-second", "code": "x = 2"}).encode("utf-8")
            req = (
                f"POST /v1/execute HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"\r\n"
            ).encode("latin1") + body
            s.sendall(req)
            _ver, status, _msg, _hdrs, resp = r.read_response()
            assert status == 200
            assert json.loads(resp.decode("utf-8")).get("id") == f"conn-{idx}-second"

        for r in readers:
            r.close()
    finally:
        for s in socks:
            s.close()
        _stop_server(server, t)


def test_idle_timeout_closes_without_408(monkeypatch: pytest.MonkeyPatch) -> None:
    """Idle timeout closes socket cleanly without sending 408 bytes."""
    # Patch idle timeout to 0.15s
    monkeypatch.setattr(cs_http, "_REQUEST_READ_TIMEOUT_SEC", 0.15)
    server, port, t = _start_server(max_threads=2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        reader = ConnectionReader(sock)

        body = b'{"id":"req-1","code":"result = 1"}'
        req = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode("latin1") + body
        sock.sendall(req)

        _ver, status, _msg, _hdrs, resp = reader.read_response()
        assert status == 200

        # Now write nothing. Wait for idle timeout
        sock.settimeout(1.5)
        # Should receive EOF (b"") without any 408 response
        tail = sock.recv(1024)
        assert tail == b""

        reader.close()
    finally:
        sock.close()
        _stop_server(server, t)


def test_pipelined_requests_both_answered() -> None:
    """Two full requests sent back-to-back are both answered and framed apart."""
    server, port, t = _start_server(max_threads=2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        reader = ConnectionReader(sock)

        body1 = b'{"id":"pipe-1","code":"result = 1"}'
        req1 = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body1)}\r\n"
            f"\r\n"
        ).encode("latin1") + body1

        body2 = b'{"id":"pipe-2","code":"result = 2"}'
        req2 = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body2)}\r\n"
            f"\r\n"
        ).encode("latin1") + body2

        # Send both requests simultaneously
        sock.sendall(req1 + req2)

        ver1, status1, _msg1, hdrs1, resp1 = reader.read_response()
        assert status1 == 200
        assert json.loads(resp1.decode("utf-8")).get("id") == "pipe-1"

        ver2, status2, _msg2, hdrs2, resp2 = reader.read_response()
        assert status2 == 200
        assert json.loads(resp2.decode("utf-8")).get("id") == "pipe-2"

        reader.close()
    finally:
        sock.close()
        _stop_server(server, t)


def test_unread_body_closes_connection() -> None:
    """Unread or invalid bodies send Connection: close and close connection."""
    server, port, t = _start_server(max_threads=2, settings_override={"max_body_bytes": 1024})

    # 1. 413 Payload Too Large
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        reader = ConnectionReader(sock)
        big_body = b"x" * 2048
        req = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(big_body)}\r\n"
            f"\r\n"
        ).encode("latin1") + big_body
        sock.sendall(req)

        _ver, status, _msg, hdrs, _resp = reader.read_response()
        assert status == 413
        assert hdrs.get("connection") == "close"
        sock.settimeout(1.0)
        assert sock.recv(1024) == b""  # EOF
        reader.close()
    finally:
        sock.close()

    # 2. Chunked Transfer-Encoding
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        reader = ConnectionReader(sock)
        req = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Transfer-Encoding: chunked\r\n"
            f"\r\n"
            f"5\r\nhello\r\n0\r\n\r\n"
        ).encode("latin1")
        sock.sendall(req)

        _ver, status, _msg, hdrs, _resp = reader.read_response()
        assert status == 400
        assert hdrs.get("connection") == "close"
        sock.settimeout(1.0)
        assert sock.recv(1024) == b""  # EOF
        reader.close()
    finally:
        sock.close()

    # 3. 404 with body
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        reader = ConnectionReader(sock)
        body = b"hello"
        req = (
            f"POST /nonexistent HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode("latin1") + body
        sock.sendall(req)

        _ver, status, _msg, hdrs, _resp = reader.read_response()
        assert status == 404
        assert hdrs.get("connection") == "close"
        sock.settimeout(1.0)
        assert sock.recv(1024) == b""  # EOF
        reader.close()
    finally:
        sock.close()

    # 4. 405 with body
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        reader = ConnectionReader(sock)
        body = b"hello"
        req = (
            f"POST /health HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode("latin1") + body
        sock.sendall(req)

        _ver, status, _msg, hdrs, _resp = reader.read_response()
        assert status == 405
        assert hdrs.get("connection") == "close"
        sock.settimeout(1.0)
        assert sock.recv(1024) == b""  # EOF
        reader.close()
    finally:
        sock.close()

    _stop_server(server, t)


def test_shutdown_closes_parked_sockets_promptly() -> None:
    """Server shutdown unregisters and closes parked sockets without waiting."""
    server, port, t = _start_server(max_threads=2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        reader = ConnectionReader(sock)

        body = b'{"id":"req-1","code":"result = 1"}'
        req = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode("latin1") + body
        sock.sendall(req)

        _ver, status, _msg, _hdrs, _resp = reader.read_response()
        assert status == 200
        time.sleep(0.05)
        assert len(server._parked) == 1

        # Shut down server now
        shutdown_start = time.monotonic()
        server.shutdown()
        server.close_sockets()
        server.drain_executor(5.0)
        server.server_close()
        t.join(timeout=3.0)
        shutdown_elapsed = time.monotonic() - shutdown_start

        # Should be almost instantaneous (< 1.5s, well below 30s drain)
        assert shutdown_elapsed < 1.5

        # Socket must be closed
        sock.settimeout(1.0)
        assert sock.recv(1024) == b""
        reader.close()
    finally:
        sock.close()


def test_http_1_0_without_keepalive_closes() -> None:
    """HTTP/1.0 request without Connection: keep-alive sends Connection: close and closes."""
    server, port, t = _start_server(max_threads=2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        reader = ConnectionReader(sock)

        body = b'{"id":"req-1.0","code":"result = 1"}'
        req = (
            f"POST /v1/execute HTTP/1.0\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode("latin1") + body
        sock.sendall(req)

        _ver, status, _msg, hdrs, _resp = reader.read_response()
        assert status == 200
        assert hdrs.get("connection") == "close"
        sock.settimeout(1.0)
        assert sock.recv(1024) == b""  # EOF
        reader.close()
    finally:
        sock.close()
        _stop_server(server, t)


def test_expect_100_continue_sent_once() -> None:
    """Expect: 100-continue receives exactly one 100 Continue before 200 OK."""
    server, port, t = _start_server(max_threads=2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        body = b'{"id":"req-100","code":"result = 1"}'
        headers = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Expect: 100-continue\r\n"
            f"\r\n"
        ).encode("latin1")
        sock.sendall(headers)

        sock.settimeout(3.0)
        f = sock.makefile("rb")
        line1 = f.readline().decode("latin1").rstrip("\r\n")
        assert "100 Continue" in line1
        # Blank line after 100 Continue
        line2 = f.readline().decode("latin1").rstrip("\r\n")
        assert line2 == ""

        # Now send body
        sock.sendall(body)

        # Next line must be 200 OK, NOT another 100 Continue!
        line3 = f.readline().decode("latin1").rstrip("\r\n")
        assert "200 OK" in line3
        f.close()
    finally:
        sock.close()
        _stop_server(server, t)


def test_client_shutdown_wr_eof_closes_cleanly() -> None:
    """Client shutdown(SHUT_WR) after request closes server side with eof reason."""
    server, port, t = _start_server(max_threads=2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        reader = ConnectionReader(sock)

        body = b'{"id":"req-eof","code":"result = 1"}'
        req = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode("latin1") + body
        sock.sendall(req)

        _ver, status, _msg, _hdrs, _resp = reader.read_response()
        assert status == 200

        # Client half-closes
        sock.shutdown(socket.SHUT_WR)

        # Server detects EOF and closes cleanly
        sock.settimeout(1.5)
        assert sock.recv(1024) == b""
        reader.close()
    finally:
        sock.close()
        _stop_server(server, t)


def test_header_timeout_sends_408_and_connection_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partial request line stalling past timeout gets 408 with Connection: close."""
    monkeypatch.setattr(cs_http, "_REQUEST_READ_TIMEOUT_SEC", 0.2)
    server, port, t = _start_server(max_threads=2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        reader = ConnectionReader(sock)

        # Partial request line, then stall
        sock.sendall(b"POST /v1/ex")
        _ver, status, _msg, hdrs, _resp = reader.read_response(timeout=2.0)
        assert status == 408
        assert hdrs.get("connection") == "close"
        assert "content-length" in hdrs

        sock.settimeout(1.0)
        assert sock.recv(1024) == b""  # EOF
        reader.close()
    finally:
        sock.close()
        _stop_server(server, t)


def test_reuse_after_503_queue_timeout() -> None:
    """Connection can be reused after a 503 from saturated concurrency semaphore."""
    sem = threading.Semaphore(0)  # Always saturated
    server, port, t = _start_server(max_threads=2, worker_semaphore=sem)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        reader = ConnectionReader(sock)

        # Request 1 with 50ms timeout -> hits 503
        body1 = b'{"id":"req-503","code":"result = 1","timeout_ms":50}'
        req1 = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body1)}\r\n"
            f"\r\n"
        ).encode("latin1") + body1
        sock.sendall(req1)

        _ver, status, _msg, hdrs, resp = reader.read_response()
        assert status == 503
        assert "close" not in hdrs.get("connection", "")

        # Now release semaphore permit
        sem.release()

        # Request 2 on the SAME socket succeeds with 200
        body2 = b'{"id":"req-ok","code":"result = 2","timeout_ms":1000}'
        req2 = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body2)}\r\n"
            f"\r\n"
        ).encode("latin1") + body2
        sock.sendall(req2)

        _ver2, status2, _msg2, _hdrs2, resp2 = reader.read_response()
        assert status2 == 200
        assert json.loads(resp2.decode("utf-8")).get("id") == "req-ok"

        reader.close()
    finally:
        sem.release()
        sock.close()
        _stop_server(server, t)


def test_connection_close_log_message(caplog: pytest.LogCaptureFixture) -> None:
    """Verify info log line on real connection close."""
    server, port, t = _start_server(max_threads=2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with caplog.at_level(logging.DEBUG, logger="compute_service"):
            sock.connect(("127.0.0.1", port))
            reader = ConnectionReader(sock)

            body = b'{"id":"req-log","code":"result = 1"}'
            req = (
                f"POST /v1/execute HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            ).encode("latin1") + body
            sock.sendall(req)

            _ver, status, _msg, _hdrs, _resp = reader.read_response()
            assert status == 200
            sock.close()
            time.sleep(0.05)

        # Check logs for close message
        close_logs = [rec.message for rec in caplog.records if "Closed connection from" in rec.message]
        assert len(close_logs) >= 1
        assert "after 1 request(s)" in close_logs[0]
        assert "reason=close" in close_logs[0]
    finally:
        _stop_server(server, t)


def test_accept_times_restamped_per_request() -> None:
    """Restamping _accept_times per request prevents false QUEUE_TIMEOUT on later calls."""
    server, port, t = _start_server(max_threads=2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        reader = ConnectionReader(sock)

        # First request
        body1 = b'{"id":"req-1","code":"result = 1"}'
        req1 = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body1)}\r\n"
            f"\r\n"
        ).encode("latin1") + body1
        sock.sendall(req1)
        _ver, status, _msg, _hdrs, _resp = reader.read_response()
        assert status == 200

        # Sleep 0.25 seconds between requests
        time.sleep(0.25)

        # Second request with timeout_ms=100.
        # If accept_time were NOT restamped, 250ms elapsed would exceed 100ms and cause QUEUE_TIMEOUT!
        body2 = b'{"id":"req-2","code":"result = 2","timeout_ms":100}'
        req2 = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body2)}\r\n"
            f"\r\n"
        ).encode("latin1") + body2
        sock.sendall(req2)
        _ver, status2, _msg, _hdrs, resp2 = reader.read_response()
        assert status2 == 200
        assert json.loads(resp2.decode("utf-8")).get("id") == "req-2"

        reader.close()
    finally:
        sock.close()
        _stop_server(server, t)


def test_auth_401_with_undrained_body_closes_connection() -> None:
    """401 with declared body above 1 MiB drain sends Connection: close and closes."""
    server, port, t = _start_server(max_threads=2, settings_override={"api_key": "correct-key"})
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        reader = ConnectionReader(sock)

        # Declared length is 2 MB (above 1 MB drain cap)
        req = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: 2097152\r\n"
            f"\r\n"
        ).encode("latin1")
        sock.sendall(req)

        _ver, status, _msg, hdrs, _resp = reader.read_response()
        assert status == 401
        assert hdrs.get("connection") == "close"

        sock.settimeout(1.0)
        assert sock.recv(1024) == b""  # EOF
        reader.close()
    finally:
        sock.close()
        _stop_server(server, t)


def test_keyless_cors_403_with_body_closes_connection() -> None:
    """Keyless 403 with declared body sends Connection: close and closes."""
    server, port, t = _start_server(max_threads=2, settings_override={"api_key": ""})
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        reader = ConnectionReader(sock)

        body = b'{"code":"result = 1"}'
        req = (
            f"POST /v1/execute HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Origin: http://malicious.example.com\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode("latin1") + body
        sock.sendall(req)

        _ver, status, _msg, hdrs, _resp = reader.read_response()
        assert status == 403
        assert hdrs.get("connection") == "close"

        sock.settimeout(1.0)
        assert sock.recv(1024) == b""  # EOF
        reader.close()
    finally:
        sock.close()
        _stop_server(server, t)


def test_selector_register_oserror_closes_connection() -> None:
    """If selector.register raises OSError, that connection is closed with reason=error."""
    import errno
    from unittest.mock import MagicMock
    from compute_service.http_server import ConnState

    server, port, t = _start_server(max_threads=2)
    s1, s2 = socket.socketpair()
    try:
        rfile = s1.makefile("rb", -1)
        state = ConnState(sock=s1, client_address=("127.0.0.1", 12345), rfile=rfile, disposition="park")
        server._conn_states[s1] = state
        server._park_queue.put(state)

        mock_selector = MagicMock()
        mock_selector.register.side_effect = OSError(errno.EMFILE, "Too many open files")

        server._park_service_loop(mock_selector)

        # Connection should have been closed and popped
        assert s1 not in server._conn_states
        assert s1 not in server._parked
        assert state.close_reason == "error"
    finally:
        s1.close()
        s2.close()
        _stop_server(server, t)


def test_readable_beats_expired_in_same_turn() -> None:
    """When a parked connection is both expired and readable, the request is served."""
    server, port, t = _start_server(max_threads=2)
    s1, s2 = socket.socketpair()
    try:
        from unittest.mock import MagicMock
        from compute_service.http_server import ConnState

        rfile = s1.makefile("rb", -1)
        # finished_at is 100 seconds ago -> clearly expired
        state = ConnState(sock=s1, client_address=("127.0.0.1", 12345), rfile=rfile, finished_at=time.monotonic() - 100.0, disposition="park")
        server._conn_states[s1] = state
        server._parked[s1] = state

        mock_selector = MagicMock()
        # When readable event arrives on accept thread:
        server._park_on_readable(state, s1, mock_selector)

        # After readable processing, socket was unregistered and popped from _parked:
        assert s1 not in server._parked

        # If idle sweep runs now in the same turn:
        server._park_service_loop(mock_selector)

        # state.close_reason should NOT be idle because readable won!
        assert state.close_reason != "idle"
    finally:
        s1.close()
        s2.close()
        _stop_server(server, t)


def test_queue_full_on_resubmit_closes_connection() -> None:
    """When accept queue is saturated on resubmit, socket closes with reason=queue_full."""
    server, port, t = _start_server(max_threads=1)
    s1, s2 = socket.socketpair()
    try:
        from compute_service.http_server import ConnState

        rfile = s1.makefile("rb", -1)
        state = ConnState(sock=s1, client_address=("127.0.0.1", 12345), rfile=rfile, disposition="ready_now")
        server._conn_states[s1] = state

        # Saturate pending count past max_workers + max_queued
        with server.executor._lock:
            server.executor._pending = server.executor._max_workers + server.executor._max_queued + 10

        # process_request on ready_now
        server.process_request(s1, state.client_address)

        assert state.close_reason == "queue_full"
        assert s1 not in server._conn_states
    finally:
        s1.close()
        s2.close()
        _stop_server(server, t)

