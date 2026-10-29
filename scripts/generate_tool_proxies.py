#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Generate writeragent_api.py — Python proxy module for venv subprocess tool calls.

Usage:
    python scripts/generate_tool_proxies.py --output plugin/scripting/writeragent_api.py
    python scripts/generate_tool_proxies.py --check plugin/scripting/writeragent_api.py
"""

import argparse
import contextlib
import keyword
import os
import pprint
import re
import sys
import tempfile
import types
from collections import defaultdict
from importlib.abc import Loader, MetaPathFinder
from typing import TYPE_CHECKING, Any, NamedTuple
from unittest.mock import MagicMock

# Ensure the project root is in sys.path
scripts_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(scripts_dir)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

# Cache mock classes to avoid duplicates and metaclass/MRO issues
_MOCK_CLASSES: dict[str, type] = {}


def get_mock_class(name):
    if name not in _MOCK_CLASSES:
        class MockBase:
            def __init__(self, *args, **kwargs): pass
            def __getattr__(self, name): return MagicMock()
            def __call__(self, *args, **kwargs): return self
            @classmethod
            def addImplementation(cls, *args, **kwargs): pass
        MockBase.__name__ = name
        _MOCK_CLASSES[name] = MockBase
    return _MOCK_CLASSES[name]


class MockModule(types.ModuleType):
    def __init__(self, name):
        super().__init__(name)
        self.__path__ = []
        self.__file__ = None

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return get_mock_class(name)


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


@contextlib.contextmanager
def _mock_uno():
    """Install UNO mocks only during generator execution; restore on exit."""
    prev_uno = sys.modules.get("uno")
    prev_unohelper = sys.modules.get("unohelper")
    prev_meta_path = list(sys.meta_path)
    finder = MockFinder()

    sys.modules["uno"] = MagicMock()
    sys.modules["unohelper"] = MockModule("unohelper")
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path[:] = prev_meta_path
        if prev_uno is not None:
            sys.modules["uno"] = prev_uno
        else:
            sys.modules.pop("uno", None)
        if prev_unohelper is not None:
            sys.modules["unohelper"] = prev_unohelper
        else:
            sys.modules.pop("unohelper", None)


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
    t = schema.get("type", "")
    if isinstance(t, list):
        types_list = [x for x in t if x != "null"]
        has_null = "null" in t
        sub_types: list[str] = []
        for member in types_list:
            if member == "array":
                items = schema.get("items")
                if isinstance(items, dict) and _get_schema_type(items) == "string":
                    sub_types.append("list[str]")
                else:
                    sub_types.append("list[Any]")
            else:
                sub_types.append(JSON_TO_PYTHON.get(str(member), "Any"))
        deduped = list(dict.fromkeys(sub_types))
        if has_null:
            deduped.append("None")
        return " | ".join(deduped) if deduped else "Any"

    if t == "array":
        items = schema.get("items")
        if isinstance(items, dict) and _get_schema_type(items) == "string":
            return "list[str]"
        return "list[Any]"
    return JSON_TO_PYTHON.get(t, "Any")


def _param_default(schema: dict) -> str:
    """Derive a Python default value from a JSON Schema property.

    Only called when "default" in schema. There is no per-type default table
    because a boolean True made apply_document_content(..., dry_run=True) a
    silent no-op. Optionals without a schema default stay None in
    schema_to_signature so _rpc_call can drop them.
    """
    return repr(schema["default"])


def _python_param_name(schema_key: str) -> str:
    """Python identifier for a schema key. Only ``range`` is remapped (builtin clash)."""
    if schema_key == "range":
        return "range_name"
    if keyword.iskeyword(schema_key):
        return schema_key + "_"
    return schema_key


class ParamRecord(NamedTuple):
    py_name: str
    schema_key: str
    annotation: str
    required: bool
    default: str | None
    doc_line: str
    is_keyword: bool


def _collect_params(tool: "ToolBase") -> list[ParamRecord]:
    # Read tool.parameters directly, not get_parameters(doc_type). ManageCharts.get_parameters
    # strips Calc or Writer/Draw fields per doc_type, but proxy stubs must keep the union.
    # The only other get_parameters override is run_venv_python_script
    # (in API_EXCLUDED_TOOLS).
    props = (tool.parameters or {}).get("properties", {})
    required_set = set((tool.parameters or {}).get("required", []))
    tool_name = getattr(tool, "name", "") or ""

    records: list[ParamRecord] = []
    seen_keys: set[str] = set()

    for key, schema in props.items():
        if not key.isidentifier() or key == "self":
            raise ValueError(f"Invalid parameter name {key!r} in tool {tool_name!r}")
        seen_keys.add(key)
        py_name = _python_param_name(key)
        is_kw = keyword.iskeyword(key)
        py_type = _python_type(schema)
        is_req = key in required_set

        if is_req:
            annotation = py_type
            default = None
        else:
            if "default" in schema:
                val = schema["default"]
                if val is None:
                    annotation = f"{py_type} | None" if "None" not in py_type.split(" | ") else py_type
                    default = "None"
                else:
                    annotation = py_type
                    default = _param_default(schema)
            else:
                annotation = f"{py_type} | None" if "None" not in py_type.split(" | ") else py_type
                default = "None"

        doc_line = _format_arg_doc(py_name, schema, required=is_req)
        records.append(
            ParamRecord(
                py_name=py_name,
                schema_key=key,
                annotation=annotation,
                required=is_req,
                default=default,
                doc_line=doc_line,
                is_keyword=is_kw,
            )
        )

    # Scripting-only kwargs (e.g. set_style number_format) stay on the Python proxy
    # even though they are omitted from the LLM/MCP schema (#374 P3).
    # Tip: scripting-only kwargs (e.g. set_style number_format) stay on the Python proxy even though
    # they are omitted from the LLM/MCP schema (#374 P3).
    for extra in sorted(getattr(tool, "scripting_only_parameters", None) or ()):
        if extra not in seen_keys:
            if not extra.isidentifier() or extra == "self":
                raise ValueError(f"Invalid scripting-only parameter name {extra!r} in tool {tool_name!r}")
            py_name = _python_param_name(extra)
            is_kw = keyword.iskeyword(extra)
            records.append(
                ParamRecord(
                    py_name=py_name,
                    schema_key=extra,
                    annotation="str | None",
                    required=False,
                    default="None",
                    doc_line=f"    {extra} (optional): Scripting-only parameter.",
                    is_keyword=is_kw,
                )
            )

    return records


def schema_to_signature(tool: "ToolBase") -> tuple[list[str], list[str]]:
    """Convert a tool's JSON Schema parameters to Python positional and keyword args."""
    params = _collect_params(tool)
    positional: list[str] = []
    keyword_args: list[str] = []
    for p in params:
        if p.required:
            positional.append(f"{p.py_name}: {p.annotation}")
        else:
            keyword_args.append(f"{p.py_name}: {p.annotation} = {p.default}")
    return positional, keyword_args


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
    # Re-entering the venv worker from a script deadlocks the pipe. The host
    # already rejects the call; the generated proxy must not advertise it.
    "run_venv_python_script",
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


def _method_doc_lines(tool: "ToolBase", params: list[ParamRecord] | None = None) -> list[str]:
    """Full tool description plus Args. Empty when the tool has neither."""
    if params is None:
        params = _collect_params(tool)

    lines: list[str] = []
    for index, paragraph in enumerate(_doc_paragraphs(getattr(tool, "description", "") or "")):
        if index:
            lines.append("")
        lines.append(paragraph)

    arg_lines: list[str] = []
    for p in params:
        if p.required:
            arg_lines.append(p.doc_line)
    for p in params:
        if not p.required:
            arg_lines.append(p.doc_line)

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
        if escaped[0].endswith('"'):
            lines.append(f'        """{escaped[0]}')
            lines.append('        """')
        else:
            lines.append(f'        """{escaped[0]}"""')
        return
    lines.append(f'        """{escaped[0]}')
    for line in escaped[1:]:
        lines.append(f"        {line}" if line else "")
    lines.append('        """')


def group_tools(tools: list["ToolBase"]) -> dict[str, list[tuple[str, "ToolBase"]]]:
    """Group tools by namespace prefix, stripping the prefix from method names."""
    from plugin.scripting.host_rpc import singularize_domain

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
            # This probe is only for the method-name prefix; it is not
            # singularize_domain; indexes_add_mark starts with index_
            # (the namespace) and would become method es_add_mark.
            if domain.endswith("s"):
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
        namespace = singularize_domain(namespace)

        # Catch hyphen/underscore spellings that only show up as the namespace key.
        if _domain_excluded(namespace):
            continue

        groups[namespace].append((rest, tool))
    return dict(groups)


def _proxy_namespace_attr(namespace: str) -> str:
    """Namespace -> Python attribute / class name prefix (e.g. 'python/sql' -> 'python_sql')."""
    return re.sub(r"\W", "_", namespace)


def _proxy_method_name(short_name: str) -> str:
    """Short name -> Python method name escaping keywords (e.g. 'import' -> 'import_')."""
    return short_name + "_" if keyword.iskeyword(short_name) else short_name


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
        '    """Send a tool call to the LibreOffice host and block for the result.',
        '    Strips None kwargs so tool defaults apply.',
        '    """',
        '    kwargs = {k: v for k, v in kwargs.items() if v is not None}',
        '    # Compute workers set this before importing plugin code. They have no',
        '    # office and no host pipe. The branch below would call get_ctx();',
        '    # exchange_tool_call would write a tool frame onto the pool stdio pipe.',
        '    if os.environ.get("WRITERAGENT_COMPUTE_WORKER") == "1":',
        '        raise RuntimeError("WriterAgent document tools are not available in the Python compute service.")',
        '    if not IS_WORKER:',
        '        try:',
        '            from plugin.scripting.host_rpc import execute_tool',
        '',
        '            return execute_tool(tool_name, kwargs, caller="script")',
        '        except Exception as e:',
        '            # Keep error .code from the RPC exception instead of dropping it',
        '            code = getattr(e, "code", None)',
        '            err = RuntimeError(f"Failed to execute tool in-process: {e}")',
        '            if code is not None:',
        '                setattr(err, "code", code)',
        '            raise err from e',
        '',
        '    from plugin.scripting.ipc import exchange_tool_call',
        '',
        '    return exchange_tool_call(tool_name, kwargs)',
        '',
        '',
        'def get_active_document_type() -> str:',
        '    """Return the active document\'s type (\'writer\', \'calc\', \'draw\', or \'unknown\')."""',
        '    res = _rpc_call("list_open_documents")',
        '    for doc in res.get("documents", []):',
        '        if doc.get("is_active"):',
        '            return doc.get("doc_type", "unknown")',
        '    return "unknown"',
        '',
        '',
    ]

    # Domain tools whitelist for host-side enforcement
    domain_tools_map = {}
    tool_methods_map = {}
    seen_methods: dict[tuple[str, str], str] = {}
    for ns, tool_list in sorted(groups.items()):
        domain_tools_map[ns] = sorted([t.name for _, t in tool_list if t.name])
        for short_name, t in tool_list:
            if not t.name:
                continue
            safe_ns = _proxy_namespace_attr(ns)
            method = _proxy_method_name(short_name)
            key = (safe_ns, method)
            prev = seen_methods.get(key)
            if prev is not None and prev != t.name:
                # Two tools that strip to one method used to emit def twice,
                # the later def won, and the catalog advertised one call for two
                # tools. make proxy-stubs stops instead.
                raise ValueError(
                    f"proxy method {safe_ns}.{method} is produced by both {prev!r} and {t.name!r}"
                )
            seen_methods[key] = t.name
            # TOOL_METHODS used to store the raw domain and the unescaped short
            # name, so _proxy_methods_by_tool did getattr(api, "python/sql") and
            # getattr(proxy, "import"), both missing, and format_script_api_catalog
            # logged no proxy method and omitted query_folder_sql and style_import.
            # The tuple is now the attribute the class body emits.
            tool_methods_map[t.name] = (safe_ns, method)

    pretty_map = pprint.pformat(domain_tools_map, indent=4, width=120)
    lines.append(f"DOMAIN_TOOLS = {pretty_map}")
    lines.append("")
    pretty_methods = pprint.pformat(tool_methods_map, indent=4, width=120)
    lines.append(f"TOOL_METHODS = {pretty_methods}")
    lines.append("")
    import builtins

    builtin_names = set(dir(builtins))
    all_exports = sorted(
        {_proxy_namespace_attr(ns) for ns in groups if _proxy_namespace_attr(ns) not in builtin_names}
        | {
            "DOMAIN_TOOLS",
            "TOOL_METHODS",
            "WORKFLOW_TASK_PREFIXES",
            "get_active_document_type",
        }
    )
    pretty_all = pprint.pformat(all_exports, indent=4, width=120)
    lines.append(f"__all__ = {pretty_all}")
    lines.append("")
    lines.append("")

    seen_namespaces: dict[str, str] = {}
    for ns in sorted(groups.keys()):
        safe_ns = _proxy_namespace_attr(ns)
        prev = seen_namespaces.get(safe_ns)
        if prev is not None and prev != ns:
            raise ValueError(
                f"proxy namespace collision: {ns!r} and {prev!r} both map to attribute {safe_ns!r}"
            )
        seen_namespaces[safe_ns] = ns

    for namespace in sorted(groups.keys()):
        tool_list = groups[namespace]
        # Emit a class that acts as a namespace. Domain names may hold characters that are invalid
        # in Python identifiers: hyphens, and the slash in "python/sql" (plugin/calc/base.py),
        # which produced "class _Python/sqlProxy:" and a module that does not import.
        safe_ns = _proxy_namespace_attr(namespace)
        class_name = "".join(part.capitalize() for part in safe_ns.split("_")) + "Proxy"
        lines.append(f"class _{class_name}:")
        lines.append(f'    """Proxy for {namespace} tools."""')
        lines.append("")

        for short_name, tool in sorted(tool_list, key=lambda x: x[0]):
            # domain_verb strip can yield a Python keyword (style_import -> import).
            short_name = _proxy_method_name(short_name)
            params = _collect_params(tool)

            pos = [f"{p.py_name}: {p.annotation}" for p in params if p.required]
            kw = [f"{p.py_name}: {p.annotation} = {p.default}" for p in params if not p.required]

            all_params_list = ["self"] + pos
            if kw:
                all_params_list.append("*")
                all_params_list.extend(kw)
            all_params = ", ".join(all_params_list)

            rpc_args: list[str] = []
            for p in params:
                if p.is_keyword:
                    rpc_args.append(f'**{{"{p.schema_key}": {p.py_name}}}')
                else:
                    rpc_args.append(f"{p.schema_key}={p.py_name}")

            kwargs_body = (", " + ", ".join(rpc_args)) if rpc_args else ""

            lines.append(f"    def {short_name}({all_params}) -> dict[str, Any]:")
            _append_docstring(lines, _method_doc_lines(tool, params=params))
            lines.append(f'        return _rpc_call("{tool.name}"{kwargs_body})')
            lines.append("")

        # Singleton instance (keep DOMAIN_TOOLS key as registered domain string)
        lines.append(f"{safe_ns} = _{class_name}()")
        lines.append("")
        lines.append("")

    return "\n".join(lines) + "\n"


def _config_in_scratch_dir(tmp_dir: str) -> None:
    """Point writeragent.json at *tmp_dir* before bootstrap runs.

    get_tools() bootstraps the plugin, and init_config() resolves the config path from the UNO
    context. Outside LibreOffice that context is a MagicMock, and since config.py started
    rejecting mock paths (CONFIG_PATH_ERROR) the generator crashed. ``make proxy-stubs`` then
    redirected nothing into writeragent_api.py and left it empty. A path already set makes
    init_config() return early; tests/conftest.py sets it the same way.
    """
    from plugin.framework import config as config_mod

    config_mod._resolved_config_path = os.path.join(tmp_dir, "writeragent.json")


def validate_generated(code: str) -> None:
    """Ensure the generated module compiles and contains core tools."""
    compiled = compile(code, "<generated>", "exec")
    ns: dict[str, Any] = {}
    exec(compiled, ns)
    domain_tools = ns.get("DOMAIN_TOOLS")
    if not isinstance(domain_tools, dict) or not domain_tools:
        raise ValueError("Validation failed: DOMAIN_TOOLS is empty")
    core_tools = domain_tools.get("core", [])
    if "list_open_documents" not in core_tools:
        raise ValueError("Validation failed: DOMAIN_TOOLS['core'] missing 'list_open_documents'")


def _atomic_write(target_path: str, content: str) -> None:
    target_dir = os.path.dirname(os.path.abspath(target_path))
    os.makedirs(target_dir, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=target_dir, delete=False, encoding="utf-8") as f:
        temp_path = f.name
        f.write(content)
    try:
        os.replace(temp_path, target_path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate writeragent_api.py tool proxies.")
    parser.add_argument("--output", help="Write generated module to PATH atomically")
    parser.add_argument("--check", help="Verify PATH matches generated output")
    args = parser.parse_args(argv)

    with _mock_uno():
        from plugin.main import get_tools

        with tempfile.TemporaryDirectory(prefix="wa-proxies-") as tmp_dir:
            _config_in_scratch_dir(tmp_dir)
            registry = get_tools()
            all_tools = registry.get_tools(
                filter_doc_type=False,
                exclude_tiers=frozenset({"specialized_control"}),
            )
            code = generate_module(all_tools)

    try:
        validate_generated(code)
    except Exception as exc:
        sys.stderr.write(f"generate_tool_proxies: {exc}\n")
        sys.exit(1)

    if args.check:
        if not os.path.exists(args.check):
            sys.stderr.write(f"generate_tool_proxies: --check target {args.check!r} does not exist\n")
            sys.exit(1)
        with open(args.check, encoding="utf-8") as f:
            target_content = f.read()
        if target_content != code:
            sys.stderr.write(
                f"generate_tool_proxies: --check failed for {args.check!r} (run make proxy-stubs to regenerate)\n"
            )
            sys.exit(1)
        return

    if args.output:
        _atomic_write(args.output, code)
        return

    print(code, end="")


if __name__ == "__main__":
    main()
