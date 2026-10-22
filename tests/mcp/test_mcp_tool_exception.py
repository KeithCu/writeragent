# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unexpected MCP tool exceptions must not become opaque INTERNAL_ERROR."""
import json
from unittest.mock import MagicMock, patch

from plugin.mcp.mcp_protocol import MCPProtocolHandler


def _handler():
    services = MagicMock()
    services.get.return_value = None
    services.tools.get.return_value = None
    return MCPProtocolHandler(services)


def test_unexpected_tool_exception_is_tool_execution_error_with_full_message():
    handler = _handler()
    boom = RuntimeError("max index out of range in table cell")
    with patch.object(handler, "_execute_with_backpressure", side_effect=boom):
        result = handler._mcp_tools_call(
            {"name": "apply_style", "arguments": {"style": "Heading 1"}},
            document_url=None,
        )
    assert result is not None
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["code"] == "TOOL_EXECUTION_ERROR"
    assert payload["message"] == "max index out of range in table cell"
    assert "INTERNAL_ERROR" not in payload["code"]


def test_writeragent_exception_is_tool_result_not_jsonrpc_internal():
    """tools/call ToolExecutionError must be HTTP 200 + isError, not HTTP 500."""
    from plugin.framework.errors import ToolExecutionError

    handler = _handler()
    boom = ToolExecutionError("max index out of range in table cell")
    with patch.object(handler, "_execute_with_backpressure", side_effect=boom):
        status, body = handler._process_jsonrpc({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "apply_style", "arguments": {"style": "Heading 1"}},
        })
    assert status == 200
    result = body["result"]
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["code"] == "TOOL_EXECUTION_ERROR"
    assert payload["message"] == "max index out of range in table cell"
    assert "error" not in body or body.get("error") is None


def _tools_call(handler):
    return handler._process_jsonrpc({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "apply_style", "arguments": {"style": "Heading 1"}},
    })


def test_busy_error_stays_http_429():
    from plugin.mcp.document_mutation_gate import BusyError
    from plugin.mcp import wire_types

    handler = _handler()
    with patch.object(handler, "_execute_with_backpressure", side_effect=BusyError("busy")):
        status, body = _tools_call(handler)
    assert status == 429
    assert body["error"]["code"] == wire_types.SERVER_BUSY
    assert body["error"].get("data", {}).get("retryable") is True


def test_timeout_error_stays_http_504():
    from plugin.mcp import wire_types

    handler = _handler()
    with patch.object(handler, "_execute_with_backpressure", side_effect=TimeoutError("slow")):
        status, body = _tools_call(handler)
    assert status == 504
    assert body["error"]["code"] == wire_types.EXECUTION_TIMEOUT


def test_writeragent_internal_error_code_is_remapped():
    """Default WriterAgentException.code is INTERNAL_ERROR; tools/call must not keep it."""
    from plugin.framework.errors import WriterAgentException

    handler = _handler()
    boom = WriterAgentException("max index out of range in table cell", code="INTERNAL_ERROR")
    with patch.object(handler, "_execute_with_backpressure", side_effect=boom):
        status, body = _tools_call(handler)
    assert status == 200
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["code"] == "TOOL_EXECUTION_ERROR"
    assert "INTERNAL_ERROR" not in payload["code"]
