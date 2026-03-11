import re

def check_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    methods = re.findall(r'def\s+([a-zA-Z_0-9]+)\s*\(', content)
    for m in methods:
        if m.startswith('__'):
            continue
        count = len(re.findall(r'\b' + m + r'\b', content))
        if count == 1:
            print(f"Potentially dead method in {filepath}: {m}")

check_file("plugin/options_handler.py")
check_file("plugin/prompt_function.py")
