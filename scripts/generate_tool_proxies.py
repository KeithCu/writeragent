#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Generate writeragent_api.py — Python proxy module for venv subprocess tool calls.

Usage: python scripts/generate_tool_proxies.py > plugin/scripting/writeragent_api.py
"""

import os
import sys
import textwrap
import pprint
from collections import defaultdict
from typing import Any, Iterable, cast

# Ensure the project root is in sys.path
scripts_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(scripts_dir)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

# Mock UNO before importing plugin modules
import types
from unittest.mock import MagicMock

# Dictionary to cache mock classes to avoid duplicates but also metaclass/MRO issues
_MOCK_CLASSES = {}

def get_mock_class(name):
    if name not in _MOCK_CLASSES:
        # Create a unique class for each name
        class MockBase:
            def __init__(self, *args, **kwargs): pass
            def __getattr__(self, name): return MagicMock()
            def __call__(self, *args, **kwargs): return self
            @classmethod
            def addImplementation(cls, *args, **kwargs): pass
        MockBase.__name__ = name
        _MOCK_CLASSES[name] = MockBase
    return _MOCK_CLASSES[name]

# Universal fallback for sys.modules
class MockModule(types.ModuleType):
    def __init__(self, name):
        super().__init__(name)
        self.__path__ = []
    def __getattr__(self, name):
        # Return a unique mock class for each attribute name
        return get_mock_class(name)

sys.modules["uno"] = MagicMock()
sys.modules["unohelper"] = MockModule("unohelper")

# Custom finder for com.sun.star hierarchy
class MockFinder:
    def find_spec(self, fullname, path, target=None):
        if fullname.startswith("com.") or fullname == "com":
            return self._gen_spec(fullname)
        return None
    def _gen_spec(self, fullname):
        from importlib.machinery import ModuleSpec
        return ModuleSpec(fullname, self)
    def create_module(self, spec):
        return MockModule(spec.name)
    def exec_module(self, module):
        pass

sys.meta_path.insert(0, MockFinder())

from plugin.framework.tool import ToolBase

JSON_TO_PYTHON = {
    "string": "str",
    "integer": "int",
    "boolean": "bool",
    "number": "float",
    "object": "dict",
    "array": "list",
}

DEFAULTS_BY_TYPE = {
    "string": '""',
    "integer": "0",
    "boolean": "True",
    "number": "0.0",
    "object": "{}",
    "array": "[]",
}


def _param_default(schema: dict) -> str:
    """Derive a Python default value from a JSON Schema property."""
    if "default" in schema:
        return repr(schema["default"])
    return DEFAULTS_BY_TYPE.get(schema.get("type", ""), "None")


def schema_to_signature(tool: ToolBase) -> tuple[list[str], list[str]]:
    """Convert a tool's JSON Schema parameters to Python positional and keyword args."""
    props = (tool.parameters or {}).get("properties", {})
    required = set((tool.parameters or {}).get("required", []))

    positional, keyword = [], []
    for param_name, schema in props.items():
        py_type = JSON_TO_PYTHON.get(schema.get("type", ""), "Any")
        if param_name in required:
            positional.append(f"{param_name}: {py_type}")
        else:
            default = _param_default(schema)
            keyword.append(f"{param_name}: {py_type} = {default}")
    return positional, keyword


def group_tools(tools: list[ToolBase]) -> dict[str, list[tuple[str, ToolBase]]]:
    """Group tools by namespace prefix, stripping the prefix from method names."""
    groups: dict[str, list[tuple[str, ToolBase]]] = defaultdict(list)
    for tool in tools:
        name = tool.name or ""
        # 1. Check specialized_domain
        domain = getattr(tool, "specialized_domain", None)
        if domain:
            namespace = domain
            # Strip prefix if it matches domain (e.g. footnotes_insert -> insert)
            prefix = domain
            if domain.endswith("s"):
                # Handle plurals (footnotes -> footnote)
                singular = domain[:-1]
                if name.startswith(singular + "_"):
                    prefix = singular
                elif name.startswith(domain + "_"):
                    prefix = domain
            
            if name.startswith(prefix + "_"):
                rest = name[len(prefix) + 1 :]
            else:
                rest = name
        else:
            # Break up "core" tools by document type
            doc_types = getattr(tool, "doc_types", []) or []
            uno_services = getattr(tool, "uno_services", []) or []
            
            # Infer doc_types from uno_services if missing
            if not doc_types and uno_services:
                inferred = set()
                for svc in uno_services:
                    if "text.TextDocument" in svc: inferred.add("writer")
                    elif "sheet.SpreadsheetDocument" in svc: inferred.add("calc")
                    elif "drawing.DrawingDocument" in svc: inferred.add("draw")
                    elif "presentation.PresentationDocument" in svc: inferred.add("draw")
                doc_types = list(inferred)

            if len(doc_types) == 1:
                namespace = doc_types[0]
            elif set(doc_types) == {"draw", "impress"}:
                namespace = "draw"
            elif not doc_types:
                # Truly universal tools stay in core (e.g. web_research, upsert_memory)
                namespace = "core"
            else:
                # Mixed support (Writer + Calc etc)
                namespace = "core"
            rest = name

        # Singularize namespace for nicer usage: footnote.insert instead of footnotes.insert
        if namespace.endswith("s") and namespace not in ("images", "styles", "forms"):
            # Very basic singularization
            namespace = namespace[:-1]

        groups[namespace].append((rest, tool))
    return dict(groups)


def generate_module(tools: list[ToolBase]) -> str:
    """Generate the complete writeragent_api.py module."""
    groups = group_tools(tools)

    lines = [
        '"""Auto-generated WriterAgent tool proxy API.',
        '',
        'Generated by scripts/generate_tool_proxies.py — DO NOT EDIT.',
        'Provides Python-native access to WriterAgent tools from venv subprocess scripts.',
        '"""',
        'import json',
        'import sys',
        'import threading',
        'import uuid',
        'from typing import Any, Dict, List, Optional, Union',
        '',
        '',
        '# ── RPC transport ──────────────────────────────────────────────',
        '_lock = threading.Lock()',
        '',
        '',
        'def _rpc_call(tool_name: str, **kwargs) -> dict:',
        '    """Send a tool call to the LibreOffice host and block for the result."""',
        '    call_id = str(uuid.uuid4())',
        '    request = {"type": "tool_call", "id": call_id, "tool": tool_name, "args": kwargs}',
        '    with _lock:',
        '        sys.stdout.write(json.dumps(request) + "\\n")',
        '        sys.stdout.flush()',
        '        # Block for response (host writes to our stdin)',
        '        line = sys.stdin.readline()',
        '    if not line:',
        '        raise ConnectionError("Lost connection to LibreOffice host")',
        '    try:',
        '        response = json.loads(line)',
        '    except json.JSONDecodeError:',
        '        raise RuntimeError(f"Host sent invalid JSON: {line[:200]}")',
        '',
        '    if response.get("status") == "error":',
        '        raise RuntimeError(response.get("message", response.get("error", "Unknown error")))',
        '    return response',
        '',
        '',
    ]

    # Domain tools whitelist for host-side enforcement
    domain_tools_map = {}
    for ns, tool_list in sorted(groups.items()):
        domain_tools_map[ns] = sorted([t.name for _, t in tool_list])

    pretty_map = pprint.pformat(domain_tools_map, indent=4, width=120)
    lines.append(f"DOMAIN_TOOLS = {pretty_map}")
    lines.append("")
    lines.append("")

    for namespace in sorted(groups.keys()):
        tool_list = groups[namespace]
        # Emit a class that acts as a namespace
        class_name = namespace.title().replace("_", "") + "Proxy"
        lines.append(f"class _{class_name}:")
        lines.append(f'    """Proxy for {namespace} tools."""')
        lines.append("")

        for short_name, tool in sorted(tool_list, key=lambda x: x[0]):
            # Generate method
            pos, kw = schema_to_signature(tool)
            # Add self
            all_params_list = ["self"] + pos
            if kw:
                all_params_list.append("*")
                all_params_list.extend(kw)
            
            all_params = ", ".join(all_params_list)

            all_param_names = list((tool.parameters or {}).get("properties", {}).keys())
            if all_param_names:
                kwargs_body = ", " + ", ".join(f"{p}={p}" for p in all_param_names)
            else:
                kwargs_body = ""

            desc = (tool.description or "").split(". ")[0] + "."
            # Escape double quotes in description
            desc = desc.replace('"', '\\"')

            lines.append(f"    def {short_name}({all_params}) -> dict:")
            lines.append(f'        """{desc}"""')
            lines.append(f'        return _rpc_call("{tool.name}"{kwargs_body})')
            lines.append("")

        # Singleton instance
        lines.append(f"{namespace} = _{class_name}()")
        lines.append("")
        lines.append("")

    return "\n".join(lines)


def main():
    # Bootstrap the registry
    from plugin.main import get_tools
    
    # We need a mock environment because get_tools() might trigger bootstrap()
    # which expects a UNO context. But ToolRegistry itself doesn't need much.
    registry = get_tools()
    
    # Get all tools, regardless of doc type or tier
    # filter_doc_type=False ensures we see all tools even without a live document
    # Get all tools, then filter out specialized_control EXCEPT for specialized_workflow_finished
    all_tools = registry.get_tools(filter_doc_type=False, exclude_tiers=frozenset())
    all_tools = [t for t in all_tools if getattr(t, "tier", None) != "specialized_control" or t.name == "specialized_workflow_finished"]
    
    print(generate_module(all_tools))


if __name__ == "__main__":
    main()
