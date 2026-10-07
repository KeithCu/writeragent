with open('plugin/chatbot/panel.py', 'r') as f:
    content = f.read()

helper = """    def _start_local_error_turn(self, text: str) -> None:
        from plugin.chatbot.tool_loop_actions import begin_send_turn, drop_turn
        begin_send_turn(self, "")
        self._append_response(text)
        self._terminal_status = "Error"
        drop_turn(self)

    def _append_response("""

import re
content = re.sub(r'    def _append_response\(', helper, content, count=1)

content = content.replace('self._append_response("\\n[Audio error: %s]\\n" % msg)', 'self._start_local_error_turn("\\n[Audio error: %s]\\n" % msg)')
content = content.replace('self._append_response("\\n[Audio error: %s]\\n" % str(re))', 'self._start_local_error_turn("\\n[Audio error: %s]\\n" % str(re))')

with open('plugin/chatbot/panel.py', 'w') as f:
    f.write(content)
