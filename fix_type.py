import re

with open('plugin/doc/document_helpers.py', 'r') as f:
    content = f.read()

content = content.replace(
    'def resolve_locator(self, doc: Any, locator: str) -> dict[str, int]:',
    'def resolve_locator(self, doc: Any, locator: str) -> dict[str, Any]:'
)

with open('plugin/doc/document_helpers.py', 'w') as f:
    f.write(content)
