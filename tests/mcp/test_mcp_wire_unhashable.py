from plugin.mcp.wire_types import parse_jsonrpc_request, JsonRpcParseError

def test_unhashable_id():
    msg = {"jsonrpc": "2.0", "method": "ping", "id": {}}
    res = parse_jsonrpc_request(msg)
    assert isinstance(res, JsonRpcParseError)

    msg = {"jsonrpc": "2.0", "method": "ping", "id": []}
    res = parse_jsonrpc_request(msg)
    assert isinstance(res, JsonRpcParseError)
