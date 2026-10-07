import re
with open('plugin/chatbot/panel.py', 'r') as f:
    content = f.read()

target = '            except Exception as e:\n                log.debug("query setFocus after send: %s", e)\n'
idx = content.find(target)
end_idx = content.find('\n    def ', idx)
block = content[idx + len(target):end_idx]

lines = block.split('\n')
new_lines = []
for line in lines:
    if line.strip():
        new_lines.append('    ' + line)
    else:
        new_lines.append(line)

new_block = '\n        try:\n' + '\n'.join(new_lines) + '\n        except Exception:\n            self._restore_query_text(query_text)\n            raise'

content = content[:idx + len(target)] + new_block + content[end_idx:]

with open('plugin/chatbot/panel.py', 'w') as f:
    f.write(content)
