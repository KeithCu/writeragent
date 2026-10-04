# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""MCPACPProxy must not crash the turn on a non-JSON MCP body."""

import json
from unittest.mock import MagicMock, patch

import requests

from plugin.acp.mcp_acp_proxy import MCPACPProxy


def _proxy() -> MCPACPProxy:
    with patch("plugin.framework.config.get_config", return_value="http://127.0.0.1:9/mcp"):
        return MCPACPProxy()


def _response(body: str, *, json_result: object = None, json_error: BaseException | None = None) -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.text = body
    if json_error is not None:
        response.json.side_effect = json_error
    else:
        response.json.return_value = json_result
    return response


class TestCallMcpJsonBody:
    def test_non_json_200_returns_error_dict(self):
        proxy = _proxy()
        body = "<html>not json</html>"
        response = _response(body, json_error=json.JSONDecodeError("Expecting value", body, 0))
        with patch("plugin.acp.mcp_acp_proxy.requests.post", return_value=response):
            result = proxy._call_mcp("tools/list")
        assert result["error"]["code"] == -32000
        assert "non-JSON" in result["error"]["message"]

    def test_json_null_returns_error_dict(self):
        proxy = _proxy()
        response = _response("null", json_result=None)
        with patch("plugin.acp.mcp_acp_proxy.requests.post", return_value=response):
            result = proxy._call_mcp("tools/list")
        assert result["error"]["code"] == -32000

    def test_json_object_is_returned(self):
        proxy = _proxy()
        payload = {"result": {"tools": []}}
        response = _response(json.dumps(payload), json_result=payload)
        with patch("plugin.acp.mcp_acp_proxy.requests.post", return_value=response):
            assert proxy._call_mcp("tools/list") == payload

    def test_transport_error_still_returns_error_dict(self):
        proxy = _proxy()
        with patch("plugin.acp.mcp_acp_proxy.requests.post", side_effect=requests.exceptions.ConnectionError("down")):
            result = proxy._call_mcp("tools/list")
        assert result["error"]["code"] == -32000
        assert "down" in result["error"]["message"]

    def test_prompt_non_json_tool_result_does_not_raise(self):
        proxy = _proxy()
        body = "<html>not json</html>"
        response = _response(body, json_error=json.JSONDecodeError("Expecting value", body, 0))
        blocks = [{"type": "tool_call", "tool_name": "read", "arguments": {}, "tool_call_id": "c1"}]
        with patch("plugin.acp.mcp_acp_proxy.requests.post", return_value=response):
            out = proxy.prompt("sess", blocks)
        assert out["content_blocks"][0]["type"] == "tool_result"
        assert out["content_blocks"][0]["content"][0]["text"].startswith("Error:")
