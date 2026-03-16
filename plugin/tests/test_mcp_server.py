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
        assert "error" in response_data

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
