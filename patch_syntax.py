import re

with open('plugin/writer/locale/harper.py', 'r') as f:
    content = f.read()

# Fix syntax error caused by regex finding replacement
content = re.sub(
    r'import re\n\s+lines = \[m\.group\(0\) for m in re\.finditer\(r"\[\^\\n\\r\]\*\(\?:\\r\\n\|\\r\|\\n\)\?", text\)\]\n\s+if lines and not lines\[-1\]:\n\s+lines\.pop\(\)',
    r'import re\n    lines = [m.group(0) for m in re.finditer(r"[^\\r\\n]*(?:\\r\\n|\\r|\\n)?", text)]\n    if lines and not lines[-1]:\n        lines.pop()',
    content
)

with open('plugin/writer/locale/harper.py', 'w') as f:
    f.write(content)
