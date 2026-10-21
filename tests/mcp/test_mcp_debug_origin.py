from unittest.mock import MagicMock
from plugin.mcp.mcp_protocol import MCPProtocolHandler

def test_debug_info_rejects_origin():
    services = MagicMock()
    handler = MCPProtocolHandler(services)

    mock_handler = MagicMock()
    mock_handler.client_address = ["127.0.0.1"]
    mock_handler.headers = {"Origin": "http://evil.com", "Content-Type": "application/json", "X-WriterAgent-Debug-Token": handler.debug_token}

    handler._send_json = MagicMock()
    handler.handle_debug_info(mock_handler)

    handler._send_json.assert_called_with(mock_handler, 403, {"error": "Forbidden: Origin header not allowed on /debug"})

def test_debug_post_rejects_origin():
    services = MagicMock()
    handler = MCPProtocolHandler(services)

    mock_handler = MagicMock()
    mock_handler.client_address = ["127.0.0.1"]
    mock_handler.headers = {"Origin": "http://evil.com", "Content-Type": "application/json", "X-WriterAgent-Debug-Token": handler.debug_token}

    handler._send_json = MagicMock()
    handler.handle_debug_post(mock_handler)

    handler._send_json.assert_called_with(mock_handler, 403, {"error": "Forbidden: Origin header not allowed on /debug"})

