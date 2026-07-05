import json
import urllib.request

from plugin.mcp.server import mcp_endpoint_url
from plugin.mcp.mcp_protocol import build_initialize_instructions


def test_mcp_endpoint_url_helper():
    assert mcp_endpoint_url("localhost", 8765) == "http://localhost:8765/mcp"
    assert mcp_endpoint_url("127.0.0.1", 9000, use_ssl=True) == "https://127.0.0.1:9000/mcp"


def test_initialize_instructions_lean_pointer_in_every_mode():
    """T4/G2: every exposure mode keeps the base + its mode hint + the get_guidance pointer, and
    stays LEAN (clients drop/truncate the connect-time text, so the manual moved out of it)."""
    for mode in ("delegate", "direct_flat", "direct_discovery", "unknown"):
        text = build_initialize_instructions(mode)
        assert "WriterAgent MCP" in text          # base preserved
        assert "get_guidance" in text             # the pointer to the on-demand manual
        assert "get_document_tree" not in text    # the nav workflow moved to the manual
        assert len(text) < 1500


def test_navigation_workflow_lives_in_the_manual():
    """R5's map-first workflow (outline -> drill -> search) is delivered via the manual now —
    get_guidance('navigation') for MCP clients and the sidebar; full_manual() for the agent-backend path."""
    from plugin.framework.agent_manual import get_section

    nav = get_section("navigation")
    assert "get_document_tree" in nav
    assert "get_heading_children" in nav
    assert "search_in_document" in nav
    assert "heading_only" in nav


def test_initialize_instructions_mode_hints_differ():
    """Each exposure mode keeps its own discovery hint (regression on the refactor)."""
    flat = build_initialize_instructions("direct_flat")
    discovery = build_initialize_instructions("direct_discovery")
    delegate = build_initialize_instructions("delegate")
    assert "listed directly" in flat
    assert "find_tools" in discovery
    assert "delegate_to_specialized" in delegate


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


def test_mcp_initialize_instructions_are_lean_and_point_to_the_manual(mcp_server):
    """T4/G2 contract: initialize.instructions is LEAN (major clients drop or truncate it — Claude
    Desktop doesn't read it, Claude Code truncates ~2KB) and points to get_guidance(topic), where
    the full behavior manual lives (single source: the shared prompt pieces in constants.py,
    mapped per topic by plugin/framework/agent_manual.py, pinned in
    tests/framework/test_agent_manual.py). Only the invariants ride along."""
    url = f"{mcp_server}/mcp"
    payload = {"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}}
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, method="POST", data=data_bytes)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.getcode() == 200
        data = json.loads(response.read().decode("utf-8"))

    instr = data["result"]["instructions"]
    assert "get_guidance" in instr                        # the pointer to the on-demand manual
    assert "structured fields" in instr                   # invariant: confirm edits structurally
    assert "accept/reject" in instr                       # invariant: tracked changes are the user's
    assert len(instr) < 1500                              # lean — the manual itself moved out
