with open('plugin/chatbot/panel.py', 'r') as f:
    content = f.read()

import re
content = re.sub(
    r'        except Exception:\n            pass\n        sc = self\.send_listener\.send_control',
    '        except Exception:\n            return\n        sc = self.send_listener.send_control',
    content
)

with open('plugin/chatbot/panel.py', 'w') as f:
    f.write(content)
