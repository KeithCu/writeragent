with open("plugin/mcp/mcp_protocol.py", "r") as f:
    text = f.read()

text = text.replace(
    'events_to_process.append(MCPEvent(kind=EventKind.TOOL_COMPLETED, data={"result": res}))',
    'events_to_process.append(MCPEvent(kind=EventKind.TOOL_COMPLETED, data={"result": res}))\n                        if req_id is not None:\n                            self._cancelled_requests.discard(req_id)'
)

with open("plugin/mcp/mcp_protocol.py", "w") as f:
    f.write(text)
