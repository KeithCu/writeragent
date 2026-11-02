from unittest.mock import MagicMock
from plugin.mcp.mcp_protocol import MCPProtocolHandler

def test_debug_call_tool_validates_args():
    services = MagicMock()
    handler = MCPProtocolHandler(services)

    res = handler._debug_call_tool("my_tool", [])
    assert res == {"error": "'args' must be a dictionary"}

    handler._run_tool = MagicMock(return_value={"status": "ok"})
    res = handler._debug_call_tool("my_tool", {"document_url": "doc123"})

    handler._run_tool.assert_called_with("my_tool", {}, document_url="doc123")
