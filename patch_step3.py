with open('plugin/chatbot/panel.py', 'r') as f:
    content = f.read()

content = content.replace(
    '            self._last_mcp_req_id = kwargs.get("req_id")',
    ''
)

content = content.replace(
    '            rid = str(kwargs.get("req_id", ""))\n            self._last_mcp_turn[rid] = current_turn(self)',
    '            rid = str(kwargs.get("req_id", ""))\n            self._last_mcp_turn = {k: v for k, v in self._last_mcp_turn.items() if getattr(v, "alive", False)}\n            self._last_mcp_turn[rid] = current_turn(self)'
)

with open('plugin/chatbot/panel.py', 'w') as f:
    f.write(content)
