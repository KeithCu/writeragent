from unittest.mock import MagicMock, patch
from plugin.mcp.mcp_protocol import MCPProtocolHandler
from plugin.mcp.wire_types import INVALID_REQUEST
import plugin.mcp.mcp_protocol as proto

def test_empty_batch_returns_400():
    services = MagicMock()
    handler = MCPProtocolHandler(services)
    mock_handler = MagicMock()
    mock_handler.headers = {}

    handler._send_json = MagicMock()
    handler._handle_mcp([], mock_handler)

    handler._send_json.assert_called_with(mock_handler, 400, {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "Empty batch"}})

def test_batch_initialize_mints_session_id():
    services = MagicMock()
    handler = MCPProtocolHandler(services)
    mock_handler = MagicMock()
    mock_handler.headers = {}

    msg = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}]

    with patch.object(proto, "_mint_session_id_once") as mock_mint:
        handler._send_json = MagicMock()
        # mock _process_jsonrpc to return 200
        handler._process_jsonrpc = MagicMock(return_value=(200, {"jsonrpc": "2.0", "id": 1, "result": {}}))

        handler._handle_mcp(msg, mock_handler)

        mock_mint.assert_called_once()
        handler._send_json.assert_called_with(mock_handler, 200, [{"jsonrpc": "2.0", "id": 1, "result": {}}])
