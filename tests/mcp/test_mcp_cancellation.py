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
    assert (None, "123") not in protocol._cancelled_requests


def test_mcp_protocol_cancelled_in_flight_request():
    services_mock = MagicMock()
    protocol = MCPProtocolHandler(services_mock)

    # Mark request as in-flight
    protocol._in_flight_requests[(None, "123")] = 1

    item = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "123"}}
    protocol._process_jsonrpc(item)
    assert (None, "123") in protocol._cancelled_requests


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
    protocol._cancelled_requests.add((None, "req-1"))

    # A new call arrives with reused id "req-1"
    with patch.object(protocol, "_execute_with_backpressure", return_value={"status": "ok"}):
        # Tools/call via _process_jsonrpc will now clear _cancelled_requests if in-flight was 0
        res = protocol._process_jsonrpc({"jsonrpc": "2.0", "id": "req-1", "method": "tools/call", "params": {"name": "some_tool", "arguments": {}}})
        res = res[1]["result"]
        assert not res.get("isError")
        assert (None, "req-1") not in protocol._cancelled_requests
        assert (None, "req-1") not in protocol._in_flight_requests


def test_cancellation_isolated_by_session():
    """Client A cancelling its request id=1 must not cancel Client B's in-flight request id=1."""
    services_mock = MagicMock()
    doc_mock = MagicMock()
    services_mock.document.resolve_document_by_url.return_value = (doc_mock, "writer")
    services_mock.document.detect_doc_type.return_value = "writer"

    protocol = MCPProtocolHandler(services_mock)
    tool_mock = MagicMock()
    protocol.tool_registry.get.return_value = tool_mock

    session_a = "client_session_A"
    session_b = "client_session_B"

    # Both clients have an in-flight request with id 1
    protocol._in_flight_requests[(session_a, 1)] = 1
    protocol._in_flight_requests[(session_b, 1)] = 1

    # Client A cancels its request 1
    cancel_item = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
    protocol._process_jsonrpc(cancel_item, session=session_a)

    # Client A's request is marked cancelled, Client B's is NOT
    assert (session_a, 1) in protocol._cancelled_requests
    assert (session_b, 1) not in protocol._cancelled_requests

    # Verify stop_checker isolation
    prepared_a = protocol._prepare_mcp_execution("tool", {}, req_id=1, session=session_a)
    assert hasattr(prepared_a, "context") and prepared_a.context.stop_checker() is True

    prepared_b = protocol._prepare_mcp_execution("tool", {}, req_id=1, session=session_b)
    assert hasattr(prepared_b, "context") and prepared_b.context.stop_checker() is False

    # Client A attempting to cancel Client B's request 2 (which Client A does not have) is dropped
    protocol._in_flight_requests[(session_b, 2)] = 1
    cancel_item_2 = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}}
    protocol._process_jsonrpc(cancel_item_2, session=session_a)
    assert (session_b, 2) not in protocol._cancelled_requests
    assert (session_a, 2) not in protocol._cancelled_requests


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
