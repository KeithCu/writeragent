from unittest.mock import MagicMock, patch

from plugin.framework.send_cancellation import SendCancellation
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
    cancellation.cancel()

    prepared = protocol._prepare_mcp_execution("some_tool", {}, None, cancel=cancellation)
    assert prepared.context.send_cancellation is cancellation
    assert prepared.context.send_cancellation.is_cancelled() is True


def test_mcp_protocol_drops_cancel_for_unknown_call():
    services_mock = MagicMock()
    protocol = MCPProtocolHandler(services_mock)

    item = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "123"}}
    protocol._process_jsonrpc(item)
    assert protocol._request_scopes == {}


def test_cancel_before_request_does_not_stick():
    services_mock = MagicMock()
    protocol = MCPProtocolHandler(services_mock)

    item = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "123"}}
    protocol._process_jsonrpc(item)
    assert protocol._request_scopes == {}

    with patch.object(protocol, "_run_tool", return_value={"status": "ok"}) as mock_run:
        with patch.object(protocol, "_tool_exposure_mode", return_value="direct_flat"):
            protocol._process_jsonrpc(
                {"jsonrpc": "2.0", "id": "123", "method": "tools/call", "params": {"name": "some_tool", "arguments": {}}},
            )
        assert mock_run.call_args.kwargs["cancel"].is_cancelled() is False
    assert protocol._request_scopes == {}


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
    scope_a = SendCancellation()
    scope_b = SendCancellation()
    protocol._request_scopes[(session_a, 1)] = [scope_a]
    protocol._request_scopes[(session_b, 1)] = [scope_b]

    cancel_item = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
    protocol._process_jsonrpc(cancel_item, session=session_a)

    assert scope_a.is_cancelled() is True
    assert scope_b.is_cancelled() is False

    cancel_item_2 = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}}
    protocol._process_jsonrpc(cancel_item_2, session=session_a)
    assert (session_b, 2) not in protocol._request_scopes


def test_mcp_notification_after_prepare_cancels_running_tool():
    services_mock = MagicMock()
    doc_mock = MagicMock()
    services_mock.document.resolve_document_by_url.return_value = (doc_mock, "writer")
    services_mock.document.detect_doc_type.return_value = "writer"

    protocol = MCPProtocolHandler(services_mock)
    tool_mock = MagicMock()
    tool_mock.is_async.return_value = False
    tool_mock.requires_document_lock.return_value = False
    protocol.tool_registry.get.return_value = tool_mock

    session = "client_session"
    req_id = 1
    scope = SendCancellation()
    protocol._request_scopes[(session, req_id)] = [scope]

    prepared = protocol._prepare_mcp_execution("tool", {}, req_id=req_id, session=session, cancel=scope)
    assert prepared.context.send_cancellation.is_cancelled() is False

    cancel_item = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": req_id}}
    protocol._process_jsonrpc(cancel_item, session=session)

    assert prepared.context.send_cancellation.is_cancelled() is True
    result = protocol._invoke_prepared_mcp_tool(prepared, "tool", {})
    assert result == {"status": "error", "code": "USER_STOPPED", "message": "Stopped by user"}


def test_cancel_after_store_before_prepare():
    services_mock = MagicMock()
    doc_mock = MagicMock()
    services_mock.document.resolve_document_by_url.return_value = (doc_mock, "writer")
    services_mock.document.detect_doc_type.return_value = "writer"

    protocol = MCPProtocolHandler(services_mock)
    tool_mock = MagicMock()
    tool_mock.is_async.return_value = False
    tool_mock.requires_document_lock.return_value = False
    protocol.tool_registry.get.return_value = tool_mock

    session = "sess"
    req_id = 7
    scope = SendCancellation()
    protocol._request_scopes[(session, req_id)] = [scope]

    cancel_item = {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": req_id},
    }
    protocol._process_jsonrpc(cancel_item, session=session)

    prepared = protocol._prepare_mcp_execution("tool", {}, req_id=req_id, session=session, cancel=scope)
    assert scope.is_cancelled() is True
    result = protocol._invoke_prepared_mcp_tool(prepared, "tool", {})
    assert result == {"status": "error", "code": "USER_STOPPED", "message": "Stopped by user"}


def test_overlapping_same_id_cancels_every_scope():
    services_mock = MagicMock()
    protocol = MCPProtocolHandler(services_mock)
    session = "sess"
    req_id = 1
    scope_a = SendCancellation()
    scope_b = SendCancellation()
    protocol._request_scopes[(session, req_id)] = [scope_a, scope_b]

    cancel_item = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": req_id}}
    protocol._process_jsonrpc(cancel_item, session=session)

    assert scope_a.is_cancelled() is True
    assert scope_b.is_cancelled() is True

    protocol._request_scopes[(session, req_id)].remove(scope_a)
    assert scope_b in protocol._request_scopes[(session, req_id)]


def test_mcp_protocol_async_tool_no_timeout_runs():
    services_mock = MagicMock()
    doc_mock = MagicMock()
    services_mock.document.resolve_document_by_url.return_value = (doc_mock, "writer")
    services_mock.document.detect_doc_type.return_value = "writer"

    protocol = MCPProtocolHandler(services_mock)
    tool_mock = MagicMock()
    tool_mock.is_async.return_value = True
    del tool_mock.timeout
    protocol.tool_registry.get.return_value = tool_mock

    prepared = protocol._prepare_mcp_execution("async_tool", {}, None, cancel=SendCancellation())
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

    prepared = protocol._prepare_mcp_execution("async_tool", {}, None, cancel=SendCancellation())
    assert isinstance(prepared, dict)
    assert prepared.get("status") == "error"
    assert prepared.get("code") == "TOOL_EXECUTION_ERROR"


def test_tools_call_uses_fresh_cancellation_under_agent_session():
    from plugin.framework.send_cancellation import SendCancellation, agent_session

    existing = SendCancellation()
    services_mock = MagicMock()
    protocol = MCPProtocolHandler(services_mock)

    with patch.object(protocol, "_run_tool", return_value={"status": "ok"}) as mock_run:
        with patch.object(protocol, "_tool_exposure_mode", return_value="direct_flat"):
            with agent_session(existing):
                protocol._process_jsonrpc(
                    {
                        "jsonrpc": "2.0",
                        "id": "ambient",
                        "method": "tools/call",
                        "params": {"name": "some_tool", "arguments": {}},
                    },
                )
        cancel = mock_run.call_args.kwargs["cancel"]
        assert cancel is not existing
        assert isinstance(cancel, SendCancellation)
        assert existing.is_cancelled() is False
