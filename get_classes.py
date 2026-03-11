import ast

with open("plugin/options_handler.py", "r") as f:
    tree = ast.parse(f.read())

for node in tree.body:
    if isinstance(node, ast.ClassDef):
        print(f"Class: {node.name}")
        for item in node.body:
            if isinstance(item, ast.FunctionDef):
                pass
