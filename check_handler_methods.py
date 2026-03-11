import re

with open("plugin/options_handler.py", "r") as f:
    content = f.read()

methods = re.findall(r'def\s+([a-zA-Z_0-9]+)\s*\(', content)
called = set(re.findall(r'self\.([a-zA-Z_0-9]+)\(', content))
called.update(re.findall(r'self\._handler\.([a-zA-Z_0-9]+)\(', content))
called.update(re.findall(r'self\._state\.([a-zA-Z_0-9]+)\(', content))

print("Defined methods:")
for m in methods:
    if m not in called and not m.startswith('__') and m not in ['callHandlerMethod', 'getSupportedMethodNames', 'itemStateChanged', 'disposing', 'actionPerformed', 'supportsService', 'getImplementationName', 'getSupportedServiceNames']:
        print(f"Potentially dead method: {m}")
