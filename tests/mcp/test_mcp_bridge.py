# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""R7: the stdio<->HTTP MCP bridge must stay usable when LibreOffice is down (plug-and-play),
and pass requests through when it is up. No LibreOffice required."""
import importlib.util
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_BRIDGE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts", "mcp_bridge.py")
_spec = importlib.util.spec_from_file_location("wa_mcp_bridge", _BRIDGE_PATH)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


# ---- parse_response ----

def test_parse_response_plain_json():
    assert bridge.parse_response('{"jsonrpc":"2.0","id":1,"result":{"ok":true}}')["result"]["ok"] is True


def test_parse_response_sse_takes_last():
    raw = 'data: {"id":1,"result":"a"}\n\ndata: {"id":1,"result":"b"}\n'
    assert bridge.parse_response(raw)["result"] == "b"


def test_parse_response_empty_raises():
    with pytest.raises(ValueError):
        bridge.parse_response("")


# ---- handle_request: LibreOffice UP (poster returns an envelope) ----

def _up(msg):
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"echo": msg.get("method")}}


def test_passthrough_when_up():
    out = bridge.handle_request({"jsonrpc": "2.0", "id": 5, "method": "tools/list"}, poster=_up)
    assert out["result"]["echo"] == "tools/list"
    assert out["id"] == 5


# ---- handle_request: LibreOffice DOWN (poster raises) ----

def _down(msg):
    raise ConnectionRefusedError("LO not running")


def test_initialize_succeeds_locally_when_down(monkeypatch):
    monkeypatch.setattr(bridge, "_INIT_RETRY_DELAY", 0)  # don't actually sleep in the test
    out = bridge.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, poster=_down)
    assert "result" in out and "error" not in out
    assert out["result"]["capabilities"]["tools"]["listChanged"] is True
    assert out["result"]["protocolVersion"]


def test_initialize_waits_then_succeeds_if_lo_appears(monkeypatch):
    # LibreOffice comes up on the 3rd attempt -> the model should get the REAL manual, not the
    # placeholder, because initialize briefly waits for LO.
    monkeypatch.setattr(bridge, "_INIT_RETRY_DELAY", 0)
    calls = {"n": 0}

    def _flaky(msg):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionRefusedError("LO still starting")
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"instructions": "REAL MANUAL"}}

    out = bridge.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, poster=_flaky)
    assert out["result"]["instructions"] == "REAL MANUAL"
    assert calls["n"] == 3


def test_tools_list_empty_when_down_not_error():
    out = bridge.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, poster=_down)
    assert out["result"]["tools"] == []
    assert "error" not in out


def test_tools_call_returns_clear_error_when_down():
    out = bridge.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "x"}}, poster=_down)
    assert "error" in out
    assert "not reachable" in out["error"]["message"].lower()


def test_notification_returns_none():
    assert bridge.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}, poster=_down) is None


def test_ping_empty_when_down():
    out = bridge.handle_request({"jsonrpc": "2.0", "id": 9, "method": "ping"}, poster=_down)
    assert out["result"] == {} and "error" not in out


# ---- post_to_lo against a real local HTTP server (exercises the HTTP path + parse) ----

class _Echo(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        out = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": {"method": body.get("method")}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_post_to_lo_real_http(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Echo)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        monkeypatch.setattr(bridge, "MCP_URL", f"http://127.0.0.1:{port}/mcp")
        out = bridge.post_to_lo({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert out["result"]["method"] == "tools/list"
    finally:
        srv.shutdown()
