import pytest
from unittest.mock import MagicMock
from plugin.mcp.mcp_protocol import MCPProtocolHandler
import plugin.mcp.mcp_protocol as proto

def test_batch_jsonrpc_omits_mcp_session_id():
    services = MagicMock()
    handler = MCPProtocolHandler(services)

    # We patch _mcp_session_id to simulate an initialized session
    proto._mcp_session_id = "test-session-id"

    mock_http_handler = MagicMock()
    mock_http_handler.headers = {"Mcp-Session-Id": "test-session-id"}

    headers_sent = []
    mock_http_handler.send_header.side_effect = lambda k, v: headers_sent.append((k, v))

    msg = [
        {"jsonrpc": "2.0", "id": 1, "method": "test1"},
        {"jsonrpc": "2.0", "id": 2, "method": "test2"},
    ]

    # mock _process_jsonrpc
    handler._process_jsonrpc = MagicMock(return_value=(200, {"jsonrpc": "2.0", "id": 1, "result": {}}))

    handler._handle_mcp(msg, mock_http_handler)

    # Did it send the session ID?
    assert ("Mcp-Session-Id", "test-session-id") in headers_sent, f"Headers sent: {headers_sent}"

if __name__ == "__main__":
    pytest.main(["-v", "test_batch_headers.py"])
