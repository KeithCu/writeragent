import json
import urllib.request

from plugin.mcp.server import mcp_endpoint_url


def test_mcp_endpoint_url_helper():
    assert mcp_endpoint_url("localhost", 8765) == "http://localhost:8765/mcp"
    assert mcp_endpoint_url("127.0.0.1", 9000, use_ssl=True) == "https://127.0.0.1:9000/mcp"


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


def test_root_info_includes_mcp_endpoint(mcp_server):
    """GET / advertises the streamable-HTTP MCP endpoint when MCP routes are registered."""
    req = urllib.request.Request(f"{mcp_server}/", method="GET")
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.getcode() == 200
        data = json.loads(response.read().decode("utf-8"))
        assert data.get("mcp_endpoint") == f"{mcp_server}/mcp"
