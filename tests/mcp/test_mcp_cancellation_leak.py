from unittest.mock import MagicMock
from plugin.mcp.mcp_protocol import MCPProtocolHandler
from plugin.mcp.wire_types import parse_jsonrpc_request

def test_in_flight_requests_does_not_leak():
    services = MagicMock()
    handler = MCPProtocolHandler(services)

    # Send a request for an excluded tool. This early returns from _mcp_tools_call
    # simulate direct_discovery mode to easily get an excluded tier.
    handler._tool_exposure_mode = MagicMock(return_value="direct_flat")

    tool_mock = MagicMock()
    tool_mock.tier = "specialized_control"  # excluded in direct_flat

    registry = MagicMock()
    registry.get.return_value = tool_mock
    handler.tool_registry = registry

    req = {"jsonrpc": "2.0", "id": "req_1", "method": "tools/call", "params": {"name": "test_tool", "arguments": {}}}

    res = handler._process_jsonrpc(req)

    assert res[0] == 200
    assert res[1]["result"]["isError"] is True

    # Must not leak in-flight request
    assert (None, "req_1") not in handler._in_flight_requests
    assert not handler._in_flight_requests

def test_bool_is_not_id():
    msg = {"jsonrpc": "2.0", "id": True, "method": "ping"}
    res = parse_jsonrpc_request(msg)
    assert res.__class__.__name__ == "JsonRpcParseError"

    msg = {"jsonrpc": "2.0", "id": False, "method": "ping"}
    res = parse_jsonrpc_request(msg)
    assert res.__class__.__name__ == "JsonRpcParseError"
