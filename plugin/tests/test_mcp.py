import pytest
import urllib.request
import urllib.error
import json
import time
import socket
from unittest.mock import MagicMock

from plugin.modules.http import HttpModule


def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def mcp_server():
    """Start the HTTP server using HttpModule with mocked services."""
    # Mock services
    services = MagicMock()

    # Mock tool registry
    tool_registry = MagicMock()
    tool_registry.get_mcp_schemas.return_value = [
        {"name": "test_tool", "description": "A test tool", "inputSchema": {"type": "object", "properties": {}}}
    ]
    tool_registry.tool_names = ["test_tool"]
    services.tools = tool_registry

    # Mock document service
    doc_svc = MagicMock()
    doc_svc.get_active_document.return_value = MagicMock()
    doc_svc.detect_doc_type.return_value = "writer"
    doc_svc.resolve_document_by_url.return_value = (MagicMock(), "writer")
    services.document = doc_svc

    # Mock event bus
    services.events = MagicMock()

    # Get a dynamic port
    port = get_free_port()

    # Mock config service
    config_svc = MagicMock()
    config_svc.proxy_for.return_value = {
        "enabled": True,
        "mcp_enabled": True,
        "port": port,
        "host": "127.0.0.1",
        "use_ssl": False
    }
    services.config = config_svc

    # Initialize HttpModule
    http_module = HttpModule()
    http_module.name = "http"
    http_module.initialize(services)
    http_module.start_background(services)
    
    url = f"http://127.0.0.1:{port}"
    max_retries = 20
    server_ready = False
    
    for _ in range(max_retries):
        try:
            req = urllib.request.Request(f"{url}/health")
            with urllib.request.urlopen(req, timeout=1) as response:
                if response.getcode() == 200:
                    server_ready = True
                    break
        except Exception:
            time.sleep(0.5)
            
    if not server_ready:
        http_module.shutdown()
        pytest.fail("Server did not start in time")

    yield url

    http_module.shutdown()


def test_health_endpoint(mcp_server):
    """Test the /health endpoint schema and status code."""
    url = f"{mcp_server}/health"
    req = urllib.request.Request(url, method="GET")
    
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.getcode() == 200
        body = response.read().decode('utf-8')
        data = json.loads(body)

        assert "status" in data
        assert data["status"] == "healthy"
        assert "server" in data
        assert data["server"] == "WriterAgent"
        assert "version" in data


def test_mcp_tools(mcp_server):
    """Test the MCP JSON-RPC tools/list endpoint schema and status code."""
    url = f"{mcp_server}/mcp"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list"
    }

    data_bytes = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, method="POST", data=data_bytes)
    req.add_header('Content-Type', 'application/json')

    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.getcode() == 200
        body = response.read().decode('utf-8')
        data = json.loads(body)

        assert data.get("jsonrpc") == "2.0"
        assert data.get("id") == 1
        assert "result" in data

        result = data["result"]
        assert "tools" in result
        assert isinstance(result["tools"], list)

        if len(result["tools"]) > 0:
            tool = result["tools"][0]
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool


def test_mcp_resources(mcp_server):
    """Test the MCP JSON-RPC resources/list endpoint schema and status code."""
    url = f"{mcp_server}/mcp"
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "resources/list"
    }
    
    data_bytes = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, method="POST", data=data_bytes)
    req.add_header('Content-Type', 'application/json')
    
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.getcode() == 200
        body = response.read().decode('utf-8')
        data = json.loads(body)

        assert data.get("jsonrpc") == "2.0"
        assert data.get("id") == 2
        assert "result" in data

        result = data["result"]
        assert "resources" in result
        assert isinstance(result["resources"], list)
