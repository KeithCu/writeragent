# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import json

import pytest

from plugin.mcp import wire_types


def test_initialize_result_required_keys():
    result = wire_types.initialize_result(
        protocol_version=wire_types.MCP_PROTOCOL_VERSION,
        client_protocol_version="2024-11-05",
        server_version="1.0.0",
        instructions="test instructions",
    )
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert result["serverInfo"]["name"] == "WriterAgent MCP"
    assert result["serverInfo"]["version"] == "1.0.0"
    assert result["instructions"] == "test instructions"


def test_call_tool_result_text_content():
    payload = wire_types.call_tool_result('{"status": "ok"}', is_error=False)
    assert payload["content"][0]["type"] == "text"
    assert payload["content"][0]["text"] == '{"status": "ok"}'
    assert "isError" not in payload

    error_payload = wire_types.call_tool_result("failed", is_error=True)
    assert error_payload["isError"] is True


def test_call_tool_result_json_serializable():
    text = json.dumps({"n": 1}, ensure_ascii=False, default=str)
    payload = wire_types.call_tool_result(text)
    json.dumps(payload, ensure_ascii=False, default=str)


def test_call_tool_result_image_content():
    # get_image returns a native MCP image block (not base64-as-text) so vision clients see the picture.
    payload = wire_types.call_tool_result_image("QUJD", mime_type="image/png")
    block = payload["content"][0]
    assert block["type"] == "image"
    assert block["data"] == "QUJD"
    assert block["mimeType"] == "image/png"
    assert "isError" not in payload
    assert wire_types.call_tool_result_image("x", is_error=True)["isError"] is True


def test_parse_jsonrpc_request_accepts_valid_request():
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    parsed = wire_types.parse_jsonrpc_request(msg)
    assert isinstance(parsed, wire_types.ParsedJsonRpcRequest)
    assert parsed.method == "tools/list"
    assert parsed.params == {}
    assert parsed.req_id == 1


def test_parse_jsonrpc_request_preserves_extra_params():
    msg = {"jsonrpc": "2.0", "id": "abc", "method": "initialize", "params": {"protocolVersion": "2024-11-05", "extra": True}}
    parsed = wire_types.parse_jsonrpc_request(msg)
    assert isinstance(parsed, wire_types.ParsedJsonRpcRequest)
    assert parsed.params["extra"] is True


@pytest.mark.parametrize(
    "msg",
    [
        "not a dict",
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "method": ""},
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []},
    ],
)
def test_parse_jsonrpc_request_rejects_invalid(msg):
    parsed = wire_types.parse_jsonrpc_request(msg)
    assert isinstance(parsed, wire_types.JsonRpcParseError)


def test_is_jsonrpc_notification():
    assert wire_types.is_jsonrpc_notification({"jsonrpc": "2.0", "method": "notifications/initialized"}) is True
    assert wire_types.is_jsonrpc_notification({"jsonrpc": "2.0", "method": "ping", "id": None}) is True
    assert wire_types.is_jsonrpc_notification({"jsonrpc": "2.0", "method": "ping", "id": 1}) is False


def test_jsonrpc_failure_preserves_data_and_codes():
    err = wire_types.jsonrpc_failure(9, wire_types.METHOD_NOT_FOUND, "Unknown method: foo")
    assert err["error"]["code"] == wire_types.METHOD_NOT_FOUND
    assert err["id"] == 9

    busy = wire_types.jsonrpc_failure(2, wire_types.SERVER_BUSY, "busy", {"retryable": True})
    assert busy["error"]["data"]["retryable"] is True


def test_jsonrpc_success_tools_list_golden_envelope():
    tools = [{"name": "test_tool", "description": "A test tool", "inputSchema": {"type": "object", "properties": {}}}]
    envelope = wire_types.jsonrpc_success(1, wire_types.list_tools_result(tools))
    assert envelope["jsonrpc"] == "2.0"
    assert envelope["id"] == 1
    assert envelope["result"]["tools"][0]["name"] == "test_tool"


def test_call_tool_request_params_from_params():
    params = wire_types.CallToolRequestParams.from_params({"name": "get_document_content", "arguments": {"scope": "full"}})
    assert params.name == "get_document_content"
    assert params.arguments == {"scope": "full"}


def test_call_tool_request_params_missing_name():
    with pytest.raises(ValueError, match="params.name"):
        wire_types.CallToolRequestParams.from_params({})


def test_progress_notification_shape():
    note = wire_types.progress_notification(progress_token="tok-1", progress=50.0, total=100.0, message="halfway")
    assert note["method"] == "notifications/progress"
    assert note["params"]["progressToken"] == "tok-1"
    assert note["params"]["progress"] == 50.0
    assert note["params"]["total"] == 100.0
    assert note["params"]["message"] == "halfway"
    assert "id" not in note
