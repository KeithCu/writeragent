# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Named My Scripts / This Document libraries for venv code (``wa.scripts`` / ``wa.doc``).

Library bodies are fetched over the existing ``tool_call`` pipe (not
``run_venv_python_script``). Defs are eval'd into a private namespace on the
current ``LocalPythonExecutor`` so later calls do not re-fetch the source.
Across runs the cache lives on that executor — document-keyed shared kernel
when session mode is shared.
"""

from __future__ import annotations

import ast
import hashlib
import keyword
import logging
import os
import re
import sys
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any

log = logging.getLogger(__name__)

ORIGIN_USER = "user"
ORIGIN_DOCUMENT = "document"

_NAMED_SCRIPT_MAX_BYTES = 200_000
_IDENT_NON_ALNUM = re.compile(r"[^0-9A-Za-z_]+")
_IDENT_MULTI_US = re.compile(r"_+")

# Current sandbox executor (set for the duration of one execute).
_current_executor: ContextVar[Any] = ContextVar("named_scripts_executor", default=None)

GET_NAMED_PYTHON_SCRIPT = "get_named_python_script"
LIST_NAMED_PYTHON_SCRIPTS = "list_named_python_scripts"


def python_identifier_from_script_name(name: str) -> str:
    """Turn a picker title into a Python identifier (tweak here, not call sites)."""
    raw = (name or "").strip()
    ident = _IDENT_NON_ALNUM.sub("_", raw)
    ident = _IDENT_MULTI_US.sub("_", ident).strip("_")
    if not ident:
        ident = "_script"
    if ident[0].isdigit():
        ident = f"_{ident}"
    if keyword.iskeyword(ident):
        ident = f"{ident}_"
    if not ident.isidentifier():
        ident = "_script"
    return ident


def script_body_hash(code: str) -> str:
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()


def _is_simple_constant(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_is_simple_constant(elt) for elt in node.elts)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _is_simple_constant(node.operand)
    return False


def extract_library_source(code: str) -> str:
    """Keep defs/classes/imports/constant assigns; drop module-level calls."""
    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        raise ValueError(f"Named script is not valid Python: {exc}") from exc

    keep: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
            keep.append(node)
        elif isinstance(node, ast.Assign):
            if all(isinstance(t, ast.Name) for t in node.targets) and _is_simple_constant(node.value):
                keep.append(node)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None and _is_simple_constant(node.value):
                keep.append(node)
    if not keep:
        return ""
    return ast.unparse(ast.Module(body=keep, type_ignores=[]))


def _rpc_named(tool_name: str, **kwargs: Any) -> Any:
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    if os.environ.get("WRITERAGENT_IS_WORKER") == "1":
        try:
            from plugin.scripting.writeragent_api import _rpc_call

            return _rpc_call(tool_name, **kwargs)
        except ImportError:
            import uuid

            from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES, read_pickle_frame, write_pickle_frame

            call_id = str(uuid.uuid4())
            request = {"type": "tool_call", "id": call_id, "tool": tool_name, "args": kwargs}
            write_pickle_frame(sys.stdout.buffer, request)
            response = read_pickle_frame(
                sys.stdin.buffer, require_dict=True, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES
            )
            if response is None:
                raise ConnectionError("Lost connection to LibreOffice host during tool call")
            if response.get("status") == "error":
                raise RuntimeError(response.get("message", "Unknown error"))
            return response.get("result", {})

    from plugin.scripting.host_rpc import execute_tool

    return execute_tool(tool_name, kwargs, caller="script")


def _executor_cache(executor: Any) -> dict[tuple[str, str], tuple[str, Any]]:
    cache = getattr(executor, "_named_script_cache", None)
    if cache is None:
        cache = {}
        executor._named_script_cache = cache
    return cache


def _checked_keys(executor: Any) -> set[tuple[str, str]]:
    checked = getattr(executor, "_named_script_checked", None)
    if checked is None:
        checked = set()
        executor._named_script_checked = checked
    return checked


def _eval_library(executor: Any, source: str, ident: str) -> SimpleNamespace:
    from plugin.contrib.smolagents.local_python_executor import evaluate_python_code

    extracted = extract_library_source(source)
    state: dict[str, Any] = {"__name__": ident}
    if extracted.strip():
        evaluate_python_code(
            extracted,
            static_tools=executor.static_tools or {},
            custom_tools=executor.custom_tools or {},
            state=state,
            authorized_imports=executor.authorized_imports,
            max_print_outputs_length=executor.max_print_outputs_length,
            timeout_seconds=executor.timeout_seconds,
        )
    skip = {"__name__", "_print_outputs", "_operations_count"}
    ns = SimpleNamespace()
    for key, val in state.items():
        if key in skip:
            continue
        setattr(ns, key, val)
    return ns


def load_named_script(origin: str, name: str, executor: Any | None = None) -> Any:
    """Return the library namespace for *name*, using the current executor cache."""
    executor = executor if executor is not None else _current_executor.get()
    if executor is None:
        raise RuntimeError("Named scripts are only available while a Python script is running.")
    if not isinstance(name, str) or not name.strip():
        raise AttributeError("Named script title must be a non-empty string")
    name = name.strip()
    cache = _executor_cache(executor)
    checked = _checked_keys(executor)
    key = (origin, name)
    # Once per execute (Isolated Run or Shared cell): hash-check. Same execute
    # later (Helpers.add then Helpers.mul) stays local.
    if key in cache and key in checked:
        return cache[key][1]
    known_hash = cache[key][0] if key in cache else None
    payload = _rpc_named(
        GET_NAMED_PYTHON_SCRIPT,
        name=name,
        origin=origin,
        known_hash=known_hash,
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid named-script payload for {name!r}")
    if payload.get("unchanged") and key in cache:
        checked.add(key)
        return cache[key][1]
    code = payload.get("code")
    if not isinstance(code, str):
        raise AttributeError(f"No {origin} script named {name!r}")
    if len(code.encode("utf-8")) > _NAMED_SCRIPT_MAX_BYTES:
        raise RuntimeError(f"Named script {name!r} is too large to import as a library")
    body_hash = str(payload.get("hash") or script_body_hash(code))
    ident = python_identifier_from_script_name(name)
    ns = _eval_library(executor, code, ident)
    cache[key] = (body_hash, ns)
    checked.add(key)
    return ns


class ScriptLibrary:
    """``wa.scripts`` (My Scripts) or ``wa.doc`` (This Document)."""

    _origin: str

    def __init__(self, origin: str) -> None:
        self._origin = origin
        self._executor: Any | None = None
        self._ident_map: dict[str, list[str]] | None = None

    def _names(self) -> list[str]:
        executor = self._executor if self._executor is not None else _current_executor.get()
        listing_cache = getattr(executor, "_named_script_listing", None) if executor is not None else None
        if listing_cache is None:
            listing = _rpc_named(LIST_NAMED_PYTHON_SCRIPTS)
            listing_cache = listing if isinstance(listing, dict) else {}
            if executor is not None:
                executor._named_script_listing = listing_cache
        raw = listing_cache.get(self._origin) or []
        if not isinstance(raw, list):
            return []
        return [str(n) for n in raw if isinstance(n, str)]

    def _ident_map_now(self) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {}
        for title in self._names():
            ident = python_identifier_from_script_name(title)
            mapping.setdefault(ident, []).append(title)
        self._ident_map = mapping
        return mapping

    def __getattr__(self, item: str) -> Any:
        if item.startswith("_"):
            raise AttributeError(item)
        mapping = self._ident_map_now()
        titles = mapping.get(item) or []
        if not titles:
            raise AttributeError(f"No {self._origin} script sanitizes to {item!r}")
        if len(titles) > 1:
            raise AttributeError(
                f"Multiple {self._origin} scripts map to {item!r}: {titles!r}. "
                "Use wa.scripts[title] / wa.doc[title] with the stored name."
            )
        return load_named_script(self._origin, titles[0], executor=self._executor)

    def __getitem__(self, name: str) -> Any:
        return load_named_script(self._origin, name, executor=self._executor)

    def __repr__(self) -> str:
        return f"ScriptLibrary(origin={self._origin!r})"


def attach_named_script_libraries(executor: Any | None = None) -> None:
    """Bind ``writeragent.scripts`` / ``writeragent.doc`` on the alias module.

    LibrePy omits ``writeragent_api``; ``import writeragent`` is
    ``writeragent_namespace``. Attach there first so Run Python Script still
    sees ``wa.scripts`` / ``wa.doc``.
    """
    mods: list[Any] = []
    try:
        from plugin.scripting import writeragent_namespace

        mods.append(writeragent_namespace)
    except ImportError:
        log.debug("named_scripts: writeragent_namespace missing", exc_info=True)
    try:
        from plugin.scripting import writeragent_api

        mods.append(writeragent_api)
    except ImportError:
        log.debug("named_scripts: writeragent_api missing (LibrePy)", exc_info=True)
    alias = sys.modules.get("writeragent")
    if alias is not None and alias not in mods:
        mods.append(alias)
    if not mods:
        return
    scripts = ScriptLibrary(ORIGIN_USER)
    doc = ScriptLibrary(ORIGIN_DOCUMENT)
    scripts._executor = executor
    doc._executor = executor
    for mod in mods:
        existing_s = getattr(mod, "scripts", None)
        if isinstance(existing_s, ScriptLibrary):
            existing_s._executor = executor
        else:
            mod.scripts = scripts
        existing_d = getattr(mod, "doc", None)
        if isinstance(existing_d, ScriptLibrary):
            existing_d._executor = executor
        else:
            mod.doc = doc


def bind_named_scripts_executor(executor: Any) -> None:
    """New execute: re-check hashes; keep module cache on the shared executor."""
    executor._named_script_checked = set()
    executor._named_script_listing = None
    _current_executor.set(executor)
    attach_named_script_libraries(executor)


def host_list_named_python_scripts(*, user_scripts: dict[str, str], document_scripts: dict[str, str]) -> dict[str, list[str]]:
    return {
        ORIGIN_USER: sorted(user_scripts.keys()),
        ORIGIN_DOCUMENT: sorted(document_scripts.keys()),
    }


def host_get_named_python_script(
    *,
    name: str,
    origin: str,
    known_hash: str | None,
    user_scripts: dict[str, str],
    document_scripts: dict[str, str],
) -> dict[str, Any]:
    store = user_scripts if origin == ORIGIN_USER else document_scripts
    if origin not in (ORIGIN_USER, ORIGIN_DOCUMENT):
        raise RuntimeError(f"Unknown named-script origin {origin!r}")
    code = store.get(name)
    if not isinstance(code, str):
        raise RuntimeError(f"No {origin} script named {name!r}")
    if len(code.encode("utf-8")) > _NAMED_SCRIPT_MAX_BYTES:
        raise RuntimeError(f"Named script {name!r} is too large to import as a library")
    digest = script_body_hash(code)
    if known_hash and known_hash == digest:
        return {"unchanged": True, "hash": digest, "name": name, "origin": origin}
    return {"unchanged": False, "hash": digest, "name": name, "origin": origin, "code": code}
