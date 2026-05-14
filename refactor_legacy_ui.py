import ast

with open('plugin/chatbot/legacy_ui.py', 'r') as f:
    source = f.read()

tree = ast.parse(source)

for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name == 'settings_box':
        print(f"settings_box starts at line {node.lineno} and ends at line {node.end_lineno}")
        print(f"Total lines: {node.end_lineno - node.lineno + 1}")
