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


def test_mcp_protocol_drops_cancel_for_unknown_call():
    services_mock = MagicMock()
    protocol = MCPProtocolHandler(services_mock)

    # Cancel notification for unknown or non-in-flight request is dropped
    item = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "123"}}
    protocol._process_jsonrpc(item)
    assert "123" not in protocol._cancelled_requests


def test_mcp_protocol_cancelled_in_flight_request():
    services_mock = MagicMock()
    protocol = MCPProtocolHandler(services_mock)

    # Mark request as in-flight
    protocol._in_flight_requests.add("123")

    item = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "123"}}
    protocol._process_jsonrpc(item)
    assert "123" in protocol._cancelled_requests


def test_mcp_protocol_reused_cancel_id_not_stopped():
    services_mock = MagicMock()
    doc_mock = MagicMock()
    services_mock.document.resolve_document_by_url.return_value = (doc_mock, "writer")
    services_mock.document.detect_doc_type.return_value = "writer"

    protocol = MCPProtocolHandler(services_mock)
    tool_mock = MagicMock()
    tool_mock.is_async.return_value = False
    protocol.tool_registry.get.return_value = tool_mock

    # Simulate an id that was cancelled in flight previously
    protocol._cancelled_requests.add("req-1")

    # A new call arrives with reused id "req-1"
    with patch.object(protocol, "_execute_with_backpressure", return_value={"status": "ok"}):
        res = protocol._mcp_tools_call({"name": "some_tool", "arguments": {}}, req_id="req-1")
        assert not res.get("isError")
        assert "req-1" not in protocol._cancelled_requests
        assert "req-1" not in protocol._in_flight_requests


def test_mcp_protocol_async_tool_no_timeout_runs():
    services_mock = MagicMock()
    doc_mock = MagicMock()
    services_mock.document.resolve_document_by_url.return_value = (doc_mock, "writer")
    services_mock.document.detect_doc_type.return_value = "writer"

    protocol = MCPProtocolHandler(services_mock)
    tool_mock = MagicMock()
    tool_mock.is_async.return_value = True
    # Tool did not declare timeout attribute
    del tool_mock.timeout
    protocol.tool_registry.get.return_value = tool_mock

    prepared = protocol._prepare_mcp_execution("async_tool", {}, None)
    assert hasattr(prepared, "context")


def test_mcp_protocol_async_tool_non_positive_timeout_errors():
    services_mock = MagicMock()
    doc_mock = MagicMock()
    services_mock.document.resolve_document_by_url.return_value = (doc_mock, "writer")
    services_mock.document.detect_doc_type.return_value = "writer"

    protocol = MCPProtocolHandler(services_mock)
    tool_mock = MagicMock()
    tool_mock.is_async.return_value = True
    tool_mock.timeout = 0
    protocol.tool_registry.get.return_value = tool_mock

    prepared = protocol._prepare_mcp_execution("async_tool", {}, None)
    assert isinstance(prepared, dict)
    assert prepared.get("status") == "error"
    assert prepared.get("code") == "TOOL_EXECUTION_ERROR"
