from unittest.mock import MagicMock, patch
from plugin.framework.queue_executor import _current_send_cancellation, SendCancellation
from plugin.mcp.mcp_protocol import MCPProtocolHandler

def test_mcp_protocol_threads_send_cancellation():
    services_mock = MagicMock()
    doc_mock = MagicMock()
    services_mock.document.resolve_document_by_url.return_value = (doc_mock, "writer")

    protocol = MCPProtocolHandler(services_mock)

    tool_registry_mock = MagicMock()
    tool_mock = MagicMock()
    tool_registry_mock.get.return_value = tool_mock
    protocol.tool_registry = tool_registry_mock

    cancellation = SendCancellation()
    token = _current_send_cancellation.set(cancellation)

    try:
        cancellation._cancelled.set()

        prepared = protocol._prepare_mcp_execution("some_tool", {}, None)
        assert prepared.context.send_cancellation is cancellation
        assert prepared.context.stop_checker() is True
    finally:
        _current_send_cancellation.reset(token)

def test_mcp_protocol_cancelled_requests_in_batch():
    services_mock = MagicMock()
    protocol = MCPProtocolHandler(services_mock)

    # Send a batch with a tool call and a cancellation notification
    batch_request = [
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "123"}},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "456"}}
    ]

    for item in batch_request:
        protocol._process_jsonrpc(item)

    assert "123" in protocol._cancelled_requests
    assert "456" in protocol._cancelled_requests

def test_mcp_protocol_cancelled_requests_handle_mcp():
    services_mock = MagicMock()
    protocol = MCPProtocolHandler(services_mock)

    # Mocking handler properly to pass validation checks
    handler_mock = MagicMock()
    handler_mock.headers = {}

    batch_request = [
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "789"}},
        {"jsonrpc": "2.0", "method": "ping", "id": 1}
    ]

    # Need to skip _reject_stale_session check
    with patch("plugin.mcp.mcp_protocol._reject_stale_session", return_value=False):
        protocol._handle_mcp(batch_request, handler_mock)

    assert "789" in protocol._cancelled_requests
