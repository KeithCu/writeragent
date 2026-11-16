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
import ast
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


def _qualify_proxy_annotation(annotation: str, shadowed: frozenset[str]) -> str:
    """Avoid class-body method names shadowing builtins in parameter annotations."""
    if "list" in shadowed:
        annotation = annotation.replace("list[", "builtins.list[")
    if "dict" in shadowed:
        annotation = annotation.replace("dict[", "builtins.dict[")
    if "type" in shadowed and annotation.startswith("type"):
        annotation = annotation.replace("type", "builtins.type", 1)
    return annotation


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
    because a boolean True made apply_content(..., dry_run=True) a
    silent no-op. Optionals without a schema default stay None in
    schema_to_signature so call_host_tool can drop them.
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
    # Read tool.parameters directly, not get_parameters(doc_type). Chart tools' get_parameters
    # strips Calc or Writer/Draw fields per doc_type, but proxy stubs must keep the union.
    # The only other get_parameters override is python_run_script
    # (in API_EXCLUDED_TOOLS).
    props = (tool.parameters or {}).get("properties", {})
    required_set = set((tool.parameters or {}).get("required", []))
    tool_name = getattr(tool, "name", "") or ""

    records: list[ParamRecord] = []

    for key, schema in props.items():
        if not key.isidentifier() or key == "self":
            raise ValueError(f"Invalid parameter name {key!r} in tool {tool_name!r}")
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
    "delegate_to_subagent",
    "delegate_to_subagent",
    "delegate_to_subagent",
    # Python outer→inner LLM hop. The inner smol agent still calls
    # workflow_finished as a registered ToolBase, not via this proxy.
    "python_delegate_domains",
    "workflow_finished",
    # Re-entering the venv worker from a script deadlocks the pipe. The host
    # already rejects the call; the generated proxy must not advertise it.
    "python_run_script",
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


# Every wa.<domain> namespace for domain-prefixed tools (<domain>_<verb>[_<object>], e.g.
# footnote_list -> wa.footnote.list, sheet_get_filter -> wa.sheet.get_filter). The longest entry
# that prefixes the name followed by "_" becomes wa.<domain>; the rest becomes the method.
# Unscoped document-wide collections in COLLECTION_NAMESPACES emit Python container dunders
# instead of flat list / get / delete methods.
# App-level tools (read_range, apply_content, get_tree, ...) omit the app prefix in their
# tool name and land on wa.writer, wa.calc, or wa.draw (APP_NAMESPACES) from the app they
# are registered for. make proxy-stubs stops on a tool name that starts with writer_/calc_/draw_
# or has neither an API_DOMAINS prefix nor a single app.
API_DOMAINS: frozenset[str] = frozenset({
    "bookmark", "cell_comment", "chart", "comment", "conditional_formatting",
    "design", "document", "embedded", "field", "footnote", "form", "frame",
    "image", "index", "mail_merge", "manual", "math", "memory", "named_range", "nav",
    "page", "page_comment", "pivot_table", "placeholder", "section", "shape", "sheet",
    "slide", "sql", "style", "table", "tool", "tracking", "web",
})
APP_NAMESPACES: frozenset[str] = frozenset({"writer", "calc", "draw"})

# Unscoped document-wide collections exposed as Python containers (for item in wa.<ns>,
# len(wa.<ns>), name in wa.<ns>, wa.<ns>[name], del wa.<ns>[name]). Declared explicitly
# because schemas cannot express document-wide uniqueness: scoped families (image, named_range)
# and positional/multi-key families (slide, field, table) stay plain methods.
COLLECTION_NAMESPACES: frozenset[str] = frozenset({
    "bookmark",
    "frame",
    "pivot_table",
    "section",
    "sheet",
})

# A non-collection list tool with an optional integer start parameter in this set
# gets a lazy namespace __iter__ (for item in wa.<ns>). The response must hold one
# list of dict rows plus has_more / next_index or truncated.
PAGED_START_PARAMS = frozenset({"start_index", "offset"})

# Top-level preamble of the generated writeragent_api.py, kept in one place so
# check_api_domains derives reserved names from the AST instead of a hand copy.
_PREAMBLE_LINES: tuple[str, ...] = (
    '"""Auto-generated WriterAgent tool proxy API.',
    '',
    'Generated by scripts/generate_tool_proxies.py — DO NOT EDIT.',
    'Provides Python-native access to WriterAgent tools from venv subprocess scripts.',
    'Tool names follow <domain>_<verb>[_<object>] (wa.<domain>.<rest>) or omit the app',
    'prefix for app-level tools (wa.writer.<name>, wa.calc.<name>, wa.draw.<name>).',
    'Collection namespaces (bookmark, frame, pivot_table, section, sheet) expose Python',
    'container access (iteration, len, in, [], del) returning dict snapshots whose keys',
    'also read as attributes.',
    'Every method returns a dict result whose keys also read as attributes (an ok',
    'status key is dropped); a tool error raises RuntimeError with .code and Stop',
    'raises UserStopped. Paged list namespaces (comment, page_comment, tracking)',
    'iterate every page lazily: for item in wa.comment.',
    'Method docstrings include the full tool description and an Args section',
    '(schema description, enum values, and required or optional).',
    '',
    'Skip replacing this with a __getattr__ proxy over DOMAIN_TOOLS: explicit',
    'per-tool classes are the public script API (IDE jump and types). Change this',
    'generator if the file is too large; do not slim the generated module by hand.',
    '"""',
    'from __future__ import annotations',
    'from typing import Any, NoReturn, TYPE_CHECKING',
    'from plugin.framework.constants import WORKFLOW_TASK_PREFIXES as _WORKFLOW_TASK_PREFIXES',
    '',
    'if TYPE_CHECKING:',
    '    import builtins  # proxy methods named list/dict shadow builtins in annotations',
    '    from collections.abc import Iterator',
    '',
    '# Re-export so venv scripts and tests share the comment-scan prefix tuple.',
    'WORKFLOW_TASK_PREFIXES = _WORKFLOW_TASK_PREFIXES',
    '',
    'from plugin.scripting.host_tool_call import call_host_tool',
    '',
    '',
    'class _Item(dict[str, Any]):',
    '    """Collection entry snapshot whose keys also read as attributes."""',
    '',
    '    __slots__: tuple[str, ...] = ()',
    '',
    '    def __getattr__(self, key: str) -> Any:',
    '        try:',
    '            return self[key]',
    '        except KeyError:',
    '            raise AttributeError(key) from None',
    '',
    '',
    'def _tool_failure(res: Any) -> BaseException:',
    '    """Build RuntimeError with .code (or UserStopped) matching ipc.exchange_tool_call.',
    '',
    '    Host tool errors arrive as dicts over RPC rather than exceptions, so',
    '    proxies inspect status and raise this uniform error shape. Stop',
    '    becomes UserStopped so except Exception cannot swallow it.',
    '    """',
    '    if isinstance(res, dict):',
    '        msg = str(res.get("message") or res.get("error") or res)',
    '        if res.get("code") == "USER_STOPPED":',
    '            from plugin.framework.ipc import UserStopped',
    '            return UserStopped(msg)',
    '        err = RuntimeError(msg)',
    '        code = res.get("code")',
    '        if code is not None:',
    '            setattr(err, "code", code)',
    '        return err',
    '    return RuntimeError(str(res))',
    '',
    '',
    'def _list_items(res: Any) -> list[_Item]:',
    '    """Extract the single list of entry dicts from a *_list tool response.',
    '',
    '    Ignores status and warnings; raises RuntimeError if the tool failed or',
    '    returned a shape without a single list payload of dicts.',
    '    """',
    '    if not isinstance(res, dict) or res.get("status") == "error":',
    '        raise _tool_failure(res)',
    '    list_vals = [val for key, val in res.items() if key != "warnings" and isinstance(val, list)]',
    '    if len(list_vals) != 1:',
    '        raise RuntimeError(f"Expected one list in tool result, got keys {sorted(res)}")',
    '    items: list[_Item] = []',
    '    for entry in list_vals[0]:',
    '        if not isinstance(entry, dict):',
    '            raise RuntimeError(f"Expected dict entries in list result, got {type(entry).__name__}")',
    '        items.append(_Item(entry))',
    '    return items',
    '',
    '',
    'def _result(res: Any) -> _Item:',
    '    """Unwrap one tool response for a flat proxy method.',
    '',
    '    Raises the tool failure for status error or a non-dict response. An ok',
    '    status key is dropped; other statuses (skipped, partial) stay readable.',
    '    """',
    '    if not isinstance(res, dict) or res.get("status") == "error":',
    '        raise _tool_failure(res)',
    '    if res.get("status") == "ok":',
    '        res = {k: v for k, v in res.items() if k != "status"}',
    '    return _Item(res)',
    '',
    '',
    'def _iter_pages(tool_name: str, start_key: str) -> Iterator[_Item]:',
    '    """Yield every row of a paged *_list tool, fetching a page only when reached.',
    '',
    '    Each page is one host call and a fresh snapshot. Pages continue while the',
    '    response says has_more (comment_list, tracking_list) or truncated',
    '    (page_comment_list); next_index wins over start + rows when present.',
    '    """',
    '    start = 0',
    '    while True:',
    '        res = call_host_tool(tool_name, **{start_key: start})',
    '        items = _list_items(res)',
    '        yield from items',
    '        if not items or not (res.get("has_more") or res.get("truncated")):',
    '            return',
    '        start = res.get("next_index", start + len(items))',
    '',
    '',
    'def _require_str_key(key: object) -> str:',
    '    """Raise KeyError for non-string keys before sending a keyed host RPC."""',
    '    if not isinstance(key, str):',
    '        raise KeyError(key)',
    '    return key',
    '',
    '',
    'def _find_item(res: Any, name: object) -> _Item | None:',
    '    """Return the item with matching name from a *_list response, or None."""',
    '    if not isinstance(name, str):',
    '        return None',
    '    for item in _list_items(res):',
    '        if item.get("name") == name:',
    '            return item',
    '    return None',
    '',
    '',
    'def _raise_keyed_failure(res: Any, name: str, list_call: Any) -> NoReturn:',
    '    """Distinguish a missing key from other tool errors using one list call.',
    '',
    '    Tool not-found error codes differ across families, so containers confirm',
    '    absence via list_call before raising KeyError; otherwise they raise the',
    '    original tool failure.',
    '    """',
    '    try:',
    '        missing = _find_item(list_call(), name) is None',
    '    except RuntimeError:',
    '        missing = False',
    '    if missing:',
    '        raise KeyError(name)',
    '    raise _tool_failure(res)',
    '',
    '',
    'def _get_item(res: Any, name: str, list_call: Any) -> _Item:',
    '    """Unwrap a get/get_info response into an _Item, or raise KeyError/RuntimeError."""',
    '    if not isinstance(res, dict) or res.get("status") == "error":',
    '        _raise_keyed_failure(res, name, list_call)',
    '    payload = {k: v for k, v in res.items() if k != "status"}',
    '    if len(payload) == 1:',
    '        only_val = next(iter(payload.values()))',
    '        if isinstance(only_val, dict):',
    '            payload = dict(only_val)',
    '    item = _Item(payload)',
    '    item.setdefault("name", name)',
    '    return item',
    '',
    '',
    'def _check_keyed(res: Any, name: str, list_call: Any) -> None:',
    '    """Validate a keyed mutation response, or raise KeyError/RuntimeError."""',
    '    if not isinstance(res, dict) or res.get("status") == "error":',
    '        _raise_keyed_failure(res, name, list_call)',
    '',
    '',
    'def get_active_document_type() -> str:',
    '    """Return the active document\'s type (\'writer\', \'calc\', \'draw\', or \'unknown\')."""',
    '    res = call_host_tool("document_list_open")',
    '    for doc in res.get("documents", []):',
    '        if doc.get("is_active"):',
    '            return doc.get("doc_type", "unknown")',
    '    return "unknown"',
    '',
    '',
    'def help(name: str | None = None) -> str:',
    '    """Return signature and docstring help for a wa.<namespace>.<method> API, or list namespaces."""',
    '    return str(call_host_tool("writeragent_help", name=name))',
    '',
    '',
)

# Names set on writeragent_api at run time by named_scripts.attach_named_script_libraries,
# plus the module-level tables emitted after the preamble.
_RUNTIME_AND_TABLE_NAMES = frozenset({"scripts", "doc", "DOMAIN_TOOLS", "TOOL_METHODS", "__all__"})


def _collect_stmt_names(stmts: list[ast.stmt], out: set[str]) -> None:
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(stmt.name)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                out.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                out.add(alias.asname or alias.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    out.add(target.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            out.add(stmt.target.id)
        elif isinstance(stmt, ast.If):
            _collect_stmt_names(stmt.body, out)
            _collect_stmt_names(stmt.orelse, out)


def _preamble_names() -> frozenset[str]:
    """Top-level names bound by ``_PREAMBLE_LINES``."""
    tree = ast.parse("\n".join(_PREAMBLE_LINES))
    names: set[str] = set()
    _collect_stmt_names(tree.body, names)
    return frozenset(names)


def alias_import_names() -> frozenset[str]:
    """First components a script may import as ``writeragent.<name>``.

    The sandbox allows those imports (``from writeragent.vision import run_vision``).
    Python then sets the submodule as an attribute of the parent module, so a
    ``wa.vision`` namespace would turn into that submodule for the rest of the run.
    """
    from plugin.scripting.sandbox import VENV_AUTHORIZED_IMPORTS

    return frozenset(
        entry.split(".")[1]
        for entry in VENV_AUTHORIZED_IMPORTS
        if entry.startswith(("plugin.", "writeragent.")) and "." in entry
    )


def check_api_domains(domains: frozenset[str]) -> None:
    """Raise ValueError for a domain that cannot be a ``wa.<domain>`` attribute."""
    import builtins

    reserved = _preamble_names() | _RUNTIME_AND_TABLE_NAMES | alias_import_names()
    for domain in sorted(domains):
        if not domain.isidentifier() or keyword.iskeyword(domain):
            raise ValueError(f"API domain {domain!r} is not a Python identifier")
        if domain in reserved or hasattr(builtins, domain):
            raise ValueError(f"API domain {domain!r} would shadow writeragent.{domain} or a builtin")


def split_tool_name(name: str, *, app: str | None = None) -> tuple[str, str]:
    """Tool name -> (``wa`` namespace, method). Keywords get a trailing ``_`` (style_import -> import_)."""
    if name.startswith(("writer_", "calc_", "draw_")):
        raise ValueError(
            f"tool {name!r} must not carry an app prefix (writer_/calc_/draw_); "
            "app-level tools omit the prefix and land on wa.<app> from the app they are registered for"
        )
    domain = max((d for d in API_DOMAINS if name.startswith(d + "_")), key=len, default=None)
    if domain is not None:
        return domain, _proxy_method_name(name[len(domain) + 1 :])
    if app in APP_NAMESPACES:
        return app, _proxy_method_name(name)
    raise ValueError(
        f"tool {name!r} does not start with an API domain; name it <domain>_<verb>[_<object>] "
        "and add a new domain to API_DOMAINS in scripts/generate_tool_proxies.py, or register it "
        "for a single app (writer/calc/draw)"
    )


def _api_tools(tools: list["ToolBase"]) -> list["ToolBase"]:
    """Tools that get a proxy: drop orchestration tools and sidebar/chat-mode domains."""
    kept = []
    for tool in tools:
        name = tool.name or ""
        if not name or name in API_EXCLUDED_TOOLS:
            continue
        domain = getattr(tool, "specialized_domain", None)
        if _domain_excluded(domain if isinstance(domain, str) else None):
            continue
        kept.append(tool)
    return kept


def _tool_app(tool: "ToolBase") -> str | None:
    """Single app ('writer', 'calc', 'draw') a tool is registered for, else None."""
    doc_types = set(getattr(tool, "doc_types", []) or [])
    if not doc_types:
        for svc in getattr(tool, "uno_services", []) or []:
            if "text.TextDocument" in svc:
                doc_types.add("writer")
            elif "sheet.SpreadsheetDocument" in svc:
                doc_types.add("calc")
            elif "drawing.DrawingDocument" in svc or "presentation.PresentationDocument" in svc:
                doc_types.add("draw")
    if doc_types == {"draw", "impress"}:
        return "draw"
    if len(doc_types) == 1:
        app = next(iter(doc_types))
        return "draw" if app == "impress" else app
    return None


def _allowlist_domain(tool: "ToolBase") -> str:
    """``DOMAIN_TOOLS`` key: the raw specialized_domain, else the single document type, else core.

    host_rpc.resolve_allowed_tools looks these keys up with the specialized domain names
    the chat passes (``shapes``, ``ranges``, ``python/sql``) and with ``writer`` / ``calc`` /
    ``draw`` / ``core``. One key can span several wa namespaces (``ranges`` holds
    sort_range and named_range_*).
    """
    domain = getattr(tool, "specialized_domain", None)
    if isinstance(domain, str) and domain:
        return domain
    return _tool_app(tool) or "core"


def group_tools(tools: list["ToolBase"]) -> dict[str, list[tuple[str, "ToolBase"]]]:
    """Group proxy tools by ``wa`` namespace with their method names (see split_tool_name)."""
    groups: dict[str, list[tuple[str, "ToolBase"]]] = defaultdict(list)
    for tool in _api_tools(tools):
        namespace, method = split_tool_name(tool.name or "", app=_tool_app(tool))
        groups[namespace].append((method, tool))
    return dict(groups)


def domain_tools(tools: list["ToolBase"]) -> dict[str, list[str]]:
    """``DOMAIN_TOOLS``: allowlist key -> sorted tool names (see _allowlist_domain)."""
    keyed: dict[str, set[str]] = defaultdict(set)
    for tool in _api_tools(tools):
        keyed[_allowlist_domain(tool)].add(tool.name or "")
    return {key: sorted(names) for key, names in sorted(keyed.items())}


def _proxy_method_name(short_name: str) -> str:
    """Short name -> Python method name escaping keywords (e.g. 'import' -> 'import_')."""
    return short_name + "_" if keyword.iskeyword(short_name) else short_name


class CollectionPlan(NamedTuple):
    """Validated list / optional get / delete mapping for a COLLECTION_NAMESPACES proxy."""

    list_tool: "ToolBase"
    get_tool: "ToolBase | None"
    delete_tool: "ToolBase"
    key: ParamRecord

    def dunder(self, tool: "ToolBase") -> str | None:
        if tool is self.list_tool:
            return "__iter__"
        if tool is self.get_tool:
            return "__getitem__"
        if tool is self.delete_tool:
            return "__delitem__"
        return None


def collection_plan(namespace: str, tool_list: list[tuple[str, "ToolBase"]]) -> CollectionPlan:
    """Validate and plan container dunder emission for a COLLECTION_NAMESPACES namespace."""
    by_method = {method: tool for method, tool in tool_list}
    list_tool = by_method.get("list")
    delete_tool = by_method.get("delete")
    if list_tool is None or delete_tool is None:
        raise ValueError(f"collection namespace {namespace!r} must have both 'list' and 'delete' tools")

    list_params = _collect_params(list_tool)
    if any(p.required for p in list_params):
        raise ValueError(
            f"collection namespace {namespace!r} list tool {list_tool.name!r} must not have required parameters"
        )
    if any(p.schema_key in PAGED_START_PARAMS for p in list_params):
        raise ValueError(
            f"collection namespace {namespace!r} list tool {list_tool.name!r} must not have a paging parameter"
        )

    del_req = [p for p in _collect_params(delete_tool) if p.required]
    if len(del_req) != 1 or del_req[0].annotation != "str":
        raise ValueError(
            f"collection namespace {namespace!r} delete tool {delete_tool.name!r} "
            "must have exactly one required str parameter"
        )
    key = del_req[0]

    get_tool = by_method.get("get")
    get_info_tool = by_method.get("get_info")
    if get_tool is not None and get_info_tool is not None:
        raise ValueError(f"collection namespace {namespace!r} has both 'get' and 'get_info' tools")
    get_tool = get_tool or get_info_tool
    if get_tool is not None:
        get_req = [p for p in _collect_params(get_tool) if p.required]
        if [p.schema_key for p in get_req] != [key.schema_key]:
            raise ValueError(
                f"collection namespace {namespace!r} get tool {get_tool.name!r} "
                f"required parameters must be [{key.schema_key!r}]"
            )

    return CollectionPlan(list_tool=list_tool, get_tool=get_tool, delete_tool=delete_tool, key=key)


def paging_start_param(namespace: str, tool_list: list[tuple[str, "ToolBase"]]) -> ParamRecord | None:
    """Return the paging start ParamRecord for a paged namespace's list tool, else None."""
    for method, tool in tool_list:
        if method != "list":
            continue
        params = _collect_params(tool)
        start = next(
            (p for p in params if not p.required and p.schema_key in PAGED_START_PARAMS and p.annotation == "int | None"),
            None,
        )
        if start is None:
            return None
        if any(p.required for p in params):
            raise ValueError(f"paged namespace {namespace!r} list tool {tool.name!r} must not have required parameters")
        return start
    return None


def _rpc_kwargs(params: list[ParamRecord]) -> str:
    """Build the trailing ', key=val, ...' argument fragment for call_host_tool."""
    rpc_args: list[str] = []
    for p in params:
        if p.is_keyword:
            rpc_args.append(f'**{{"{p.schema_key}": {p.py_name}}}')
        else:
            rpc_args.append(f"{p.schema_key}={p.py_name}")
    return (", " + ", ".join(rpc_args)) if rpc_args else ""


def _collection_doc(
    kind: str,
    ns: str,
    key_name: str,
    list_tool_name: str,
    dropped_optionals: list[str],
    has_get: bool,
) -> str:
    form = {
        "iter": f"for item in wa.{ns}",
        "getitem": f"wa.{ns}[{key_name}]",
        "delitem": f"del wa.{ns}[{key_name}]",
    }[kind]
    if kind == "iter":
        read_form = f"wa.{ns}[{key_name}]" if has_get else f"wa.{ns}[{key_name}] (read from this list)"
        detail = (
            f"Collection form: ``{form}`` (also ``len(wa.{ns})``, ``{key_name} in wa.{ns}``, and ``{read_form}``). "
            "Each call fetches a fresh snapshot in one host RPC and yields dict items whose keys "
            "also read as attributes. Tool errors raise RuntimeError with .code."
        )
    elif kind == "getitem":
        source = (
            "one host RPC (plus one list call on error to tell a missing name from other failures)"
            if has_get
            else f"one ``{list_tool_name}`` RPC"
        )
        detail = (
            f"Collection form: ``{form}``. Fetches a fresh dict snapshot whose keys also read as "
            f"attributes ({source}). Missing names raise KeyError; other tool errors raise "
            "RuntimeError with .code."
        )
    else:
        detail = (
            f"Collection form: ``{form}``. Deletes in one host RPC (plus one list call on error "
            "to tell a missing name from other failures); missing names raise KeyError and other "
            "tool errors raise RuntimeError with .code."
        )
    if dropped_optionals:
        joined = ", ".join(dropped_optionals)
        detail += f" The collection form passes no optional arguments ({joined}); filter the items in Python."
    return detail


def _with_collection_doc(base_doc: list[str], note: str) -> list[str]:
    return [*base_doc, "", note] if base_doc else [note]


def _collection_lines(ns: str, plan: CollectionPlan) -> list[str]:
    """Emit __iter__, __len__, __contains__, __getitem__ and __delitem__ for a collection proxy."""
    key = plan.key
    key_name = key.py_name
    key_rpc = _rpc_kwargs([key])
    list_name = plan.list_tool.name or ""
    del_name = plan.delete_tool.name or ""
    dropped_list = [p.py_name for p in _collect_params(plan.list_tool) if not p.required]
    dropped_del = [p.py_name for p in _collect_params(plan.delete_tool) if not p.required]

    lines: list[str] = []

    # __iter__
    lines.append("    def __iter__(self) -> Iterator[_Item]:")
    iter_doc = _with_collection_doc(
        _method_doc_lines(plan.list_tool, params=[]),
        _collection_doc("iter", ns, key_name, list_name, dropped_list, plan.get_tool is not None),
    )
    _append_docstring(lines, iter_doc)
    lines.append(f'        return iter(_list_items(call_host_tool("{list_name}")))')
    lines.append("")

    # __len__
    lines.append("    def __len__(self) -> int:")
    _append_docstring(lines, [f"Return the number of {ns} items (calls {list_name})."])
    lines.append(f'        return len(_list_items(call_host_tool("{list_name}")))')
    lines.append("")

    # __contains__
    lines.append(f"    def __contains__(self, {key_name}: object) -> bool:")
    _append_docstring(lines, [f"Return True if a {ns} item named {key_name} exists (calls {list_name})."])
    lines.append(f'        return _find_item(call_host_tool("{list_name}"), {key_name}) is not None')
    lines.append("")

    # __getitem__
    lines.append(f"    def __getitem__(self, {key_name}: str) -> _Item:")
    if plan.get_tool is not None:
        get_name = plan.get_tool.name or ""
        dropped_get = [p.py_name for p in _collect_params(plan.get_tool) if not p.required]
        get_doc = _with_collection_doc(
            _method_doc_lines(plan.get_tool, params=[key]),
            _collection_doc("getitem", ns, key_name, list_name, dropped_get, True),
        )
        _append_docstring(lines, get_doc)
        lines.append(f"        {key_name} = _require_str_key({key_name})")
        lines.append(
            f'        return _get_item(call_host_tool("{get_name}"{key_rpc}), {key_name}, '
            f'lambda: call_host_tool("{list_name}"))'
        )
    else:
        get_doc = _with_collection_doc(
            [f"Get a {ns} item snapshot by {key_name} from {list_name}."],
            _collection_doc("getitem", ns, key_name, list_name, dropped_list, False),
        )
        _append_docstring(lines, get_doc)
        lines.append(f'        item = _find_item(call_host_tool("{list_name}"), _require_str_key({key_name}))')
        lines.append("        if item is None:")
        lines.append(f"            raise KeyError({key_name})")
        lines.append("        return item")
    lines.append("")

    # __delitem__
    lines.append(f"    def __delitem__(self, {key_name}: str) -> None:")
    del_doc = _with_collection_doc(
        _method_doc_lines(plan.delete_tool, params=[key]),
        _collection_doc("delitem", ns, key_name, list_name, dropped_del, plan.get_tool is not None),
    )
    _append_docstring(lines, del_doc)
    lines.append(f"        {key_name} = _require_str_key({key_name})")
    lines.append(
        f'        _check_keyed(call_host_tool("{del_name}"{key_rpc}), {key_name}, '
        f'lambda: call_host_tool("{list_name}"))'
    )
    lines.append("")

    return lines


def _paged_doc(ns: str, dropped_optionals: list[str]) -> str:
    arg_note = f" ({', '.join(dropped_optionals)})" if dropped_optionals else ""
    return (
        f"Iterator form: ``for item in wa.{ns}`` reads every page lazily, one host RPC per page, "
        "as dict items whose keys also read as attributes. Each page is a fresh snapshot, so "
        f"collect ``list(wa.{ns})`` before deleting in a loop. Membership (``in``) compares whole "
        f"rows; match a field instead, e.g. ``any(r.get(key) == value for r in wa.{ns})``. "
        f"It passes no optional arguments{arg_note}; filter the items in Python."
    )


def _paged_iter_lines(ns: str, list_tool: "ToolBase", start: ParamRecord) -> list[str]:
    """Emit __iter__ for a paged namespace proxy."""
    tool_name = list_tool.name or ""
    lines = ["    def __iter__(self) -> Iterator[_Item]:"]
    _append_docstring(
        lines,
        [f"Iterate every {ns} row lazily, one {tool_name} call per page (see list)."],
    )
    lines.append(f'        return _iter_pages("{tool_name}", "{start.schema_key}")')
    lines.append("")
    return lines


def generate_module(tools: list["ToolBase"]) -> str:
    """Generate the complete writeragent_api.py module."""
    check_api_domains(API_DOMAINS | APP_NAMESPACES)
    groups = group_tools(tools)
    plans = {
        ns: collection_plan(ns, groups[ns])
        for ns in sorted(COLLECTION_NAMESPACES & groups.keys())
    }
    paged = {
        ns: p
        for ns in sorted(groups)
        if ns not in plans and (p := paging_start_param(ns, groups[ns])) is not None
    }

    lines = list(_PREAMBLE_LINES)

    domain_tools_map = domain_tools(tools)
    tool_methods_map = {}
    seen_methods: dict[tuple[str, str], str] = {}
    seen_tools: dict[str, "ToolBase"] = {}
    for ns, tool_list in sorted(groups.items()):
        plan = plans.get(ns)
        for method, t in tool_list:
            tool_name = t.name or ""
            key = (ns, method)
            prev = seen_methods.get(key)
            if prev is not None and prev != tool_name:
                # A keyword-escaped method (style_import -> import_) can equal a
                # tool literally named style_import_; without this check, the later
                # def would win silently.
                raise ValueError(
                    f"proxy method {ns}.{method} is produced by both {prev!r} and {tool_name!r}"
                )
            seen_methods[key] = tool_name
            prev_tool = seen_tools.get(tool_name)
            if prev_tool is not None and prev_tool is not t:
                raise ValueError(
                    f"duplicate tool name {tool_name!r} in proxy generation (TOOL_METHODS requires one entry per tool)"
                )
            seen_tools[tool_name] = t
            mapped_method = (plan.dunder(t) if plan is not None else None) or method
            tool_methods_map[tool_name] = (ns, mapped_method)

    pretty_map = pprint.pformat(domain_tools_map, indent=4, width=120)
    lines.append(f"DOMAIN_TOOLS = {pretty_map}")
    lines.append("")
    pretty_methods = pprint.pformat(tool_methods_map, indent=4, width=120)
    lines.append(f"TOOL_METHODS = {pretty_methods}")
    lines.append("")
    all_exports = sorted(
        set(groups)
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

    for namespace in sorted(groups.keys()):
        tool_list = groups[namespace]
        class_name = "".join(part.capitalize() for part in namespace.split("_")) + "Proxy"
        lines.append(f"class _{class_name}:")
        lines.append(f'    """Proxy for {namespace} tools."""')
        lines.append("")

        import builtins as _builtins_mod

        plan = plans.get(namespace)
        emitted_tools = [
            (method, tool)
            for method, tool in tool_list
            if plan is None or plan.dunder(tool) is None
        ]
        proxy_method_names = frozenset(n for n, _unused in emitted_tools)
        shadowed_names = proxy_method_names & set(dir(_builtins_mod))

        for method, tool in sorted(emitted_tools, key=lambda x: x[0]):
            params = _collect_params(tool)

            pos = [
                f"{p.py_name}: {_qualify_proxy_annotation(p.annotation, shadowed_names)}"
                for p in params
                if p.required
            ]
            kw = [
                f"{p.py_name}: {_qualify_proxy_annotation(p.annotation, shadowed_names)} = {p.default}"
                for p in params
                if not p.required
            ]

            all_params_list = ["self"] + pos
            if kw:
                all_params_list.append("*")
                all_params_list.extend(kw)
            all_params = ", ".join(all_params_list)

            kwargs_body = _rpc_kwargs(params)

            lines.append(f"    def {method}({all_params}) -> _Item:")
            doc_lines = _method_doc_lines(tool, params=params)
            if namespace in paged and method == "list":
                start_param = paged[namespace]
                dropped = [
                    p.py_name
                    for p in params
                    if not p.required and p.schema_key != start_param.schema_key
                ]
                doc_lines = _with_collection_doc(doc_lines, _paged_doc(namespace, dropped))
            _append_docstring(lines, doc_lines)
            lines.append(f'        return _result(call_host_tool("{tool.name}"{kwargs_body}))')
            lines.append("")

        if plan is not None:
            lines.extend(_collection_lines(namespace, plan))
        elif namespace in paged:
            list_tool = dict(tool_list)["list"]
            lines.extend(_paged_iter_lines(namespace, list_tool, paged[namespace]))

        lines.append(f"{namespace} = _{class_name}()")
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
    if "document_list_open" not in core_tools:
        raise ValueError("Validation failed: DOMAIN_TOOLS['core'] missing 'document_list_open'")


def _atomic_write(target_path: str, content: str) -> None:
    if os.path.exists(target_path):
        with open(target_path, encoding="utf-8") as f:
            if f.read() == content:
                return
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
        from plugin.main import bind

        bind()

        from plugin.app import get_tools

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
