import pytest
from unittest.mock import MagicMock, patch
import json
from io import BytesIO

from plugin.modules.http.mcp_protocol import MCPProtocolHandler

class MockHandler:
    """Mock GenericRequestHandler for testing."""
    def __init__(self, headers, body=b""):
        self.headers = headers
        self.rfile = BytesIO(body)
        self.wfile = BytesIO()
        self.client_address = ("127.0.0.1", 12345)
        self.sent_responses = []
        self.sent_headers = []
        self.headers_ended = False

    def send_response(self, code, message=None):
        self.sent_responses.append(code)

    def send_header(self, keyword, value):
        self.sent_headers.append((keyword, value))

    def end_headers(self):
        self.headers_ended = True

def test_handle_mcp_post_routing():
    """Test that handle_mcp_post properly parses body and extracts X-Document-URL."""
    # Setup mock services
    services = MagicMock()
    mcp_protocol = MCPProtocolHandler(services)

    # We patch _handle_mcp to just record the call, to verify parameters
    with patch.object(mcp_protocol, '_handle_mcp') as mock_handle_mcp:
        body_data = {"jsonrpc": "2.0", "method": "test"}
        body_bytes = json.dumps(body_data).encode("utf-8")

        headers = {
            "Content-Length": str(len(body_bytes)),
            "X-Document-URL": "file:///test/doc.odt"
        }

        handler = MockHandler(headers, body_bytes)

        mcp_protocol.handle_mcp_post(handler)

        mock_handle_mcp.assert_called_once()
        args, kwargs = mock_handle_mcp.call_args

        # Verify body
        assert args[0] == body_data
        # Verify handler
        assert args[1] == handler
        # Verify X-Document-URL logic
        assert kwargs.get("document_url") == "file:///test/doc.odt"

def test_handle_mcp_post_no_doc_url():
    """Test handle_mcp_post with missing X-Document-URL header."""
    services = MagicMock()
    mcp_protocol = MCPProtocolHandler(services)

    with patch.object(mcp_protocol, '_handle_mcp') as mock_handle_mcp:
        body_bytes = b'{"jsonrpc": "2.0", "method": "test"}'
        headers = {"Content-Length": str(len(body_bytes))}
        handler = MockHandler(headers, body_bytes)

        mcp_protocol.handle_mcp_post(handler)

        args, kwargs = mock_handle_mcp.call_args
        assert kwargs.get("document_url") is None

def test_handle_mcp_post_invalid_json():
    """Test handle_mcp_post with invalid JSON body."""
    services = MagicMock()
    mcp_protocol = MCPProtocolHandler(services)

    with patch.object(mcp_protocol, '_handle_mcp') as mock_handle_mcp:
        body_bytes = b'{invalid_json}'
        headers = {"Content-Length": str(len(body_bytes))}
        handler = MockHandler(headers, body_bytes)

        mcp_protocol.handle_mcp_post(handler)

        # _handle_mcp should not be called if body is invalid
        mock_handle_mcp.assert_not_called()

        # Response should be 400 Bad Request
        assert 400 in handler.sent_responses

        response_data = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert response_data.get("status") == "error"
        assert response_data.get("code") == "PARSE_ERROR"

def test_send_cors_headers_allowed():
    """Test _send_cors_headers allows safe origins."""
    services = MagicMock()
    mcp_protocol = MCPProtocolHandler(services)

    safe_origins = [
        "http://localhost",
        "https://localhost",
        "http://localhost:3000",
        "https://localhost:8443",
        "http://127.0.0.1",
        "https://127.0.0.1",
        "http://127.0.0.1:8080",
        "http://[::1]",
        "http://[::1]:3000",
    ]

    for origin in safe_origins:
        handler = MockHandler({"Origin": origin})
        mcp_protocol._send_cors_headers(handler)

        headers_dict = dict(handler.sent_headers)
        assert "Access-Control-Allow-Origin" in headers_dict
        assert headers_dict["Access-Control-Allow-Origin"] == origin

def test_send_cors_headers_rejected():
    """Test _send_cors_headers rejects unsafe origins."""
    services = MagicMock()
    mcp_protocol = MCPProtocolHandler(services)

    unsafe_origins = [
        "http://localhost.attacker.com",
        "https://127.0.0.1.badguy.com",
        "http://example.com",
        "https://[::1].evil.net",
    ]

    for origin in unsafe_origins:
        handler = MockHandler({"Origin": origin})
        mcp_protocol._send_cors_headers(handler)

        headers_dict = dict(handler.sent_headers)
        assert "Access-Control-Allow-Origin" not in headers_dict

def test_handle_mcp_post_missing_content_length():
    """Test missing Content-Length header returns a structured JSON-RPC error."""
    services = MagicMock()
    mcp_protocol = MCPProtocolHandler(services)

    # Missing Content-Length means _read_body returns {}
    # and _handle_mcp processes {} which results in a 400 JSON-RPC error.
    body_bytes = b'{"jsonrpc": "2.0", "method": "test"}'
    headers = {}  # No Content-Length
    handler = MockHandler(headers, body_bytes)

    mcp_protocol.handle_mcp_post(handler)

    assert 400 in handler.sent_responses
    response_data = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert response_data.get("jsonrpc") == "2.0"
    assert response_data.get("id") is None
    assert "error" in response_data
    assert response_data["error"].get("code") == -32600
    assert "Invalid JSON-RPC" in response_data["error"].get("message", "")

def test_handle_mcp_post_truncated_json():
    """Test when Content-Length is larger than body (truncated JSON).
    Should hit invalid-json path and not call _handle_mcp."""
    services = MagicMock()
    mcp_protocol = MCPProtocolHandler(services)

    with patch.object(mcp_protocol, '_handle_mcp') as mock_handle_mcp:
        # A valid json but we say it's much longer than it is.
        # Wait, if we use a valid json but rfile.read returns it, it might still parse valid!
        # Let's provide an actual truncated json.
        body_bytes = b'{"jsonrpc": "2.0", "method":'
        headers = {"Content-Length": "100"}  # Claiming it's 100 bytes long
        handler = MockHandler(headers, body_bytes)

        mcp_protocol.handle_mcp_post(handler)

        mock_handle_mcp.assert_not_called()
        assert 400 in handler.sent_responses

        response_data = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert response_data.get("status") == "error"
        assert response_data.get("code") == "PARSE_ERROR"

def test_handle_mcp_invalid_json_rpc():
    """Test when JSON-RPC method format is unknown or invalid.
    Ensure invalid JSON-RPC method formats return the expected error shape."""
    services = MagicMock()
    mcp_protocol = MCPProtocolHandler(services)

    # Valid JSON but unknown method
    body_data = {"jsonrpc": "2.0", "method": "invalid/method", "id": 1}
    body_bytes = json.dumps(body_data).encode("utf-8")
    headers = {"Content-Length": str(len(body_bytes))}
    handler = MockHandler(headers, body_bytes)

    mcp_protocol.handle_mcp_post(handler)

    assert 400 in handler.sent_responses

    response_data = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert response_data.get("jsonrpc") == "2.0"
    assert response_data.get("id") == 1
    assert "error" in response_data
    assert response_data["error"].get("code") == -32601  # Method not found
    assert "Unknown method" in response_data["error"].get("message", "")

def test_handle_mcp_raises():
    """Ensure when _handle_mcp raises or its internal handler raises,
    the server returns a stable error envelope (500)."""
    services = MagicMock()
    mcp_protocol = MCPProtocolHandler(services)

    # We patch _mcp_ping (a valid method) to raise an exception
    with patch.object(mcp_protocol, '_mcp_ping', side_effect=Exception("Test Internal Error")):
        body_data = {"jsonrpc": "2.0", "method": "ping", "id": 42}
        body_bytes = json.dumps(body_data).encode("utf-8")
        headers = {"Content-Length": str(len(body_bytes))}
        handler = MockHandler(headers, body_bytes)

        mcp_protocol.handle_mcp_post(handler)

        # 500 status code is sent
        assert 500 in handler.sent_responses

        response_data = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert response_data.get("jsonrpc") == "2.0"
        assert response_data.get("id") == 42
        assert "error" in response_data
        assert response_data["error"].get("code") == -32603  # Internal error
        assert "Test Internal Error" in response_data["error"].get("message", "")
