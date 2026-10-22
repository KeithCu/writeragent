# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import json
import urllib.error
import urllib.request

import pytest

import plugin.mcp.mcp_session as mcp_session
from plugin.mcp.wire_types import INVALID_REQUEST, MCP_PROTOCOL_VERSION


def _initialize_payload(req_id=1):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "initialize",
        "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
    }


def _post_mcp(mcp_server, payload, *, session_id=None, path="/mcp"):
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{mcp_server}{path}", method="POST", data=data_bytes)
    req.add_header("Content-Type", "application/json")
    if session_id is not None:
        req.add_header("Mcp-Session-Id", session_id)
    return urllib.request.urlopen(req, timeout=5)


def _read_http_error(err):
    try:
        return err.read() or b""
    except (ConnectionResetError, BrokenPipeError, TimeoutError, OSError):
        return b""


def test_delete_mcp_is_405_and_does_not_teardown_session(mcp_server):
    req = urllib.request.Request(f"{mcp_server}/mcp", method="DELETE")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    err = exc_info.value
    assert err.code == 405
    allow = err.headers.get("Allow", "")
    assert "GET" in allow
    assert "POST" in allow
    assert _read_http_error(err) == b""

    with _post_mcp(mcp_server, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) as response:
        assert response.status == 200
        body = json.loads(response.read().decode("utf-8"))
        assert "result" in body


def test_stale_session_id_without_initialize_is_404(mcp_server):
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _post_mcp(mcp_server, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, session_id="deadbeef")
    err = exc_info.value
    assert err.code == 404
    raw = _read_http_error(err)
    assert raw
    body = json.loads(raw.decode("utf-8"))
    assert body["error"]["code"] == INVALID_REQUEST
    assert "initialize" in body["error"]["message"].lower()


def test_initialize_with_stale_id_returns_200_and_session_header(mcp_server):
    with _post_mcp(mcp_server, _initialize_payload(), session_id="deadbeef") as response:
        assert response.status == 200
        session_id = response.headers.get("Mcp-Session-Id")
        assert session_id
        assert session_id != "deadbeef"


def test_second_initialize_keeps_the_same_session_id(mcp_server):
    with _post_mcp(mcp_server, _initialize_payload(10)) as first:
        assert first.status == 200
        first_id = first.headers.get("Mcp-Session-Id")
        first.read()
    with _post_mcp(mcp_server, _initialize_payload(11)) as second:
        assert second.status == 200
        assert second.headers.get("Mcp-Session-Id") == first_id


def test_post_without_session_id_still_works(mcp_server):
    with _post_mcp(mcp_server, {"jsonrpc": "2.0", "id": 2, "method": "ping"}) as response:
        assert response.status == 200
        body = json.loads(response.read().decode("utf-8"))
        assert body.get("result") == {}


def test_restart_makes_old_session_id_404(mcp_server):
    previous = mcp_session._mcp_session_id
    try:
        with _post_mcp(mcp_server, _initialize_payload(20)) as first:
            old_id = first.headers.get("Mcp-Session-Id")
            first.read()
        assert old_id
        mcp_session._mcp_session_id = None
        with _post_mcp(mcp_server, _initialize_payload(21)) as second:
            new_id = second.headers.get("Mcp-Session-Id")
            second.read()
        assert new_id
        assert new_id != old_id
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post_mcp(mcp_server, {"jsonrpc": "2.0", "id": 22, "method": "tools/list"}, session_id=old_id)
        assert exc_info.value.code == 404
    finally:
        if mcp_session._mcp_session_id is None:
            mcp_session._mcp_session_id = previous


def test_stale_session_id_on_get_sse_is_404(mcp_server):
    req = urllib.request.Request(f"{mcp_server}/mcp", method="GET")
    req.add_header("Accept", "text/event-stream")
    req.add_header("Mcp-Session-Id", "deadbeef")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=5)
    assert exc_info.value.code == 404


def test_stale_batch_is_404_without_processing(mcp_server):
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _post_mcp(mcp_server, batch, session_id="deadbeef")
    assert exc_info.value.code == 404
