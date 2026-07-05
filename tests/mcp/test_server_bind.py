# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""R7: the HTTP/MCP server must be resilient to a transiently/busy port instead of failing
silently on the first bind (the "I have to restart to connect" symptom). No LibreOffice required."""
import socket

import pytest

from plugin.mcp.server import HttpServer


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_bind_with_retry_succeeds_on_free_port():
    srv = HttpServer(route_registry=None, port=_free_port(), host="127.0.0.1")
    server = srv._bind_with_retry()
    try:
        assert server is not None
    finally:
        server.server_close()


def test_bind_with_retry_retries_then_raises_when_busy(monkeypatch):
    # Hold the port with an active listener so the bind keeps failing.
    occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupier.bind(("127.0.0.1", 0))
    occupier.listen(1)
    port = occupier.getsockname()[1]

    sleeps = {"n": 0}
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: sleeps.__setitem__("n", sleeps["n"] + 1))

    try:
        srv = HttpServer(route_registry=None, port=port, host="127.0.0.1")
        srv._BIND_ATTEMPTS = 3
        with pytest.raises(OSError):
            srv._bind_with_retry()
        # 3 attempts => 2 sleeps between them (it retried, not failed on the first try).
        assert sleeps["n"] == 2
    finally:
        occupier.close()


def test_bind_with_retry_recovers_when_port_frees_midway(monkeypatch):
    """If the holder releases the port between attempts, the next attempt should bind."""
    occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupier.bind(("127.0.0.1", 0))
    occupier.listen(1)
    port = occupier.getsockname()[1]

    # Free the port on the first retry sleep, so attempt #2 succeeds.
    def _free_on_sleep(*_a, **_k):
        occupier.close()

    monkeypatch.setattr("time.sleep", _free_on_sleep)

    srv = HttpServer(route_registry=None, port=port, host="127.0.0.1")
    srv._BIND_ATTEMPTS = 5
    server = srv._bind_with_retry()
    try:
        assert server is not None
    finally:
        server.server_close()
