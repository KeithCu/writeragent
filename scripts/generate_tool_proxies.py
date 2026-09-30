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

import keyword
import os
import re
import sys
import pprint
from collections import defaultdict
from importlib.abc import Loader, MetaPathFinder

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
        self.__file__ = None

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return get_mock_class(name)

sys.modules["uno"] = MagicMock()
sys.modules["unohelper"] = MockModule("unohelper")

# Custom finder for com.sun.star hierarchy
class MockFinder(MetaPathFinder, Loader):
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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plugin.framework.tool import ToolBase

JSON_TO_PYTHON = {
    "string": "str",
    "integer": "int",
    "boolean": "bool",
    "number": "float",
    "object": "dict[str, Any]",
    "array": "list[Any]",
}

DEFAULTS_BY_TYPE = {
    "string": '""',
    "integer": "0",
    "boolean": "True",
    "number": "0.0",
    "object": "{}",
    "array": "[]",
}


def _get_schema_type(schema: dict) -> str:
    t = schema.get("type", "")
    if isinstance(t, list):
        types_list = [x for x in t if x != "null"]
        t = types_list[0] if types_list else ""
    return str(t)


def _python_type(schema: dict) -> str:
    """JSON Schema property -> Python annotation with type arguments.

    Bare ``dict`` / ``list`` would trip ``reportMissingTypeArgument``. Match
    the type-checking dialect: ``dict[str, Any]``, ``list[str]`` when items
    are strings, otherwise ``list[Any]``.
    """
    t = _get_schema_type(schema)
    if t == "array":
        items = schema.get("items")
        if isinstance(items, dict) and _get_schema_type(items) == "string":
            return "list[str]"
        return "list[Any]"
    return JSON_TO_PYTHON.get(t, "Any")


def _param_default(schema: dict) -> str:
    """Derive a Python default value from a JSON Schema property."""
    if "default" in schema:
        return repr(schema["default"])
    return DEFAULTS_BY_TYPE.get(_get_schema_type(schema), "None")


def _python_param_name(schema_key: str) -> str:
    """Python identifier for a schema key. Only ``range`` is remapped (builtin clash)."""
    if schema_key == "range":
        return "range_name"
    if keyword.iskeyword(schema_key):
        return schema_key + "_"
    return schema_key


def _iter_params(tool: "ToolBase") -> list[tuple[str, str, dict]]:
    """Yield (python_name, schema_key, property_schema) in schema order."""
    props = (tool.parameters or {}).get("properties", {})
    return [(_python_param_name(key), key, schema) for key, schema in props.items()]


def schema_to_signature(tool: "ToolBase") -> tuple[list[str], list[str]]:
    """Convert a tool's JSON Schema parameters to Python positional and keyword args."""
    required = set((tool.parameters or {}).get("required", []))

    positional, keyword = [], []
    for py_name, schema_key, schema in _iter_params(tool):
        py_type = _python_type(schema)
        if schema_key in required:
            positional.append(f"{py_name}: {py_type}")
        else:
            if "default" in schema:
                default = _param_default(schema)
                keyword.append(f"{py_name}: {py_type} = {default}")
            else:
                # Omit from the wire (see _rpc_call dropping None) so the tool's
                # own default applies. A boolean default of True was making
                # apply_document_content(..., dry_run=True) a silent no-op.
                keyword.append(f"{py_name}: {py_type} | None = None")
    return positional, keyword


# Sidebar-only / chat-mode domains: not part of the Python proxy surface. web_research stays.
API_EXCLUDED_DOMAINS = frozenset({
    "writing_plan",
    "deep_research",
    "brainstorming",
    "document_research",
    "ppt-master",
    "ppt_master",
})

# LLM specialized-toolset orchestration. Chat/MCP still registers these; venv
# scripts call document tools directly and do not run the delegate/finish loop.
API_EXCLUDED_TOOLS = frozenset({
    "delegate_to_specialized_writer_toolset",
    "delegate_to_specialized_calc_toolset",
    "delegate_to_specialized_draw_toolset",
    # Python outer→inner LLM hop. The inner smol agent still calls
    # specialized_workflow_finished as a registered ToolBase, not via this proxy.
    "delegate_tool_domains",
    "specialized_workflow_finished",
})


def _domain_excluded(domain: str | None) -> bool:
    """True for chat-mode domains, including ``ppt-master`` / ``ppt_master`` spellings."""
    if not isinstance(domain, str) or not domain:
        return False
    folded = {domain, domain.replace("-", "_"), domain.replace("_", "-")}
    return bool(folded & API_EXCLUDED_DOMAINS)


def _doc_paragraphs(text: str) -> list[str]:
    """Collapse runs of whitespace inside each paragraph; keep blank-line breaks."""
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", normalized):
        collapsed = " ".join(block.split())
        if collapsed:
            paragraphs.append(collapsed)
    return paragraphs


def _as_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    if text[-1] not in ".!?":
        return text + "."
    return text


def _format_arg_doc(py_name: str, schema: dict, *, required: bool) -> str:
    """One Google-style Args line. Type stays on the signature, not here."""
    kind = "required" if required else "optional"
    desc = _as_sentence(" ".join(str(schema.get("description") or "").split()))
    enum = schema.get("enum")
    bits = [desc] if desc else []
    if isinstance(enum, (list, tuple)) and enum:
        shown = ", ".join(" ".join(str(item).split()) for item in enum)
        bits.append(f"One of: {shown}.")
    detail = " ".join(bits)
    if detail:
        return f"    {py_name} ({kind}): {detail}"
    return f"    {py_name} ({kind}):"


def _method_doc_lines(tool: "ToolBase") -> list[str]:
    """Full tool description plus Args. Empty when the tool has neither."""
    lines: list[str] = []
    for index, paragraph in enumerate(_doc_paragraphs(getattr(tool, "description", "") or "")):
        if index:
            lines.append("")
        lines.append(paragraph)

    required = set((tool.parameters or {}).get("required", []))
    params = _iter_params(tool)
    ordered = [item for item in params if item[1] in required]
    ordered.extend(item for item in params if item[1] not in required)
    arg_lines: list[str] = []
    seen: set[str] = set()
    for py_name, schema_key, schema in ordered:
        seen.add(schema_key)
        arg_lines.append(_format_arg_doc(py_name, schema, required=schema_key in required))
    # Same extras the signature appends (e.g. set_style number_format).
    for extra in sorted(getattr(tool, "scripting_only_parameters", None) or ()):
        if extra not in seen:
            arg_lines.append(f"    {extra} (optional): Scripting-only parameter.")
    if arg_lines:
        if lines:
            lines.append("")
        lines.append("Args:")
        lines.extend(arg_lines)
    return lines


def _py_doc_escape(text: str) -> str:
    """Make text safe inside a non-raw triple-quoted docstring.

    Backslashes are escapes there, and an embedded ``\"\"\"`` would end the
    literal. Escaping both keeps the runtime docstring equal to the tool text.
    """
    return text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def _append_docstring(lines: list[str], doc_lines: list[str]) -> None:
    if not doc_lines:
        lines.append('        """"""')
        return
    escaped = [_py_doc_escape(line) for line in doc_lines]
    if len(escaped) == 1:
        lines.append(f'        """{escaped[0]}"""')
        return
    lines.append(f'        """{escaped[0]}')
    for line in escaped[1:]:
        lines.append(f"        {line}" if line else "")
    lines.append('        """')


def group_tools(tools: list["ToolBase"]) -> dict[str, list[tuple[str, "ToolBase"]]]:
    """Group tools by namespace prefix, stripping the prefix from method names."""
    groups: dict[str, list[tuple[str, "ToolBase"]]] = defaultdict(list)
    for tool in tools:
        name = tool.name or ""
        # Orchestration names stay on the chat tool list; skip before namespace grouping
        # so they never become methods or DOMAIN_TOOLS entries.
        if name in API_EXCLUDED_TOOLS:
            continue
        # 1. Check specialized_domain
        domain = getattr(tool, "specialized_domain", None)
        if _domain_excluded(domain if isinstance(domain, str) else None):
            continue
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
        if namespace == "indexes":
            namespace = "index"
        elif namespace.endswith("s") and namespace not in ("images", "styles", "forms"):
            # Very basic singularization
            namespace = namespace[:-1]

        # Catch hyphen/underscore spellings that only show up as the namespace key.
        if _domain_excluded(namespace):
            continue

        groups[namespace].append((rest, tool))
    return dict(groups)


def generate_module(tools: list["ToolBase"]) -> str:
    """Generate the complete writeragent_api.py module."""
    groups = group_tools(tools)

    lines = [
        '"""Auto-generated WriterAgent tool proxy API.',
        '',
        'Generated by scripts/generate_tool_proxies.py — DO NOT EDIT.',
        'Provides Python-native access to WriterAgent tools from venv subprocess scripts.',
        'Method docstrings include the full tool description and an Args section',
        '(schema description, enum values, and required or optional).',
        '',
        'Skip replacing this with a __getattr__ proxy over DOMAIN_TOOLS: explicit',
        'per-tool classes are the public script API (IDE jump and types). Change this',
        'generator if the file is too large; do not slim the generated module by hand.',
        '"""',
        'import os',
        'from typing import Any',
        'from plugin.framework.constants import WORKFLOW_TASK_PREFIXES as _WORKFLOW_TASK_PREFIXES',
        '',
        '# Re-export so venv scripts and tests share the comment-scan prefix tuple.',
        'WORKFLOW_TASK_PREFIXES = _WORKFLOW_TASK_PREFIXES',
        '',
        '',
        '# Detect if running in-process (LibreOffice host) or out-of-process (Venv worker)',
        'IS_WORKER = os.environ.get("WRITERAGENT_IS_WORKER") == "1"',
        '',
        '',
        'def _rpc_call(tool_name: str, **kwargs: Any) -> dict[str, Any]:',
        '    """Send a tool call to the LibreOffice host and block for the result."""',
        '    kwargs = {k: v for k, v in kwargs.items() if v is not None}',
        '    if not IS_WORKER:',
        '        try:',
        '            from plugin.scripting.host_rpc import execute_tool',
        '',
        '            return execute_tool(tool_name, kwargs, caller="script")',
        '        except Exception as e:',
        '            raise RuntimeError(f"Failed to execute tool in-process: {e}")',
        '',
        '    from plugin.scripting.ipc import exchange_tool_call',
        '',
        '    return exchange_tool_call(tool_name, kwargs)',
        '',
        '',
        'def get_active_document_type() -> str:',
        '    """Return the active document\'s type (\'writer\', \'calc\', \'draw\', or \'unknown\')."""',
        '    try:',
        '        res = _rpc_call("list_open_documents")',
        '        for doc in res.get("documents", []):',
        '            if doc.get("is_active"):',
        '                return doc.get("doc_type", "unknown")',
        '    except Exception:',
        '        pass',
        '    return "unknown"',
        '',
        '',
    ]

    # Domain tools whitelist for host-side enforcement
    domain_tools_map = {}
    for ns, tool_list in sorted(groups.items()):
        domain_tools_map[ns] = sorted([t.name for _, t in tool_list if t.name])

    pretty_map = pprint.pformat(domain_tools_map, indent=4, width=120)
    lines.append(f"DOMAIN_TOOLS = {pretty_map}")
    lines.append("")
    lines.append("")

    for namespace in sorted(groups.keys()):
        tool_list = groups[namespace]
        # Emit a class that acts as a namespace (hyphens in domain names are invalid in Python identifiers).
        safe_ns = namespace.replace("-", "_")
        class_name = "".join(part.capitalize() for part in safe_ns.split("_")) + "Proxy"
        lines.append(f"class _{class_name}:")
        lines.append(f'    """Proxy for {namespace} tools."""')
        lines.append("")

        for short_name, tool in sorted(tool_list, key=lambda x: x[0]):
            # domain_verb strip can yield a Python keyword (style_import -> import).
            if keyword.iskeyword(short_name):
                short_name = short_name + "_"
            # Generate method
            pos, kw = schema_to_signature(tool)
            # Add self
            all_params_list = ["self"] + pos
            if kw:
                all_params_list.append("*")
                all_params_list.extend(kw)
            
            all_params = ", ".join(all_params_list)

            rpc_pairs = [(schema_key, py_name) for py_name, schema_key, _schema in _iter_params(tool)]
            # Scripting-only kwargs (e.g. set_style number_format) stay on the Python proxy
            # even though they are omitted from the LLM/MCP schema (#374 P3).
            scripting_only = sorted(getattr(tool, "scripting_only_parameters", None) or ())
            seen_schema = {schema_key for schema_key, _py in rpc_pairs}
            for extra in scripting_only:
                if extra not in seen_schema:
                    rpc_pairs.append((extra, extra))
                    if "*" not in all_params_list:
                        all_params_list.append("*")
                    all_params_list.append(f'{extra}: str | None = None')
                    all_params = ", ".join(all_params_list)
            if rpc_pairs:
                kwargs_body = ", " + ", ".join(f"{schema_key}={py_name}" for schema_key, py_name in rpc_pairs)
            else:
                kwargs_body = ""

            lines.append(f"    def {short_name}({all_params}) -> dict[str, Any]:")
            _append_docstring(lines, _method_doc_lines(tool))
            lines.append(f'        return _rpc_call("{tool.name}"{kwargs_body})')
            lines.append("")

        # Singleton instance (keep DOMAIN_TOOLS key as registered domain string)
        lines.append(f"{safe_ns} = _{class_name}()")
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
    # specialized_control is the inner chat loop (including specialized_workflow_finished).
    # Venv scripts do not run that loop, so the whole tier stays off the proxy.
    all_tools = registry.get_tools(filter_doc_type=False, exclude_tiers=frozenset())
    all_tools = [t for t in all_tools if getattr(t, "tier", None) != "specialized_control"]
    
    print(generate_module(all_tools))


if __name__ == "__main__":
    main()
