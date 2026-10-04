import pytest
import threading
from unittest.mock import MagicMock
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
