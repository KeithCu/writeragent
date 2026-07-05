#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Resilient stdio<->HTTP MCP bridge for WriterAgent (R7 — plug-and-play connect).

WHY: WriterAgent's MCP server is an HTTP server that lives INSIDE LibreOffice
(http://localhost:8765/mcp). An MCP client configured to talk to that URL directly
(``{"type":"http","url":"..."}``) tries to connect once at startup; if LibreOffice
is not running yet, the connection fails and the user must restart the client to
reconnect ("I have to close and reopen to connect").

A stdio MCP server, by contrast, is *spawned by the client* — so it is always present
the moment the client starts. This bridge is that stdio server: the client launches it,
and it forwards every request to LibreOffice over HTTP, **waiting/retrying when
LibreOffice is not up yet**. So it no longer matters who starts first — the connection
establishes on its own, with no restart. Configure it once (no per-use fiddling):

    "writer-agent": { "command": "python3", "args": ["/abs/path/scripts/mcp_bridge.py"] }

Override the target with WRITERAGENT_MCP_URL. Pure stdlib so the client's Python can run it.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request

MCP_URL = os.environ.get("WRITERAGENT_MCP_URL", "http://localhost:8765/mcp")
# Keep default in sync with plugin/mcp/wire_types.py MCP_PROTOCOL_VERSION.
PROTOCOL_VERSION = os.environ.get("WRITERAGENT_MCP_PROTOCOL", "2025-11-25")
_HEALTH_URL = MCP_URL.rsplit("/mcp", 1)[0] + "/health"
_POLL_SECONDS = 3.0
_HTTP_TIMEOUT = 30.0
# The manual (instructions) is delivered only once, at initialize. If LibreOffice is still coming
# up, briefly wait for it so the client gets the REAL manual instead of the placeholder (it won't
# re-initialize later). Bounded so a never-present LO doesn't hang the client's handshake.
_INIT_ATTEMPTS = 5
_INIT_RETRY_DELAY = 1.5

_stdout_lock = threading.Lock()


def parse_response(raw: str) -> dict:
    """Parse an MCP HTTP response body — either plain JSON or an SSE ``data:`` stream.

    Returns the LAST JSON object found (streamable-HTTP may send several SSE events; the final
    one carries the result). Raises ValueError if nothing parses."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty response")
    if raw[:1] in "{[":
        return json.loads(raw)
    last = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload and payload != "[DONE]":
                try:
                    last = json.loads(payload)
                except json.JSONDecodeError:
                    continue
    if last is None:
        raise ValueError("no JSON object in response")
    return last


def post_to_lo(body: dict, timeout: float = _HTTP_TIMEOUT) -> dict:
    """POST one JSON-RPC message to the LibreOffice MCP server and return the parsed envelope."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        MCP_URL,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return parse_response(resp.read().decode("utf-8"))


def _local_initialize_result(req_id) -> dict:
    """A valid initialize result used when LibreOffice is not reachable yet, so the client's
    handshake still succeeds. Advertises listChanged so the client refreshes tools once LO appears."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "WriterAgent MCP (bridge)", "version": "bridge"},
            "instructions": (
                "WriterAgent bridge: LibreOffice is not reachable yet at %s. Open LibreOffice with the "
                "WriterAgent extension; tools will appear automatically (no restart needed)." % MCP_URL
            ),
        },
    }


def _error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_request(msg: dict, poster=post_to_lo) -> dict | None:
    """Route one client->bridge JSON-RPC message. Returns the envelope to send back, or None for
    notifications (no response). ``poster`` does the HTTP call (injected in tests).

    Resilience: when LibreOffice is down, ``initialize`` still succeeds (local result), ``tools/list``
    returns an empty list (not an error, so the client keeps the connection), and other calls return a
    clear error the model can act on — instead of the whole connection failing."""
    method = msg.get("method")
    req_id = msg.get("id")
    is_notification = "id" not in msg

    if is_notification:
        # Client notifications (e.g. notifications/initialized) need no response and nothing
        # actionable downstream; forward best-effort and never raise.
        if method and method != "notifications/initialized":
            try:
                poster(msg)
            except Exception:
                pass
        return None

    if method == "initialize":
        # The manual (instructions) is delivered ONCE here, so it's worth briefly waiting for
        # LibreOffice if it's coming up — otherwise the client connects with the placeholder manual
        # and never gets the real one this session. Retry a few times before falling back.
        import time

        for attempt in range(_INIT_ATTEMPTS):
            try:
                return poster(msg)
            except Exception:
                if attempt < _INIT_ATTEMPTS - 1:
                    time.sleep(_INIT_RETRY_DELAY)
        return _local_initialize_result(req_id)

    try:
        return poster(msg)
    except Exception as e:
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": []}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        return _error(req_id, -32001, "WriterAgent (LibreOffice) is not reachable at %s: %s" % (MCP_URL, e))


def _write(obj: dict) -> None:
    with _stdout_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def _lo_reachable() -> bool:
    try:
        with urllib.request.urlopen(_HEALTH_URL, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _watch_lo(stop: threading.Event) -> None:
    """Emit notifications/tools/list_changed when LibreOffice transitions down->up, so a client that
    connected while LO was down refreshes its (then-empty) tool list automatically."""
    was_up = _lo_reachable()
    while not stop.is_set():
        stop.wait(_POLL_SECONDS)
        if stop.is_set():
            break
        up = _lo_reachable()
        if up and not was_up:
            _write({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        was_up = up


def main() -> int:
    stop = threading.Event()
    watcher = threading.Thread(target=_watch_lo, args=(stop,), daemon=True)
    watcher.start()
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                reply = handle_request(msg)
            except Exception as e:  # never let one message kill the bridge
                reply = _error(msg.get("id"), -32603, "bridge error: %s" % e)
            if reply is not None:
                _write(reply)
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
