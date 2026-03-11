import re

with open("plugin/options_handler.py", "r") as f:
    content = f.read()

# Let's see if any other methods are really unused.
# Like _read_select, _populate_select, etc.

methods = re.findall(r'def\s+([a-zA-Z_0-9]+)\s*\(', content)

for m in methods:
    if m.startswith('__'): continue
    occurrences = content.count(m)
    if occurrences == 1:
        print(f"Dead method: {m}")
