import ast

def get_defined_methods(filepath):
    with open(filepath, "r") as f:
        tree = ast.parse(f.read())

    methods = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    methods.append(item.name)
    return methods

def get_called_methods(filepath):
    with open(filepath, "r") as f:
        content = f.read()

    import re
    called = re.findall(r'self\.([a-zA-Z_0-9]+)\(', content)
    called += re.findall(r'self\._handler\.([a-zA-Z_0-9]+)\(', content)
    return set(called)

methods = get_defined_methods("plugin/options_handler.py")
called = get_called_methods("plugin/options_handler.py")

print("Methods defined:", methods)
print("Methods called:", called)
for m in methods:
    if m not in called and not m.startswith('__') and m not in ['callHandlerMethod', 'getSupportedMethodNames', 'itemStateChanged', 'disposing', 'actionPerformed', 'supportsService', 'getImplementationName', 'getSupportedServiceNames']:
        print("POSSIBLY DEAD:", m)
