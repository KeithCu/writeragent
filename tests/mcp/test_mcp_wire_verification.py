# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis verification for MCP wire_types JSON-RPC 2.0 message parsing."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.mcp.wire_types import (
    ParsedJsonRpcRequest,
    JsonRpcParseError,
    parse_jsonrpc_request,
    is_jsonrpc_notification,
    initialize_result,
    call_tool_result_image,
)


def test_parse_jsonrpc_request_valid() -> None:
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    res = parse_jsonrpc_request(msg)
    assert isinstance(res, ParsedJsonRpcRequest)
    assert res.method == "tools/list"
    assert res.req_id == 1


def test_parse_jsonrpc_request_invalid() -> None:
    res = parse_jsonrpc_request({"invalid": True})
    assert isinstance(res, JsonRpcParseError)
    assert res.code == -32600


@given(req_id=st.one_of(st.integers(), st.text(min_size=1, max_size=10)))
@settings(max_examples=50)
def test_parse_jsonrpc_request_with_ids(req_id: int | str) -> None:
    msg = {"jsonrpc": "2.0", "id": req_id, "method": "ping"}
    res = parse_jsonrpc_request(msg)
    assert isinstance(res, ParsedJsonRpcRequest)
    assert res.req_id == req_id


def test_is_jsonrpc_notification() -> None:
    # Notification has no 'id' field
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert is_jsonrpc_notification(notif) is True

    req = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert is_jsonrpc_notification(req) is False


def test_initialize_result_structure() -> None:
    init_res = initialize_result(
        protocol_version="2025-11-25",
        server_version="1.0.0",
        instructions="Test instructions",
    )
    assert isinstance(init_res, dict)
    assert init_res["protocolVersion"] == "2025-11-25"
    assert "capabilities" in init_res


def test_call_tool_result_image_structure() -> None:
    img_res = call_tool_result_image("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    assert isinstance(img_res, dict)
    assert "content" in img_res
    assert img_res["content"][0]["type"] == "image"
